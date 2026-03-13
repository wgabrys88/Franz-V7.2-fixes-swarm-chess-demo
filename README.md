# Panel — A Dumb Message Router for VLM Brain Swarms

**Panel** is a multi-process system where independent "brain" scripts orchestrate screen observation, LLM reasoning, and physical input automation through a central HTTP router that has **zero domain knowledge**.

The router knows nothing about chess, images, or AI. It reads two fields — `agent` and `recipients` — and moves messages between processes. Everything else is the brains' problem.

---

## Architecture

### Before (Original)

```mermaid
graph TD
    subgraph Panel["panel.py :1236"]
        Router[HTTP Router]
    end

    subgraph Brains
        Observer[observer.py]
        Swarm[swarm.py]
    end

    subgraph External
        LMStudio[LM Studio :1235]
        Chrome[panel.html in Chrome]
        Win32[win32.py CLI]
    end

    Observer -->|POST /route| Router
    Swarm -->|POST /route| Router
    Router -->|subprocess| Win32
    Router -->|HTTP POST| LMStudio
    Router -->|SSE push| Chrome
    Chrome -->|POST /result| Router
    Router -->|SSE push| Observer
    Router -->|SSE push| Swarm

    Observer -.->|capture, annotate, vlm| Router
    Swarm -.->|vlm x8 specialists, capture, annotate, vlm executor, screen drag| Router

    style Panel fill:#1a1a2e,stroke:#4a9eff,color:#e8e8f0
    style Observer fill:#1a1a2e,stroke:#3ecf8e,color:#e8e8f0
    style Swarm fill:#1a1a2e,stroke:#f0a000,color:#e8e8f0
    style LMStudio fill:#1a1a2e,stroke:#c084fc,color:#e8e8f0
    style Chrome fill:#1a1a2e,stroke:#ff4455,color:#e8e8f0
    style Win32 fill:#1a1a2e,stroke:#06b6d4,color:#e8e8f0
```

**Problems in the original:**
- Duplicated `_parse_args()` in observer.py and swarm.py
- Duplicated SSE listener boilerplate in both brains
- Hardcoded URLs in every file independently
- `_err()` returned `None` instead of `NoReturn` — Pylance type narrowing failed
- `serve_forever()` called twice on the same server (daemon thread + main thread)
- `match` statement missing wildcard — `result` potentially unbound
- Magic values scattered outside frozen dataclasses (30.0 stale timeout, 640×640 capture size, semaphore count 3, DPI value 2, exit codes)
- Scale factor parsed from CLI but never used — capture always produced 640×640
- Specialists were invisible to the UI panel — no status reporting
- `ctypes.windll.user32.VkKeyScanW` used inconsistently instead of the declared `_user32` binding
- `text[:120]` and `text[:80]` slicing in debug prints violated no-slicing rule

### After (Revised)

```mermaid
graph TD
    subgraph Panel["panel.py :1236"]
        Router[HTTP Router]
    end

    subgraph Brains
        Observer[observer.py]
        Swarm[swarm.py]
        subgraph Specialists["8 Specialist Threads"]
            Pawn[pawn]
            Knight[knight]
            Bishop[bishop]
            Rook[rook]
            Queen[queen]
            King[king]
            Tactics[tactics]
            Positional[positional]
        end
    end

    subgraph Shared["brain_util.py"]
        ParseArgs[parse_brain_args]
        SSEListen[sse_listen]
        Capture[capture with scale]
        VLMHelpers[vlm / vlm_text]
        UIHelpers[ui_pending / ui_done / ui_error]
    end

    subgraph External
        LMStudio[LM Studio :1235]
        Chrome["panel.html in Chrome<br/>10-pane adaptive grid"]
        Win32["win32.py CLI<br/>--scale support"]
    end

    Observer -->|uses| Shared
    Swarm -->|uses| Shared
    Specialists -->|ui_pending / ui_done| Router
    Observer -->|POST /route| Router
    Swarm -->|POST /route| Router
    Router -->|subprocess --scale| Win32
    Router -->|HTTP POST| LMStudio
    Router -->|SSE push| Chrome
    Chrome -->|POST /result| Router
    Router -->|SSE push| Observer
    Router -->|SSE push| Swarm

    style Panel fill:#1a1a2e,stroke:#4a9eff,color:#e8e8f0
    style Observer fill:#1a1a2e,stroke:#3ecf8e,color:#e8e8f0
    style Swarm fill:#1a1a2e,stroke:#f0a000,color:#e8e8f0
    style Specialists fill:#0d0d0f,stroke:#c084fc,color:#e8e8f0
    style Shared fill:#0d0d0f,stroke:#06b6d4,color:#e8e8f0
    style LMStudio fill:#1a1a2e,stroke:#c084fc,color:#e8e8f0
    style Chrome fill:#1a1a2e,stroke:#ff4455,color:#e8e8f0
    style Win32 fill:#1a1a2e,stroke:#06b6d4,color:#e8e8f0
```

---

## What Changed — Summary

### `brain_util.py`
| Change | Detail |
|--------|--------|
| `PANEL_URL` constant | Eliminates hardcoded URLs across brains |
| `SSE_BASE_URL` constant | Same |
| `SSEConfig` dataclass | Frozen config for reconnect delay and timeout |
| `parse_brain_args()` | Shared `--region`/`--scale` parser, uses `enumerate`/`elif` |
| `sse_listen()` | Shared SSE connection/parsing/reconnect in daemon thread |
| `capture()` accepts `scale` | Scale-based capture alternative to fixed width/height |

### `win32.py`
| Change | Detail |
|--------|--------|
| `_err() -> NoReturn` | Pylance type narrowing now works after all guard calls |
| `exit_ok`/`exit_cancel`/`dpi_awareness` in `Win32Config` | No more magic values |
| `VkKeyScanW` in `_setup_bindings()` | Consistent `_user32` usage, new `_vk_scan()` helper |
| `get_arg` uses `enumerate` | Modern iteration |
| `--scale` support in `capture` | Computes output size from cropped region × scale |

### `panel.py`
| Change | Detail |
|--------|--------|
| `case _` wildcard in match | `result` always bound — Pylance clean |
| Single `serve_forever()` | `start()` no longer spawns daemon thread |
| `_Config` has `slots=True` | Consistent with all other dataclasses |
| Config fields for magic values | `stale_timeout`, `default_capture_width/height` |
| `_tandem_select` validates parts | No more potential `IndexError` |
| `_handle_capture` supports `capture_scale` | Passes `--scale` to win32.py when present |
| Removed dead `t > 1e12` branch | Was unreachable |

### `observer.py`
| Change | Detail |
|--------|--------|
| Uses `bu.parse_brain_args()` | No more duplicated arg parsing |
| Uses `bu.sse_listen()` | No more duplicated SSE boilerplate |
| Captures with `scale=cfg.scale` | Scale factor finally used |
| `error_retry_delay` in config | No more magic `3.0` |
| Removed `text[:120]` slicing | Full text in prints |

### `swarm.py`
| Change | Detail |
|--------|--------|
| Uses `bu.parse_brain_args()` | No more duplicated arg parsing |
| Uses `bu.sse_listen()` | No more duplicated SSE boilerplate |
| Semaphore created from `cfg.vlm_concurrency` | Not module-level, configurable |
| All 8 specialists report to UI | `ui_pending`/`ui_done`/`ui_error` per specialist |
| Removed dead `observer_image` parameter | Was passed but never used |
| Removed `[:80]` slicing | Full text in prints |
| Type hints on regex pattern/match | Pylance clean |
| Captures with `scale=cfg.scale` | Consistent with observer |

### `panel.html`
| Change | Detail |
|--------|--------|
| Adaptive grid: `repeat(min(count,5), 1fr)` + auto-rows | Handles 10+ agents in 2+ rows |
| `colorMap` as module-level `Map` | Replaces function-property pattern |
| `try/catch` on `JSON.parse` in SSE listeners | No silent crashes |
| Safe base64: `arrayBuffer` → `Uint8Array` → `btoa` | Compatible with Python/OpenAI API |
| `updateGridLayout()` extracted | Single source of truth for column logic |

---

## Issue Tracker

### Resolved: 7 High/Medium + 22 Low + 5 Cross-File

| ID | Severity | File | Issue | Resolution |
|----|----------|------|-------|------------|
| W1 | High | win32.py | `_err` returns `None` not `NoReturn` | Changed to `-> NoReturn` |
| P1 | High | panel.py | `match` missing wildcard, `result` unbound | Added `case _` |
| P2 | Medium | panel.py | `serve_forever()` called twice | Single call in `__main__` |
| X1 | Medium | observer+swarm | `_parse_args` duplicated | `bu.parse_brain_args()` |
| X2 | Medium | observer+swarm | SSE listener duplicated | `bu.sse_listen()` |
| S3 | Medium | swarm.py | Semaphore(3) hardcoded | `cfg.vlm_concurrency` |
| S4 | Medium | swarm.py | Module-level throttle | Created in `main()` |
| W2-W5, P4-P8, O3-O8, S5-S11, H2-H5 | Low | Various | Magic values, slicing, missing hints | All addressed |
| X3-X5 | Low-Info | Cross-file | URL duplication, unused scale | Constants + scale wired through |

---

## Files

| File | Role | Lines |
|------|------|-------|
| `brain_util.py` | Shared module for all brains | ~230 |
| `win32.py` | Windows 11 screen automation CLI | ~580 |
| `panel.py` | HTTP router, zero domain knowledge | ~340 |
| `observer.py` | Screen observation brain | ~80 |
| `swarm.py` | Multi-specialist decision brain | ~230 |
| `panel.html` | Browser UI with SSE + annotation | ~240 |

---

## Usage

```
python panel.py observer.py swarm.py
```

1. Select capture region (rubber-band on screen)
2. Select scale reference (horizontal width = desired scale factor)
3. Panel starts on `http://127.0.0.1:1236`
4. Open Chrome to the panel URL
5. Brains auto-launch and begin cycling

---

## Claude Opus 4 System Prompt

The following prompt is designed for a fresh conversation with no history. Copy it as-is into the system prompt field.

````
You are reviewing and maintaining a Python 3.13 + Windows 11 + Chrome project called "Panel" — a dumb message router for VLM brain swarms. Your role is to perform logical review, code review, log analysis, and produce modifications when asked. You work with one file at a time.

SYSTEM OVERVIEW:

Panel is a multi-process system where independent "brain" scripts orchestrate screen observation, LLM reasoning, and physical input automation through a central HTTP router that has ZERO domain knowledge.

COMPONENTS (6 files):

1. panel.py — ThreadingHTTPServer on 127.0.0.1:1236. Routes messages based ONLY on the "recipients" field. Four sync recipients: "capture" (subprocess to win32.py, returns image_b64), "annotate" (SSE round-trip through Chrome panel.html, returns annotated image_b64), "vlm" (HTTP POST to LM Studio on 127.0.0.1:1235, returns full response JSON), "screen" (subprocess to win32.py for mouse/keyboard actions). Any other recipient name is async SSE push to /agent-events?agent=<name>. Rule: at most one sync recipient per request. Panel logs everything to panel.txt via Python logging. VLM requests and responses are logged as full raw dicts (including base64 image data). Panel never inspects, modifies, or makes decisions based on message content. It reads only "agent" and "recipients" fields for routing. Launched as: python panel.py brain1.py brain2.py — it starts the HTTP server, then spawns each brain as a subprocess with --region and --scale args. The start() function creates the server and returns it. The __main__ block calls srv.serve_forever() once — there is no daemon thread for the server.

2. brain_util.py — Shared module imported by all brains. Contains: VLMConfig frozen dataclass (model="qwen3.5-0.8b", temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5, frequency_penalty=0.0, repeat_penalty=1.0, stream=False, plus optional stop/seed/logit_bias). All None-valued fields excluded from request JSON. Contains PANEL_URL="http://127.0.0.1:1236/route" and SSE_BASE_URL="http://127.0.0.1:1236/agent-events" constants. Contains SSEConfig frozen dataclass (reconnect_delay=1.0, timeout=6000.0). Contains parse_brain_args(argv) -> tuple[str, float] for shared --region/--scale parsing. Contains sse_listen(url, callback, sse_cfg) that runs SSE connection/parsing/reconnect in a daemon thread, calling callback(event_name, data_dict) for each event. Contains route() helper that POSTs to panel /route endpoint with agent+recipients+payload. Contains capture() that accepts either width/height or scale parameter — when scale>0, sends capture_scale to panel which passes --scale to win32.py. Contains annotate(), vlm(), vlm_text(), screen(), push(), ui_pending(), ui_done(), ui_error() convenience wrappers. Contains make_vlm_request(cfg, system_prompt, user_content) and make_vlm_request_with_image(cfg, system_prompt, image_b64, user_text). Contains grid/arrow overlay builders and coordinate helpers. SENTINEL="NONE", NORM=1000.

3. observer.py — Brain process. Runs a loop: capture screen (using scale factor) → annotate with 8x8 grid → send annotated image + prompt to VLM → push analysis text + image to UI and to swarm agent → wait for "cycle_done" SSE signal from swarm. Uses OBSERVER_VLM = VLMConfig(max_tokens=500). Uses bu.sse_listen() with callback. Uses bu.parse_brain_args(). Sequential — one VLM call per cycle.

4. swarm.py — Brain process. Receives observer analysis via SSE (using bu.sse_listen). Spawns 8 specialist threads (pawn/knight/bishop/rook/queen/king/tactics/positional) that each call VLM with text-only prompts. Each specialist reports to the UI panel via bu.ui_pending/ui_done/ui_error using their own name as the agent. Uses threading.Semaphore created from cfg.vlm_concurrency (default 3) to throttle concurrent VLM calls to match LM Studio's parallel slots. The semaphore is created in main() and passed explicitly — not module-level. 6 piece specialists share a template prompt via str.format(). Tactics and positional have distinct prompts. All prompts enforce strict output: "from to" or "NONE". After specialists finish, if multiple proposals exist, an executor captures screen (using scale), annotates with colored arrows per proposal, sends image to VLM to pick the best. Executor also throttled by the same semaphore. Winning move executed as a drag action via screen recipient. Signals observer cycle_done when finished. Uses SPECIALIST_VLM=VLMConfig(max_tokens=220) and EXECUTOR_VLM=VLMConfig(max_tokens=170).

5. panel.html — Single-page browser UI served at /. Connects to /agent-events?agent=ui via SSE. Displays agent status pills, text pane, image pane. Adaptive CSS grid: up to 5 columns, auto-rows for overflow — handles 10+ agent panes (observer + swarm + 8 specialists). Handles "annotate" events by rendering overlays on OffscreenCanvas and POSTing result back to /result. Overlay rendering supports arbitrary polygon2D data: any brain can send any number of overlay objects with points arrays, closed flag, stroke/fill colors. Base64 encoding after annotation uses arrayBuffer → Uint8Array → String.fromCharCode → btoa for clean standard base64 compatible with Python and OpenAI API. JSON.parse wrapped in try/catch for all SSE listeners. Color map uses module-level Map. Dark cyber aesthetic, independent dark theme (does not follow OS). Designed for 1080p 16:9 Chrome.

6. win32.py — CLI tool for Windows 11 screen automation. _err() returns NoReturn for proper Pylance type narrowing. Uses VkKeyScanW through _user32 binding (set up in _setup_bindings). Subcommands: capture (screenshot region to stdout as raw PNG bytes — accepts --scale as alternative to --width/--height, computes output dimensions from cropped region native pixels × scale), select_region (interactive rubber-band selection), drag/click/double_click/right_click (mouse actions), type_text/press_key/hotkey (keyboard), scroll_up/scroll_down, cursor_pos. Takes --region as "x1,y1,x2,y2" normalized 0-1000 coordinates. exit_ok/exit_cancel/dpi_awareness are in Win32Config frozen dataclass.

SCALE FACTOR:

When panel.py starts, the user selects a capture region and a horizontal scale reference. Scale = abs(x2-x1)/1000.0 from the reference selection. This scale is passed to brains via --scale. Brains pass it to bu.capture(scale=cfg.scale). Panel sends capture_scale to win32.py capture --scale. Win32 captures full screen → crops to region → computes output as (crop_w*scale, crop_h*scale) → stretches → PNG. Smaller images = faster VLM processing.

PROTOCOL DETAILS:

All brain-to-panel communication is POST /route with JSON body containing "agent" (string), "recipients" (list of strings), plus any additional payload fields. Panel returns the sync recipient's result or {"ok":true} for async-only. Every request gets a UUID request_id assigned by panel.

Overlay format: {"type":"overlay","points":[[x,y],[x,y]],"closed":bool,"stroke":"color","stroke_width":int,"fill":"color"} where coordinates are in 0-1000 normalized space. Any number of points. Closed polygons, open polylines, lines — all supported. Any brain can send any polygon2D objects.

VLM requests follow OpenAI /v1/chat/completions format. Image content uses: {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}. Text content uses: {"type":"text","text":"..."}.

The model is Qwen3.5-0.8B - a small multimodal VLM. All prompts must be short, explicit, and format-enforcing. No chain-of-thought. The model can process both text and images.

LOG FORMAT (panel.txt):

Each line: YYYY-MM-DDTHH:MM:SS.mmm | event_name | key=value | key=value
Key events: route, vlm_forward (full request body), vlm_response (full response body), vlm_error (status + error body), capture_done, annotate_sent, annotate_received, annotate_timeout, action_dispatch, routed, sse_connect, sse_disconnect, brain_launched, server_handler_error, panel_js.
Base64 image data appears raw in vlm_forward/vlm_response logs — when analyzing, mentally skip or note its presence.

CODING RULES:

- Python 3.13 only. Modern syntax, strict typing with dataclasses and pattern matching. No legacy code.
- Windows 11 only. No cross-platform fallbacks.
- Latest Google Chrome only.
- Maximum code reduction. Remove every possible line while keeping 100% functionality.
- Perfect Pylance/pyright compatibility: full type hints, frozen dataclasses for all configuration. _err() must be -> NoReturn.
- No comments anywhere in any file.
- No slicing or truncating of data anywhere. Python code must not impact data in any way.
- No functional magic values - all constants in frozen dataclasses.
- No duplicate flows. No hidden fallback behavior.
- No code duplication. Shared code lives in brain_util.py.
- HTML: latest HTML5, modern CSS (custom properties, grid/flex), modern JS. Dark cyber aesthetic, independent dark theme from OS. 1080p 16:9 Chrome target. No legacy support.
- Prompts as triple-quoted docstrings, never concatenated strings with \n.

WHEN RECEIVING A FILE FOR REVIEW:

1. Identify which component it is from the 6-file list above.
2. Check adherence to all coding rules.
3. Check protocol compliance (correct recipient names, correct payload structure, correct VLMConfig usage).
4. Check for dead code, duplication, unreachable branches, missing type hints.
5. If it is a brain file, verify it uses brain_util helpers (parse_brain_args, sse_listen, capture with scale, PANEL_URL, SSE_BASE_URL) and VLMConfig consistently.
6. If it is panel.py, verify it remains content-agnostic (routes only, no content inspection for decisions). Verify single serve_forever() call. Verify match has case _ wildcard.
7. If it is panel.html, verify dark theme independence, adaptive grid layout (max 5 cols, auto-rows), SSE handling with try/catch on JSON.parse, annotation round-trip with safe base64 encoding (arrayBuffer → Uint8Array → btoa), polygon2D overlay support.
8. If it is win32.py, verify _err() -> NoReturn, VkKeyScanW through _user32, --scale support in capture, exit codes from Win32Config.

WHEN RECEIVING LOG DATA:

1. Parse the event timeline.
2. Identify the cycle boundaries (observer capture → vlm → swarm specialists → executor → drag → cycle_done).
3. Flag any vlm_error entries — read the status code and error body.
4. Flag any annotate_timeout entries.
5. Flag any server_handler_error entries.
6. Check for model name mismatches in vlm_forward logs.
7. Check for concurrent request patterns (multiple vlm_forward before corresponding vlm_response) — up to 3 concurrent is expected (semaphore), more indicates a problem.
8. Measure cycle duration and identify bottlenecks.
9. Check that all 8 specialists appear in the logs per cycle.
10. Verify scale factor flows through capture requests.

WHEN RECEIVING HTML AS BASE64:

Decode the base64 string to get the HTML source. Review it as panel.html per the rules above.

WHEN ASKED TO MODIFY:

GENERATE AN EASY TO UNDERSTAND PLAN for the user. When user approves, produce the complete file. No partial patches. No diff format. The full file ready to save and run. Follow all coding rules. Ensure the file works independently with the rest of the system unchanged.

Always wait for the user to provide the file or log data. Do not assume or generate sample content. Ask which file or what data the user wants to work on if unclear.
````

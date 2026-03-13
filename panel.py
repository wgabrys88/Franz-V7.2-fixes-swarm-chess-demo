import base64
import http.server
import json
import logging
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


SENTINEL: str = "NONE"

SYNC_RECIPIENTS: frozenset[str] = frozenset({"capture", "annotate", "vlm", "screen"})


@dataclass(frozen=True, slots=True)
class _Config:
    host: str = "127.0.0.1"
    port: int = 1236
    vlm_url: str = "http://127.0.0.1:1235/v1/chat/completions"
    annotate_timeout: float = 19.0
    vlm_timeout: float = 360.0
    sse_keepalive_interval: float = 70.0
    max_sse_queue_size: int = 256
    log_file: str = "panel.txt"
    stale_timeout: float = 30.0
    default_capture_width: int = 640
    default_capture_height: int = 640


CFG: _Config = _Config()
WIN32_PATH: Path = Path(__file__).resolve().parent / "win32.py"
PANEL_HTML: Path = Path(__file__).resolve().parent / "panel.html"
HERE: Path = Path(__file__).resolve().parent


class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not isinstance(record.msg, dict):
            ts: str = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            ms: int = int(time.time() * 1000) % 1000
            return f"{ts}.{ms:03d} | raw | msg={record.msg}"
        d: dict[str, Any] = dict(record.msg)
        event: str = d.pop("event", "unknown")
        t: float = d.pop("ts", time.time())
        iso: str = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
        ms_val: int = int(t * 1000) % 1000
        pairs: str = " | ".join(f"{k}={v}" for k, v in sorted(d.items()))
        if pairs:
            return f"{iso}.{ms_val:03d} | {event} | {pairs}"
        return f"{iso}.{ms_val:03d} | {event}"


_log_handler: logging.FileHandler = logging.FileHandler(HERE / CFG.log_file, encoding="utf-8")
_log_handler.setFormatter(_PlainFormatter())
_logger: logging.Logger = logging.getLogger("panel")
_logger.setLevel(logging.DEBUG)
_logger.addHandler(_log_handler)

_pending: dict[str, dict[str, Any]] = {}
_pending_lock: threading.Lock = threading.Lock()

_agent_sse_lock: threading.Lock = threading.Lock()
_agent_sse_queues: dict[str, list[queue.Queue[bytes | None]]] = {}

_startup_region: str = SENTINEL
_startup_scale: float = 1.0


def _log(event: str, **extra: Any) -> None:
    entry: dict[str, Any] = {"event": event, "ts": time.time()}
    entry.update(extra)
    _logger.debug(entry)


def _push_to_queues(
    queues: list[queue.Queue[bytes | None]],
    lock: threading.Lock,
    event: str,
    data: dict[str, Any],
) -> None:
    chunk: bytes = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
    with lock:
        dead: list[queue.Queue[bytes | None]] = []
        for q in queues:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                dead.append(q)
                _log("sse_queue_full")
        for q in dead:
            queues.remove(q)


def _agent_sse_push(agent: str, event: str, data: dict[str, Any]) -> None:
    with _agent_sse_lock:
        queues: list[queue.Queue[bytes | None]] = _agent_sse_queues.get(agent, [])
    if queues:
        _push_to_queues(queues, _agent_sse_lock, event, data)


def _win32(args: list[str], request_id: str, agent: str) -> None:
    cmd: list[str] = [sys.executable, str(WIN32_PATH)] + args
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        _log("win32_action_failed", request_id=request_id, agent=agent,
             args=args, returncode=proc.returncode,
             stderr=proc.stderr.decode(errors="replace"))


def _handle_capture(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    region: str = body.get("region", SENTINEL)
    capture_scale: float = body.get("capture_scale", 0.0)
    capture_size: list[int] = body.get("capture_size", [CFG.default_capture_width, CFG.default_capture_height])
    cmd: list[str] = [sys.executable, str(WIN32_PATH), "capture", "--region", region]
    if capture_scale > 0.0:
        cmd.extend(["--scale", str(capture_scale)])
    else:
        cmd.extend(["--width", str(capture_size[0]), "--height", str(capture_size[1])])
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        _log("capture_failed", request_id=rid, agent=agent,
             returncode=proc.returncode, stderr=proc.stderr.decode(errors="replace"))
        return {"error": f"capture failed: rc={proc.returncode}"}
    if not proc.stdout:
        _log("capture_empty", request_id=rid, agent=agent)
        return {"error": "capture returned empty"}
    image_b64: str = base64.b64encode(proc.stdout).decode("ascii")
    _log("capture_done", request_id=rid, agent=agent, size=len(image_b64))
    return {"image_b64": image_b64}


def _handle_annotate(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    image_b64: str = body.get("image_b64", SENTINEL)
    overlays: list[dict[str, Any]] = body.get("overlays", [])
    slot_ref: dict[str, Any] = {"event": threading.Event(), "result": SENTINEL, "ts": time.time()}
    with _pending_lock:
        _pending[rid] = slot_ref
    data: dict[str, Any] = {
        "request_id": rid,
        "agent": agent,
        "image_b64": image_b64,
        "overlays": overlays,
    }
    _agent_sse_push("ui", "annotate", data)
    _log("annotate_sent", request_id=rid, agent=agent,
         overlays=len(overlays), has_image=(image_b64 != SENTINEL))
    got_result: bool = slot_ref["event"].wait(timeout=CFG.annotate_timeout)
    if not got_result:
        _log("annotate_timeout", request_id=rid, agent=agent)
        with _pending_lock:
            _pending.pop(rid, None)
        return {"error": "annotate timeout"}
    _log("annotate_received", request_id=rid, agent=agent)
    return {"image_b64": slot_ref["result"]}


def _handle_vlm(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    vlm_request: dict[str, Any] = body.get("vlm_request", {})
    _log("vlm_forward", request_id=rid, agent=agent, body=vlm_request)
    fwd_body: bytes = json.dumps(vlm_request, separators=(",", ":")).encode()
    fwd_req: urllib.request.Request = urllib.request.Request(
        CFG.vlm_url, data=fwd_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(fwd_req, timeout=CFG.vlm_timeout) as resp:
            resp_bytes: bytes = resp.read()
        resp_obj: dict[str, Any] = json.loads(resp_bytes)
        _log("vlm_response", request_id=rid, agent=agent, body=resp_obj)
        return resp_obj
    except urllib.error.HTTPError as exc:
        error_body: str = ""
        try:
            error_body = exc.read().decode(errors="replace")
        except Exception:
            pass
        _log("vlm_error", request_id=rid, agent=agent,
             status=exc.code, body=error_body)
        return {"error": f"HTTP {exc.code}: {error_body}"}
    except Exception as exc:
        _log("vlm_error", request_id=rid, agent=agent, error=str(exc))
        return {"error": str(exc)}


def _handle_screen(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    actions: list[dict[str, Any]] = body.get("actions", [])
    region: str = body.get("region", SENTINEL)
    for act in actions:
        t: str = act.get("type", "")
        _log("action_dispatch", request_id=rid, agent=agent, action_type=t)
        match t:
            case "drag":
                _win32(["drag",
                        "--from_pos", f"{act['x1']},{act['y1']}",
                        "--to_pos", f"{act['x2']},{act['y2']}",
                        "--region", region], rid, agent)
            case "click":
                _win32(["click", "--pos", f"{act['x']},{act['y']}",
                        "--region", region], rid, agent)
            case "double_click":
                _win32(["double_click", "--pos", f"{act['x']},{act['y']}",
                        "--region", region], rid, agent)
            case "right_click":
                _win32(["right_click", "--pos", f"{act['x']},{act['y']}",
                        "--region", region], rid, agent)
            case "type_text":
                _win32(["type_text", "--text", act["text"]], rid, agent)
            case "press_key":
                _win32(["press_key", "--key", act["key"]], rid, agent)
            case "hotkey":
                _win32(["hotkey", "--keys", act["keys"]], rid, agent)
            case "scroll_up":
                _win32(["scroll_up", "--pos", f"{act['x']},{act['y']}",
                        "--region", region, "--clicks", str(act["clicks"])],
                       rid, agent)
            case "scroll_down":
                _win32(["scroll_down", "--pos", f"{act['x']},{act['y']}",
                        "--region", region, "--clicks", str(act["clicks"])],
                       rid, agent)
            case "cursor_pos":
                _win32(["cursor_pos", "--region", region], rid, agent)
    return {"ok": True}


def _handle_async_push(recipient: str, body: dict[str, Any], rid: str, agent: str) -> None:
    data: dict[str, Any] = dict(body)
    data["request_id"] = rid
    data["sender"] = agent
    _agent_sse_push(recipient, "message", data)
    _log("routed", request_id=rid, sender=agent, recipient=recipient)


def _select_region() -> str:
    proc: subprocess.CompletedProcess[bytes] = subprocess.run(
        [sys.executable, str(WIN32_PATH), "select_region"], capture_output=True,
    )
    if proc.returncode != 0:
        _log("select_region_failed", returncode=proc.returncode,
             stderr=proc.stderr.decode(errors="replace"))
        return SENTINEL
    return proc.stdout.decode().strip()


def _tandem_select() -> tuple[str, float]:
    print("Select capture region...")
    _log("select_region_prompt")
    region: str = _select_region()
    if region == SENTINEL:
        _log("select_region_empty")
        return SENTINEL, 1.0
    print(f"Region: {region}")
    _log("select_region_done", region=region)
    print("Select horizontal scale reference...")
    _log("select_scale_prompt")
    scale_region: str = _select_region()
    if scale_region == SENTINEL:
        _log("select_scale_empty")
        return region, 1.0
    parts: list[str] = scale_region.split(",")
    if len(parts) != 4:
        _log("select_scale_invalid", raw=scale_region)
        return region, 1.0
    x1: int = int(parts[0])
    x2: int = int(parts[2])
    scale: float = abs(x2 - x1) / 1000.0
    print(f"Scale: {scale:.4f}")
    _log("select_scale_done", scale=scale)
    return region, scale


class PanelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, code: int, data: dict[str, Any]) -> None:
        raw: bytes = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _parse_body(self, body: bytes) -> dict[str, Any] | None:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _log("json_parse_error", error=str(exc))
            self._json(400, {"error": "bad json"})
            return None

    def _serve_sse(self, q: queue.Queue[bytes | None], on_cleanup: Any) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        self.wfile.write(b"event: connected\ndata: {}\n\n")
        self.wfile.flush()
        _log("sse_connect", client=str(self.client_address))
        try:
            while True:
                try:
                    chunk: bytes | None = q.get(timeout=CFG.sse_keepalive_interval)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if chunk is None:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            on_cleanup()
            _log("sse_disconnect", client=str(self.client_address))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path: str = self.path.split("?")[0]
        if path == "/":
            raw: bytes = PANEL_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self._cors()
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/ready":
            self._json(200, {"ok": True, "region": _startup_region, "scale": _startup_scale})
        elif path == "/agent-events":
            params: dict[str, list[str]] = parse_qs(urlparse(self.path).query)
            agent_name: str = params.get("agent", [SENTINEL])[0]
            if agent_name == SENTINEL:
                self._json(400, {"error": "agent parameter required"})
                return
            q: queue.Queue[bytes | None] = queue.Queue(maxsize=CFG.max_sse_queue_size)
            with _agent_sse_lock:
                if agent_name not in _agent_sse_queues:
                    _agent_sse_queues[agent_name] = []
                _agent_sse_queues[agent_name].append(q)

            def cleanup() -> None:
                with _agent_sse_lock:
                    agent_list: list[queue.Queue[bytes | None]] = _agent_sse_queues.get(agent_name, [])
                    try:
                        agent_list.remove(q)
                    except ValueError:
                        pass

            self._serve_sse(q, cleanup)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path: str = self.path.split("?")[0]
        length: int = int(self.headers.get("Content-Length", 0))
        body: bytes = self.rfile.read(length) if length else b""

        if path == "/route":
            req: dict[str, Any] | None = self._parse_body(body)
            if req is None:
                return

            agent: str | None = req.get("agent")
            recipients: list[str] | None = req.get("recipients")
            if agent is None or recipients is None:
                self._json(400, {"error": "agent and recipients required"})
                return

            rid: str = str(uuid.uuid4())
            _log("route", request_id=rid, agent=agent, recipients=recipients)

            sync_targets: list[str] = [r for r in recipients if r in SYNC_RECIPIENTS]
            async_targets: list[str] = [r for r in recipients if r not in SYNC_RECIPIENTS]

            if len(sync_targets) > 1:
                self._json(400, {"error": "at most one sync recipient allowed"})
                return

            for target in async_targets:
                _handle_async_push(target, req, rid, agent)

            if not sync_targets:
                self._json(200, {"request_id": rid, "ok": True})
                return

            sync_target: str = sync_targets[0]
            result: dict[str, Any]
            match sync_target:
                case "capture":
                    result = _handle_capture(req, rid, agent)
                case "annotate":
                    result = _handle_annotate(req, rid, agent)
                case "vlm":
                    result = _handle_vlm(req, rid, agent)
                case "screen":
                    result = _handle_screen(req, rid, agent)
                case _:
                    result = {"error": f"unknown sync recipient: {sync_target}"}

            result["request_id"] = rid
            status: int = 200 if "error" not in result else 502
            self._json(status, result)

        elif path == "/result":
            data: dict[str, Any] | None = self._parse_body(body)
            if data is None:
                return
            rid_val: str = data.get("request_id", SENTINEL)
            annotated: str = data.get("image_b64", SENTINEL)
            with _pending_lock:
                slot: dict[str, Any] | None = _pending.pop(rid_val, None)
                stale: list[str] = [k for k, v in _pending.items() if time.time() - v["ts"] > CFG.stale_timeout]
                for k in stale:
                    _pending.pop(k)
            if slot:
                slot["result"] = annotated
                slot["event"].set()
                _log("result_received", request_id=rid_val, size=len(annotated))
                self._json(200, {"ok": True})
            else:
                _log("result_unknown_rid", request_id=rid_val)
                self._json(404, {"error": "unknown request_id"})

        elif path == "/panel-log":
            data = self._parse_body(body)
            if data is None:
                return
            _log("panel_js", **data)
            self._json(200, {"ok": True})

        else:
            self._json(404, {"error": "not found"})


def _handle_server_error(request: Any, client_address: Any) -> None:
    _log("server_handler_error", error=str(sys.exc_info()[1]),
         client=str(client_address))


def start(host: str = CFG.host, port: int = CFG.port) -> http.server.ThreadingHTTPServer:
    server: http.server.ThreadingHTTPServer = http.server.ThreadingHTTPServer((host, port), PanelHandler)
    server.handle_error = _handle_server_error
    _log("server_start", host=host, port=port)
    return server


if __name__ == "__main__":
    _startup_region, _startup_scale = _tandem_select()
    if _startup_region == SENTINEL:
        print("No region selected, exiting.")
        raise SystemExit(1)
    _log("startup", region=_startup_region, scale=_startup_scale)
    print(f"Region: {_startup_region}  Scale: {_startup_scale:.4f}")
    srv: http.server.ThreadingHTTPServer = start()
    print(f"Panel running on http://{CFG.host}:{CFG.port}")
    brain_procs: list[subprocess.Popen[bytes]] = []
    brain_files: list[str] = sys.argv[1:]
    if not brain_files:
        print("No brain files specified. Usage: python panel.py brain1.py brain2.py ...")
        print("Panel running with no brains.")
    for brain_file in brain_files:
        brain_path: Path = HERE / brain_file
        if not brain_path.exists():
            print(f"WARNING: brain file not found: {brain_file}")
            continue
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            [sys.executable, str(brain_path), "--region", _startup_region, "--scale", str(_startup_scale)],
        )
        _log("brain_launched", file=brain_file, pid=proc.pid)
        print(f"Launched {brain_file} pid={proc.pid}")
        brain_procs.append(proc)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for p in brain_procs:
            p.terminate()

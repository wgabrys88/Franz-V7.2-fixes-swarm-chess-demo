import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import brain_util as bu


@dataclass(frozen=True, slots=True)
class ObserverConfig:
    panel_url: str = bu.PANEL_URL
    sse_url: str = f"{bu.SSE_BASE_URL}?agent=observer"
    agent: str = "observer"
    region: str = bu.SENTINEL
    scale: float = 1.0
    grid_size: int = 8
    grid_color: str = "rgba(0,255,200,0.95)"
    grid_stroke_width: int = 4
    swarm_agent: str = "swarm"
    startup_delay: float = 1.0
    error_retry_delay: float = 3.0


OBSERVER_VLM: bu.VLMConfig = bu.VLMConfig(max_tokens=500)

SYSTEM_PROMPT: str = """\
Chess commentator. White bottom, Black top.
Columns a-h left-right. Rows 1-8 bottom-top.
1. Describe position in 2 sentences: active pieces, threats, checks, pins.
2. List 2-4 candidate White moves as: e2 e4 (from to). Brief reason each.
3. Flag any checkmate, capture, or fork clearly."""

USER_PROMPT: str = "Current position and best moves for White?"


def _run_cycle(cfg: ObserverConfig, grid_overlays: list[dict[str, Any]]) -> None:
    bu.ui_pending(cfg.panel_url, cfg.agent, status=OBSERVER_VLM.model)

    raw_b64: str = bu.capture(
        cfg.panel_url, cfg.agent, cfg.region,
        scale=cfg.scale,
    )
    if raw_b64 == bu.SENTINEL:
        bu.ui_error(cfg.panel_url, cfg.agent, text="capture failed")
        return

    annotated_b64: str = bu.annotate(
        cfg.panel_url, cfg.agent, raw_b64, grid_overlays,
    )
    if annotated_b64 == bu.SENTINEL:
        annotated_b64 = raw_b64

    vlm_request: dict[str, Any] = bu.make_vlm_request_with_image(
        OBSERVER_VLM, SYSTEM_PROMPT, annotated_b64, USER_PROMPT,
    )

    text: str = bu.vlm_text(cfg.panel_url, cfg.agent, vlm_request)
    print(f"observer vlm response: {text}")

    bu.ui_done(
        cfg.panel_url, cfg.agent,
        text=text, image_b64=annotated_b64, status=OBSERVER_VLM.model,
    )

    bu.push(
        cfg.panel_url, cfg.agent, [cfg.swarm_agent],
        text=text, image_b64=annotated_b64,
    )
    print("observer: pushed to swarm")


def main() -> None:
    region, scale = bu.parse_brain_args(sys.argv[1:])
    cfg: ObserverConfig = ObserverConfig(region=region, scale=scale)
    print(f"observer started region={cfg.region} scale={cfg.scale}")
    grid_overlays: list[dict[str, Any]] = bu.make_grid_overlays(
        cfg.grid_size, cfg.grid_color, cfg.grid_stroke_width,
    )

    cycle_done: threading.Event = threading.Event()

    def on_sse_event(event_name: str, data: dict[str, Any]) -> None:
        if event_name == "message" and data.get("event_type") == "cycle_done":
            cycle_done.set()

    bu.sse_listen(cfg.sse_url, on_sse_event)
    time.sleep(cfg.startup_delay)

    while True:
        cycle_done.clear()
        try:
            _run_cycle(cfg, grid_overlays)
        except Exception as e:
            print(f"observer error: {e}")
            bu.ui_error(cfg.panel_url, cfg.agent, text=f"ERROR: {e}")
            time.sleep(cfg.error_retry_delay)
            continue

        print("observer waiting for cycle_done...")
        cycle_done.wait()
        print("observer cycle_done received, starting next cycle")


if __name__ == "__main__":
    main()

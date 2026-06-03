"""
watcher.py — the capture loop.

Every tick: grab the active window, screenshot, perceptual-hash to skip
unchanged screens, OCR the text, redact secrets, store it. OCR runs in a
background thread so a slow frame never stalls the loop. Nothing here touches
the network and no image is ever written to disk past the temp file the OCR
reads — that file is deleted in `capture_frame`'s `finally`.
"""

from __future__ import annotations

import time
import signal
import logging

from engram.capture import AsyncCapture, get_active_window_info
from engram.store import get_store, load_config, is_app_ignored

log = logging.getLogger("engram.watcher")


def watch(interval: float | None = None, scale: float = 0.5,
          fast_ocr: bool = True, db_path: str | None = None) -> None:
    """
    Run the capture loop until interrupted.

    interval   seconds between ticks (default: config `capture_interval`, 10s)
    scale      screenshot downscale before OCR — lower is faster on CPU
    fast_ocr   macOS Vision Fast level (~3x faster than Accurate)
    """
    config = load_config()
    interval = interval if interval is not None else config.get("capture_interval", 10)
    store = get_store(db_path)
    cap = AsyncCapture()
    prev_phash = ""
    running = {"on": True}

    def _stop(*_):
        running["on"] = False
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("engram watching — every %ss, scale=%s, fast_ocr=%s", interval, scale, fast_ocr)
    print(f"engram: watching your screen every {interval}s. Ctrl-C to stop.")

    captured = 0
    try:
        while running["on"]:
            app_name, window_title = get_active_window_info()

            # Respect the ignore list (password managers, etc.) — never even capture.
            if app_name and is_app_ignored(app_name, config):
                time.sleep(interval)
                continue

            cap.submit(prev_phash=prev_phash, window_info=(app_name, window_title),
                       scale=scale, fast_ocr=fast_ocr)

            # Drain any frame that finished since last tick.
            frame = cap.get_result()
            if frame is not None:
                prev_phash = frame.phash or prev_phash
                store.insert(
                    timestamp=frame.timestamp,
                    app_name=frame.app_name,
                    window_title=frame.window_title,
                    text=frame.text,
                    extra_context=frame.extra_context,
                    phash=frame.phash,
                )
                captured += 1
                if captured % 10 == 0:
                    log.info("captured %d frames", captured)

            # Reload config periodically so edits take effect without a restart.
            config = load_config()
            interval = interval if interval is not None else config.get("capture_interval", 10)
            time.sleep(interval)
    finally:
        cap.shutdown()
        print(f"\nengram: stopped. {captured} frames captured this session.")

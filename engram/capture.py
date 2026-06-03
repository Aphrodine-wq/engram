"""
capture.py — Screenshot capture + local OCR, optimized for Intel Macs.

Key Intel optimizations:
  - VNRequestTextRecognitionLevelFast (CPU-friendly, ~3x faster than Accurate)
  - Screenshots downscaled via sips before OCR (less pixels = faster)
  - JPEG instead of PNG (faster write/read)
  - Perceptual hash on tiny thumbnail (instant)
  - Threaded OCR so watcher loop isn't blocked
"""

from __future__ import annotations

import subprocess
import tempfile
import os
import time
import logging
from dataclasses import dataclass
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, Future

log = logging.getLogger("engram.capture")


@dataclass
class ScreenFrame:
    """A single parsed screen capture."""
    timestamp: float
    text: str
    app_name: str = ""
    window_title: str = ""
    phash: str = ""
    extra_context: str = ""


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------

def capture_screenshot(path: Optional[str] = None, scale: float = 0.5) -> str:
    """
    Take a screenshot using macOS screencapture.
    Uses JPEG (faster I/O than PNG) and downscales via sips
    to reduce OCR processing time on Intel CPUs.
    """
    if path is None:
        fd, path = tempfile.mkstemp(suffix=".jpg", prefix="ceyes_")
        os.close(fd)

    # -x = no sound, -t jpg = JPEG format (faster than PNG)
    subprocess.run(
        ["screencapture", "-x", "-t", "jpg", path],
        check=True,
        capture_output=True,
    )

    # Downscale to reduce OCR workload on Intel CPU
    if scale < 1.0:
        try:
            _downscale_sips(path, scale)
        except Exception:
            pass  # if sips fails, just OCR at full res

    return path


def _downscale_sips(path: str, scale: float):
    """
    Use macOS built-in `sips` to resize — no Python image libs needed.
    Fast because sips uses CoreGraphics natively.
    """
    result = subprocess.run(
        ["sips", "-g", "pixelWidth", path],
        capture_output=True, text=True, timeout=3
    )
    for line in result.stdout.splitlines():
        if "pixelWidth" in line:
            width = int(line.split(":")[-1].strip())
            new_width = int(width * scale)
            subprocess.run(
                ["sips", "--resampleWidth", str(new_width), path, "--out", path],
                capture_output=True, timeout=5
            )
            break


# ---------------------------------------------------------------------------
# Window info
# ---------------------------------------------------------------------------

def get_active_window_info() -> tuple[str, str]:
    """Get the active app name and window title via AppleScript."""
    script = '''
    tell application "System Events"
        set frontApp to name of first application process whose frontmost is true
        set frontWindow to ""
        try
            set frontWindow to name of front window of (first application process whose frontmost is true)
        end try
        return frontApp & "|||" & frontWindow
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3
        )
        parts = result.stdout.strip().split("|||")
        return (parts[0] if len(parts) > 0 else "", parts[1] if len(parts) > 1 else "")
    except Exception:
        return ("", "")


# ---------------------------------------------------------------------------
# OCR — tiered approach for Intel
# ---------------------------------------------------------------------------

def ocr_image(image_path: str, fast: bool = True) -> str:
    """
    OCR dispatcher. Tries in order:
      1. macOS Vision framework (Fast or Accurate level)
      2. tesseract fallback
    """
    text = _ocr_vision(image_path, fast=fast)
    if text:
        return text
    return _ocr_tesseract(image_path)


def _ocr_vision(image_path: str, fast: bool = True) -> str:
    """
    macOS Vision framework OCR.
    On Intel: Fast level (~0.5-1s) instead of Accurate (~2-4s).
    Fast level still gets 90%+ of text right for screen content.
    """
    try:
        import Vision
        import Quartz
        from Foundation import NSURL

        image_url = NSURL.fileURLWithPath_(image_path)
        image_source = Quartz.CGImageSourceCreateWithURL(image_url, None)
        if image_source is None:
            return ""

        cg_image = Quartz.CGImageSourceCreateImageAtIndex(image_source, 0, None)
        if cg_image is None:
            return ""

        request = Vision.VNRecognizeTextRequest.alloc().init()

        # KEY INTEL OPTIMIZATION: Fast mode is ~3x faster on CPU
        if fast:
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
        else:
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)

        # Language correction adds latency — skip it in fast mode
        request.setUsesLanguageCorrection_(not fast)

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            cg_image, None
        )
        success = handler.performRequests_error_([request], None)

        if not success[0]:
            return ""

        results = request.results()
        if not results:
            return ""

        lines = []
        for observation in results:
            candidate = observation.topCandidates_(1)
            if candidate:
                lines.append(candidate[0].string())

        return "\n".join(lines)

    except ImportError:
        return ""
    except Exception:
        return ""


def _ocr_tesseract(image_path: str) -> str:
    """Fallback OCR using tesseract. Install: brew install tesseract"""
    try:
        result = subprocess.run(
            ["tesseract", image_path, "stdout", "-l", "eng",
             "--psm", "3",          # auto page segmentation
             "--oem", "1"],          # LSTM engine
            capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip()
    except FileNotFoundError:
        return "[OCR unavailable — install pyobjc-framework-Vision or: brew install tesseract]"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Perceptual hashing (change detection)
# ---------------------------------------------------------------------------

def compute_phash(image_path: str) -> str:
    """
    Perceptual hash on a tiny thumbnail — effectively instant.
    hash_size=8 (vs 12) for speed on Intel. Still good enough for dedup.
    """
    try:
        from PIL import Image
        import imagehash
        img = Image.open(image_path)
        img.thumbnail((128, 128))
        return str(imagehash.phash(img, hash_size=8))
    except Exception:
        return ""


def has_changed(current_phash: str, prev_phash: str, threshold: int = 6) -> bool:
    """Check if screen has changed enough to warrant OCR."""
    if not prev_phash or not current_phash:
        return True
    try:
        import imagehash
        distance = imagehash.hex_to_hash(current_phash) - imagehash.hex_to_hash(prev_phash)
        return distance >= threshold
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Main capture pipelines
# ---------------------------------------------------------------------------

def capture_frame(prev_phash: str = "", similarity_threshold: int = 6,
                  scale: float = 0.5, fast_ocr: bool = True,
                  window_info: Optional[tuple[str, str]] = None) -> Optional[ScreenFrame]:
    """
    Full pipeline: screenshot → downscale → diff check → OCR → cleanup.
    Returns None if screen hasn't changed enough.

    Intel-tuned defaults:
      - scale=0.5 (half resolution — less work for CPU OCR)
      - fast_ocr=True (VNRequestTextRecognitionLevelFast)
      - similarity_threshold=6 (tuned for smaller hash size)

    Pass window_info=(app_name, window_title) to skip redundant AppleScript call.
    """
    path = None
    try:
        path = capture_screenshot(scale=scale)

        # Quick diff check BEFORE expensive OCR
        current_phash = compute_phash(path)
        if not has_changed(current_phash, prev_phash, similarity_threshold):
            return None

        # OCR
        text = ocr_image(path, fast=fast_ocr)
        if not text.strip():
            return None

        # OCR cleanup — fix common Vision framework errors on Intel Macs
        try:
            from engram.ocr_cleanup import OCRCleaner
            _cleaner = OCRCleaner()
            text = _cleaner.clean(text)
        except ImportError:
            pass

        # Privacy filter — redact sensitive content before it leaves this function
        try:
            from engram.privacy import redact
            result = redact(text)
            text = result.text
            if result.redacted_count:
                log.info("Redacted %d sensitive pattern(s): %s",
                         result.redacted_count, ", ".join(result.redacted_types))
        except ImportError:
            pass  # privacy module not available, proceed unfiltered

        # Window context — reuse if already fetched by caller
        if window_info:
            app_name, window_title = window_info
        else:
            app_name, window_title = get_active_window_info()

        return ScreenFrame(
            timestamp=time.time(),
            text=text,
            app_name=app_name,
            window_title=window_title,
            phash=current_phash,
        )

    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def capture_frame_with_vision(api_key: str, prev_phash: str = "",
                               similarity_threshold: int = 6,
                               scale: float = 0.5) -> Optional[ScreenFrame]:
    """
    Enhanced pipeline: local OCR + Claude Vision API for semantic context.
    Sends a smaller JPEG to the API to reduce upload time + token cost.
    """
    import base64

    path = None
    try:
        path = capture_screenshot(scale=0.6)

        current_phash = compute_phash(path)
        if not has_changed(current_phash, prev_phash, similarity_threshold):
            return None

        # Local OCR (fast mode)
        text = ocr_image(path, fast=True)

        # Privacy filter
        try:
            from engram.privacy import redact
            result = redact(text)
            text = result.text
        except ImportError:
            pass

        # Claude Vision for richer understanding
        extra_context = ""
        try:
            import anthropic
            with open(path, "rb") as f:
                image_data = base64.standard_b64encode(f.read()).decode("utf-8")

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data},
                        },
                        {
                            "type": "text",
                            "text": "In 2-3 concise sentences, describe what the user is doing on their screen. Focus on: which app, what task, key visible content. Be specific.",
                        },
                    ],
                }],
            )
            extra_context = response.content[0].text
        except Exception as e:
            extra_context = f"[Vision API error: {e}]"

        app_name, window_title = get_active_window_info()

        return ScreenFrame(
            timestamp=time.time(),
            text=text,
            app_name=app_name,
            window_title=window_title,
            phash=current_phash,
            extra_context=extra_context,
        )

    finally:
        if path and os.path.exists(path):
            os.unlink(path)


# ---------------------------------------------------------------------------
# Threaded capture (non-blocking for watcher loop)
# ---------------------------------------------------------------------------

class AsyncCapture:
    """
    Runs OCR in a background thread so the watcher loop isn't blocked.
    On Intel, OCR can take 1-3s — this prevents missed intervals.
    """

    def __init__(self, max_workers: int = 1):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._pending: Optional[Future] = None

    def submit(self, prev_phash: str = "", window_info: Optional[tuple[str, str]] = None, **kwargs) -> None:
        """Submit a capture job. Skips if previous job still running."""
        if self._pending and not self._pending.done():
            return  # previous OCR still running, skip this tick
        self._pending = self._executor.submit(
            capture_frame, prev_phash, window_info=window_info, **kwargs
        )

    def get_result(self) -> Optional[ScreenFrame]:
        """Get result if ready, None if still processing or no result."""
        if self._pending and self._pending.done():
            try:
                result = self._pending.result()
                self._pending = None
                return result
            except Exception:
                self._pending = None
                return None
        return None

    def shutdown(self):
        self._executor.shutdown(wait=False)

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
import sys
import shutil
import time
import logging
from dataclasses import dataclass
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, Future

log = logging.getLogger("engram.capture")

# Platform detection. The capture/window/OCR backends differ per-OS; everything
# downstream of ScreenFrame (store, search, REST, MCP) is platform-agnostic.
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")


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
    Take a screenshot to a temp JPEG and downscale it to reduce OCR workload.

    Backend is per-OS: macOS uses the native `screencapture`/`sips` tools;
    everything else (Windows, Linux) uses Pillow's ImageGrab. JPEG is used for
    faster I/O than PNG. The file is deleted by the caller in `capture_frame`.
    """
    if path is None:
        fd, path = tempfile.mkstemp(suffix=".jpg", prefix="ceyes_")
        os.close(fd)

    if IS_MACOS:
        # -x = no sound, -t jpg = JPEG format (faster than PNG)
        subprocess.run(
            ["screencapture", "-x", "-t", "jpg", path],
            check=True,
            capture_output=True,
        )
    else:
        _capture_screenshot_pillow(path)

    # Downscale to reduce OCR workload on CPU
    if scale < 1.0:
        try:
            _downscale(path, scale)
        except Exception:
            pass  # if resize fails, just OCR at full res

    return path


def _capture_screenshot_pillow(path: str):
    """
    Cross-platform screenshot via Pillow.

    On Windows we grab only the **focused window's** rectangle when we can find
    it — this crops out the taskbar, desktop icons, and other monitors, which
    dramatically reduces OCR noise (the whole reason a desktop capture reads as
    garbage). Falls back to the full virtual desktop if the foreground window
    can't be resolved or isn't usable.
    """
    from PIL import ImageGrab

    bbox = _foreground_window_bbox() if IS_WINDOWS else None
    try:
        if bbox:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        else:
            img = ImageGrab.grab(all_screens=True)  # span all monitors
    except TypeError:
        img = ImageGrab.grab()  # older Pillow / non-Windows without the kwarg
    img.convert("RGB").save(path, "JPEG", quality=85)


def _foreground_window_bbox():
    """
    (left, top, right, bottom) of the focused window in virtual-screen
    coordinates, or None if it can't be resolved.

    Uses the DWM *extended frame bounds* (which exclude the invisible resize
    border / drop shadow) and falls back to GetWindowRect. Returns None for a
    minimized window or an implausibly small rect, so the caller drops back to
    a full-screen grab.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
        ]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

        hwnd = user32.GetForegroundWindow()
        if not hwnd or user32.IsIconic(hwnd):
            return None

        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        rect = wintypes.RECT()
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        if hr != 0:  # not S_OK — fall back to the plain window rect
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None

        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        if (right - left) < 200 or (bottom - top) < 150:
            return None  # too small to be a useful content window
        return (left, top, right, bottom)
    except Exception:
        return None


def _downscale(path: str, scale: float):
    """Resize the screenshot in place, picking the fastest backend per-OS."""
    if IS_MACOS:
        _downscale_sips(path, scale)
    else:
        _downscale_pillow(path, scale)


def _downscale_pillow(path: str, scale: float):
    """Resize via Pillow — used on Windows/Linux where `sips` doesn't exist."""
    from PIL import Image
    img = Image.open(path)
    width, height = img.size
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    img.convert("RGB").resize(new_size, Image.LANCZOS).save(path, "JPEG", quality=80)


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
    """Get the active app name and window title for the current OS."""
    if IS_MACOS:
        return _active_window_macos()
    if IS_WINDOWS:
        return _active_window_windows()
    return ("", "")


def _active_window_macos() -> tuple[str, str]:
    """Active app name and window title via AppleScript."""
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


def _active_window_windows() -> tuple[str, str]:
    """
    Active process name + window title via the Win32 API (ctypes, no deps).

    app_name is the foreground process's executable basename without `.exe`
    (e.g. "chrome", "Code"); window_title is its title-bar text. Both empty on
    failure. restype/argtypes are set explicitly so HANDLE/HWND aren't
    truncated on 64-bit.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ("", "")

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app_name = _process_name_windows(pid.value)
        return (app_name, title)
    except Exception:
        return ("", "")


def _process_name_windows(pid: int) -> str:
    """Executable basename (sans `.exe`) for a PID via QueryFullProcessImageNameW."""
    if not pid:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(260)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                base = os.path.basename(buf.value)
                if base.lower().endswith(".exe"):
                    base = base[:-4]
                return base
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


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


def _find_tesseract() -> Optional[str]:
    """Locate the tesseract binary: PATH first, then common install dirs."""
    exe = shutil.which("tesseract")
    if exe:
        return exe
    candidates = [
        os.environ.get("TESSERACT_CMD", ""),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _preprocess_for_ocr(image_path: str) -> Optional[str]:
    """
    Produce a temp image tuned for Tesseract and return its path (caller deletes).

    Tesseract is trained on ~300-DPI black-on-white text; raw screenshots are
    low-DPI and full-color, which is why UI text reads as garbage. We convert to
    grayscale, upscale small captures so glyphs are big enough to recognize,
    autocontrast, and lightly sharpen. Returns None if Pillow is unavailable, so
    the caller can OCR the original.
    """
    try:
        from PIL import Image, ImageOps, ImageFilter
    except Exception:
        return None
    try:
        img = Image.open(image_path).convert("L")  # grayscale
        w, h = img.size
        target_w = 1600  # upscale narrow/window captures; leave big ones alone
        if 0 < w < target_w:
            factor = min(3.0, target_w / w)
            img = img.resize((int(w * factor), int(h * factor)), Image.LANCZOS)
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.SHARPEN)
        fd, out = tempfile.mkstemp(suffix=".png", prefix="ceyes_ocr_")
        os.close(fd)
        img.save(out, "PNG")
        return out
    except Exception:
        return None


def _ocr_tesseract(image_path: str) -> str:
    """
    Fallback OCR using tesseract. This is the primary OCR path on Windows/Linux.
    Install: Windows `winget install UB-Mannheim.TesseractOCR`, macOS `brew install tesseract`.

    Off macOS the image is preprocessed (grayscale/upscale/contrast) first, which
    markedly improves accuracy on screen captures.
    """
    exe = _find_tesseract()
    if not exe:
        return ("[OCR unavailable — install pyobjc-framework-Vision (macOS) or "
                "tesseract (winget install UB-Mannheim.TesseractOCR / brew install tesseract)]")

    ocr_path, tmp = image_path, None
    if not IS_MACOS:
        tmp = _preprocess_for_ocr(image_path)
        if tmp:
            ocr_path = tmp
    try:
        result = subprocess.run(
            [exe, ocr_path, "stdout", "-l", "eng",
             "--psm", "3",          # auto page segmentation
             "--oem", "1"],          # LSTM engine
            capture_output=True, text=True, timeout=20
        )
        return result.stdout.strip()
    except Exception:
        return ""
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


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

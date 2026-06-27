"""
audio.py — Microphone capture + local speech-to-text, mirroring capture.py.

The audio path is the screen path with two boxes swapped:

    record mic ──▶ RMS/VAD gate ──▶ Whisper STT ──▶ cleanup ──▶ redact ──▶ store
    (16k mono       (skip silence,    (faster-whisper  (reuse     (privacy)  (frames,
     float32,        the audio analog   local, on CPU)   OCR                  source=
     in memory)      of phash dedup)                     cleaner)             'audio')

Raw audio is NEVER written to disk — not even a temp file. Samples live in an
in-memory numpy buffer, get transcribed, then dropped. Only redacted text is
stored. That's a strictly stronger privacy stance than the screen path, which
writes a temp JPEG for the OCR engine to read off disk.

This module is opt-in: `engram listen` is a separate command from `engram
watch`, and screen capture never records audio. Recording people speaking
carries real legal weight (two-party-consent states), so it's a deliberate,
separate action — off by default and announced loudly when it starts.

Requires the optional [audio] extra:  pip install 'engram-memory[audio]'
"""

from __future__ import annotations

import time
import signal
import logging
from dataclasses import dataclass
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, Future

log = logging.getLogger("engram.audio")

SAMPLE_RATE = 16000              # Whisper expects 16 kHz mono
CHANNELS = 1
DEFAULT_SEGMENT_SECONDS = 15.0
DEFAULT_MODEL = "base"
# RMS energy below this is treated as silence and never transcribed — the audio
# analog of skipping an unchanged screen via perceptual hash. Spends CPU only
# when someone is actually talking.
SILENCE_RMS = 0.006


@dataclass
class AudioSegment:
    """A single transcribed audio segment. Mirror of capture.ScreenFrame."""
    timestamp: float
    text: str
    source: str = "audio"
    app_name: str = "mic"        # capture-source label ("mic", later "zoom", etc.)
    window_title: str = ""        # speaker/channel, when known
    duration: float = 0.0


def _missing_dep(name: str) -> ImportError:
    return ImportError(
        f"engram audio needs the optional '[audio]' extra ({name} not installed). "
        f"Install it with:  pip install 'engram-memory[audio]'"
    )


# ---------------------------------------------------------------------------
# Recording — in-memory only, raw audio never touches disk
# ---------------------------------------------------------------------------

def record_segment(seconds: float = DEFAULT_SEGMENT_SECONDS,
                   sample_rate: int = SAMPLE_RATE, device=None):
    """
    Record `seconds` of mono audio from the mic into an in-memory float32 numpy
    array and return it. Nothing is written to disk — the buffer is the only
    copy and the caller drops it after transcription.
    """
    try:
        import sounddevice as sd
    except ImportError:
        raise _missing_dep("sounddevice")
    frames = sd.rec(int(seconds * sample_rate), samplerate=sample_rate,
                    channels=CHANNELS, dtype="float32", device=device)
    sd.wait()
    return frames.reshape(-1)


def rms(samples) -> float:
    """Root-mean-square energy of a float32 sample buffer."""
    import numpy as np
    if samples is None or len(samples) == 0:
        return 0.0
    arr = np.asarray(samples, dtype="float64")
    return float(np.sqrt(np.mean(arr * arr)))


def is_silent(samples, threshold: float = SILENCE_RMS) -> bool:
    """True if the segment is below the speech-energy floor — skip transcription."""
    return rms(samples) < threshold


# ---------------------------------------------------------------------------
# Transcription — faster-whisper, local, CPU-friendly
# ---------------------------------------------------------------------------

_model = None
_model_name: Optional[str] = None


def _get_model(name: str = DEFAULT_MODEL):
    """Lazy-load and cache a faster-whisper model (like OCR's cleaner singleton).
    First call downloads the model weights (~75MB for 'base')."""
    global _model, _model_name
    if _model is not None and _model_name == name:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise _missing_dep("faster-whisper")
    # int8 on CPU keeps it light enough to run beside the screen watcher.
    _model = WhisperModel(name, device="cpu", compute_type="int8")
    _model_name = name
    return _model


def transcribe(samples, model: str = DEFAULT_MODEL) -> str:
    """
    Transcribe an in-memory 16 kHz float32 buffer to text. The VAD filter drops
    non-speech regions inside the segment before decoding.
    """
    m = _get_model(model)
    segments, _info = m.transcribe(samples, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def _clean_and_redact(text: str) -> str:
    """OCR cleanup + privacy redaction — the exact tail of the screen pipeline."""
    try:
        from engram.ocr_cleanup import OCRCleaner
        text = OCRCleaner().clean(text)
    except ImportError:
        pass
    try:
        from engram.privacy import redact
        result = redact(text)
        text = result.text
        if result.redacted_count:
            log.info("Redacted %d sensitive pattern(s) from audio: %s",
                     result.redacted_count, ", ".join(result.redacted_types))
    except ImportError:
        pass
    return text


def capture_audio_segment(seconds: float = DEFAULT_SEGMENT_SECONDS,
                          model: str = DEFAULT_MODEL, device=None,
                          app_name: str = "mic") -> Optional[AudioSegment]:
    """
    One-shot pipeline: record → silence-gate → transcribe → cleanup → redact.
    Returns None for silent or empty segments. Mirror of capture.capture_frame().
    """
    samples = record_segment(seconds, device=device)
    if is_silent(samples):
        return None
    text = transcribe(samples, model=model)
    del samples  # drop the raw audio the instant we have text
    if not text.strip():
        return None
    return AudioSegment(timestamp=time.time(), text=_clean_and_redact(text),
                        app_name=app_name, duration=seconds)


# ---------------------------------------------------------------------------
# Threaded transcription (non-blocking for the listen loop)
# ---------------------------------------------------------------------------

class AsyncTranscribe:
    """
    Transcribe segments in a background thread so recording the next segment
    overlaps STT of the previous one. Mirror of capture.AsyncCapture — except
    recording itself is synchronous (it *is* the time window), so only the
    heavy STT step runs in the worker.
    """

    def __init__(self, max_workers: int = 1):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._pending: Optional[Future] = None

    def submit(self, samples, model: str = DEFAULT_MODEL, app_name: str = "mic") -> None:
        """Submit a transcription job. Skips if the previous one is still running."""
        if self._pending and not self._pending.done():
            return
        self._pending = self._executor.submit(self._run, samples, model, app_name)

    @staticmethod
    def _run(samples, model: str, app_name: str) -> Optional[AudioSegment]:
        text = transcribe(samples, model=model)
        if not text.strip():
            return None
        return AudioSegment(timestamp=time.time(), text=_clean_and_redact(text),
                            app_name=app_name)

    def get_result(self) -> Optional[AudioSegment]:
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


# ---------------------------------------------------------------------------
# The listen loop — opt-in mirror of watcher.watch()
# ---------------------------------------------------------------------------

def audio_watch(seconds: float = DEFAULT_SEGMENT_SECONDS, model: str = DEFAULT_MODEL,
                device=None, app_name: str = "mic", db_path: Optional[str] = None) -> None:
    """
    Opt-in microphone capture loop. Records short segments, skips silence,
    transcribes locally, redacts, and stores transcripts in the SAME frames
    table as screen captures (source='audio') so one search spans both.

    Off by default and separate from `engram watch`: running this command is the
    explicit consent action. Raw audio never touches disk.
    """
    from engram.store import get_store

    store = get_store(db_path)
    worker = AsyncTranscribe()
    running = {"on": True}

    def _stop(*_):
        running["on"] = False
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(f"engram: LISTENING to your microphone in {seconds:.0f}s segments. Ctrl-C to stop.")
    print("        Raw audio is never saved — only redacted transcript text.")
    print("        Make sure everyone in the room knows recording is on.")
    log.info("engram listening — segment=%ss, model=%s", seconds, model)

    stored = 0
    try:
        while running["on"]:
            # Drain a finished transcription before recording the next segment.
            seg = worker.get_result()
            if seg is not None and seg.text.strip():
                store.insert(
                    timestamp=seg.timestamp,
                    app_name=seg.app_name,
                    window_title=seg.window_title,
                    text=seg.text,
                    source="audio",
                )
                stored += 1
                if stored % 5 == 0:
                    log.info("stored %d audio segments", stored)

            samples = record_segment(seconds, device=device)
            if is_silent(samples):
                continue  # silence — the audio analog of an unchanged screen
            worker.submit(samples, model=model, app_name=app_name)
    finally:
        worker.shutdown()
        print(f"\nengram: stopped listening. {stored} audio segments stored this session.")

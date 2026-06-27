"""
Tests for the audio path and the unified seen-and-heard timeline.

These run WITHOUT the optional [audio] extra installed: the silence-gate test
skips if numpy is missing, and the store test needs no audio deps at all. No
microphone and no model download are involved.
"""

import time

import pytest

from engram.store import EyesStore


def test_silence_gate_skips_quiet_audio():
    """RMS gate is the audio analog of phash dedup — silence must be skipped."""
    np = pytest.importorskip("numpy")
    from engram.audio import is_silent, rms

    silence = np.zeros(SAMPLE := 16000, dtype="float32")
    assert is_silent(silence)

    t = np.linspace(0, 1, SAMPLE, dtype="float32")
    speech_like = (0.5 * np.sin(2 * np.pi * 220 * t)).astype("float32")
    assert not is_silent(speech_like)
    assert rms(speech_like) > rms(silence)


def test_audio_and_screen_share_one_searchable_timeline(tmp_path):
    """Audio transcripts land in the same frames table and the same FTS index,
    tagged source='audio', so one search spans what you saw and what you heard."""
    store = EyesStore(db_path=str(tmp_path / "t.db"))
    now = time.time()

    store.insert(timestamp=now - 5, app_name="Safari", window_title="docs",
                 text="postgres connection pool tuning notes", source="screen")
    store.insert(timestamp=now - 2, app_name="zoom", window_title="standup",
                 text="we agreed to ship the audio feature on friday", source="audio")

    recent = store.get_recent(minutes=60, limit=10)
    assert {e.source for e in recent} == {"screen", "audio"}

    hits = store.search("audio feature")
    assert hits, "FTS5 should find the audio transcript"
    assert any(h.source == "audio" for h in hits)


def test_existing_inserts_default_to_screen(tmp_path):
    """Back-compat: callers that don't pass source still work and read as 'screen'."""
    store = EyesStore(db_path=str(tmp_path / "t.db"))
    store.insert(timestamp=time.time(), app_name="Terminal",
                 window_title="zsh", text="git push origin main")
    latest = store.get_latest()
    assert latest is not None
    assert latest.source == "screen"

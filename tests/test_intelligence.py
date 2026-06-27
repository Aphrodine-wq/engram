"""
Tests for the intelligence layer — ask (hybrid retrieval over the unified
seen+heard timeline) and connections (the entity graph). Pure-Python, no audio
deps, no model, no network.
"""

import time

import pytest

from engram.store import EyesStore
from engram.intelligence import ask, connections, extract_entities, hybrid_search


def _seed(store):
    now = time.time()
    rows = [
        (now - 600, "Safari", "FTW pricing doc", "contractor payout fee structure and pricing tiers", "screen"),
        (now - 400, "zoom",   "standup with Josh", "Josh said we should ship the audio feature on friday", "audio"),
        (now - 300, "Cursor", "engram/audio.py", "record segment then transcribe with faster whisper", "screen"),
        (now - 200, "zoom",   "call with Josh",  "Josh wants the invoice.pdf sent for the FTW work", "audio"),
        (now - 100, "Mail",   "inbox",           "unrelated newsletter about gardening tips", "screen"),
    ]
    for ts, app, win, text, src in rows:
        store.insert(timestamp=ts, app_name=app, window_title=win, text=text, source=src)
    return store


def test_ask_spans_screen_and_audio_with_citations(tmp_path):
    store = _seed(EyesStore(db_path=str(tmp_path / "t.db")))

    res = ask("what did we decide about the audio feature", limit=5, store=store)

    assert res.evidence, "ask should retrieve evidence"
    # The decision was spoken on a call — audio must be reachable, not just screen.
    assert any(ev.source == "heard" for ev in res.evidence)
    # Every piece of evidence is cited (has a real timestamp and a location).
    assert all(ev.timestamp > 0 and ev.where for ev in res.evidence)
    # The irrelevant gardening row should not crowd the top result.
    assert "gardening" not in res.evidence[0].snippet.lower()


def test_hybrid_beats_pure_keyword_ordering(tmp_path):
    store = _seed(EyesStore(db_path=str(tmp_path / "t.db")))
    ev = hybrid_search(store, "Josh FTW invoice", limit=5)
    assert ev
    top_text = " ".join(e.snippet.lower() for e in ev[:2])
    assert "invoice" in top_text or "ftw" in top_text


def test_connections_builds_the_entity_graph(tmp_path):
    store = _seed(EyesStore(db_path=str(tmp_path / "t.db")))

    res = connections("Josh", store=store)
    assert res["found"]
    assert res["mentions"] >= 2
    related = {r["entity"] for r in res["related"]}
    # Josh co-occurs with the FTW project, the invoice file, and the zoom app.
    assert any("invoice" in r.lower() for r in related) or "FTW" in related or "zoom" in related


def test_extract_entities_pulls_people_files_apps(tmp_path):
    store = EyesStore(db_path=str(tmp_path / "t.db"))
    store.insert(timestamp=time.time(), app_name="Cursor", window_title="engram/store.py",
                 text="Josh reviewed the migration in store.py and pinged @mason", source="screen")
    e = store.get_latest()
    ents = extract_entities(e)
    assert "Josh" in ents["person"]
    assert "mason" in ents["person"]
    assert any(f.endswith(".py") for f in ents["file"])
    assert "Cursor" in ents["app"]

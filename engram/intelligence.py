"""
intelligence.py — ask your memory, and the entity graph that connects it.

This is the layer that makes Engram *understand* instead of merely *record*.
Two capabilities, both fully local, text-only, no network and no model call:

  ask(question)        Hybrid retrieval over the unified seen-and-heard
                       timeline: keyword (FTS5) and semantic (TF-IDF) rankings
                       fused by reciprocal rank, returned as ranked, cited
                       evidence plus an extractive answer. The MCP client turns
                       that evidence into prose — Engram supplies the facts and
                       the citations, the agent writes the sentence.

  connections(name)    A lightweight knowledge graph — people, projects, apps,
                       and files that co-occur across your captures — so you can
                       ask "what's connected to Josh / the FTW launch".

Why this beats raw recall: one ranked answer spans screen AND audio, ranking
improves on bare FTS via TF-IDF salience, and it resolves who/what — all over
2KB-of-text rows, not gigabytes of frames. (Lexical TF-IDF matches on shared
salient terms, not synonyms; local embeddings are the next upgrade and slot in
behind the same ask() surface.)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import Counter, defaultdict

from engram.store import get_store, parse_natural_time
from engram.semantic import TFIDFIndex, _tokenize
from engram._stop_words import STOP_WORDS

DEFAULT_LOOKBACK_DAYS = 7
POOL_CAP = 5000
RRF_K = 60  # reciprocal-rank-fusion damping constant


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    entry_id: int
    timestamp: float
    source: str          # 'seen' (screen) or 'heard' (audio)
    where: str           # app — window title
    snippet: str
    score: float


@dataclass
class Answer:
    question: str
    answer: str                       # extractive, local — prose is the agent's job
    evidence: list = field(default_factory=list)   # list[Evidence], best first
    entities: list = field(default_factory=list)   # entities present in the evidence
    span: Optional[tuple] = None      # (start, end) epoch if the question was time-scoped


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kind(source: str) -> str:
    return "heard" if source == "audio" else "seen"


def _snippet(text: str, q_terms: set[str], width: int = 220) -> str:
    """A readable window of `text` centered on the first matching query term."""
    text = (text or "").replace(">>>", "").replace("<<<", "")
    text = " ".join(text.split())
    if not text:
        return ""
    low = text.lower()
    pos = -1
    for t in q_terms:
        i = low.find(t)
        if i != -1:
            pos = i
            break
    if pos == -1:
        return text[:width] + ("…" if len(text) > width else "")
    start = max(0, pos - 60)
    end = min(len(text), start + width)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


# Cues that mean the user scoped the question in time. Only then do we narrow
# the candidate pool; otherwise we look back DEFAULT_LOOKBACK_DAYS.
_TIME_CUES = (
    "today", "yesterday", "this morning", "last night",
    "this week", "last week", "this month",
)


def _maybe_timescope(question: str) -> Optional[tuple]:
    q = question.lower()
    for cue in _TIME_CUES:
        if cue in q:
            return parse_natural_time(cue)
    m = re.search(r"last (\d+) (hour|hours|day|days|week|weeks)", q)
    if m:
        return parse_natural_time(f"last {m.group(1)} {m.group(2)}")
    return None


def _candidate_pool(store, span: Optional[tuple]):
    if span:
        start, end = span
    else:
        end = time.time()
        start = end - DEFAULT_LOOKBACK_DAYS * 86400
    return store.get_by_time_range(start, end, limit=POOL_CAP)


# ---------------------------------------------------------------------------
# Hybrid retrieval (keyword FTS5 + semantic TF-IDF, fused by reciprocal rank)
# ---------------------------------------------------------------------------

def hybrid_search(store, query: str, limit: int = 8, span: Optional[tuple] = None) -> list[Evidence]:
    pool = _candidate_pool(store, span)
    pool_map = {e.id: e for e in pool}

    # Semantic ranking over the pool.
    idx = TFIDFIndex()
    idx.build(pool)
    sem = idx.search(query, top_k=limit * 3)

    # Keyword ranking via FTS5 over the whole DB (already rank-ordered).
    try:
        kw = store.search(query, limit=limit * 3)
    except Exception:
        kw = []
    for e in kw:
        pool_map.setdefault(e.id, e)   # keyword hits may fall outside the pool window

    # Reciprocal Rank Fusion — combine the two rankings without tuning weights.
    scores: dict[int, float] = defaultdict(float)
    for rank, (doc_id, _sim, _shared) in enumerate(sem):
        scores[doc_id] += 1.0 / (RRF_K + rank)
    for rank, e in enumerate(kw):
        scores[e.id] += 1.0 / (RRF_K + rank)

    q_terms = set(_tokenize(query))
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:limit]

    out = []
    for doc_id, sc in ranked:
        e = pool_map.get(doc_id)
        if e is None:
            continue
        where = e.app_name or "?"
        if e.window_title:
            where += f" — {e.window_title}"
        out.append(Evidence(
            entry_id=e.id,
            timestamp=e.timestamp,
            source=_kind(getattr(e, "source", "screen")),
            where=where,
            snippet=_snippet(e.text or "", q_terms),
            score=round(sc, 4),
        ))
    return out


def _extractive_answer(evidence: list[Evidence]) -> str:
    if not evidence:
        return "No memory of that yet."
    lines = ["Based on what you saw and heard:"]
    for ev in evidence[:3]:
        when = time.strftime("%b %d %H:%M", time.localtime(ev.timestamp))
        lines.append(f"  • [{ev.source}, {when}, {ev.where}] {ev.snippet}")
    lines.append("")
    lines.append("(Engram returns the cited evidence; ask your agent to synthesize the answer.)")
    return "\n".join(lines)


def ask(question: str, limit: int = 8, store=None) -> Answer:
    """Ask the unified memory a question. Returns cited evidence + an extractive
    answer. Fully local — no model call, no network."""
    store = store or get_store()
    span = _maybe_timescope(question)
    evidence = hybrid_search(store, question, limit=limit, span=span)

    # Surface the entities that actually appear in the retrieved evidence.
    pool_by_id = {ev.entry_id: ev for ev in evidence}
    ents: Counter = Counter()
    for e in _candidate_pool(store, span):
        if e.id in pool_by_id:
            for typ, vals in extract_entities(e).items():
                for v in vals:
                    ents[v] += 1
    entities = [v for v, _ in ents.most_common(12)]

    return Answer(question=question, answer=_extractive_answer(evidence),
                  evidence=evidence, entities=entities, span=span)


# ---------------------------------------------------------------------------
# Knowledge graph (lightweight, local, heuristic)
# ---------------------------------------------------------------------------

# Capitalized words that are almost never the entity you mean — UI chrome,
# weekday/month names, etc. Keeps the heuristic person/proper-noun pass quieter.
_COMMON_CAP = {
    "The", "This", "That", "From", "With", "And", "For", "You", "Your", "New",
    "Open", "Close", "Save", "Edit", "File", "View", "Search", "Home", "Back",
    "Next", "Send", "Reply", "Inbox", "Settings", "Monday", "Tuesday",
    "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January",
    "February", "March", "April", "June", "July", "August", "September",
    "October", "November", "December", "Today", "Tomorrow", "Yesterday",
}

_FILE_RE = re.compile(
    r"\b[\w-]{1,40}\.(?:py|js|ts|tsx|jsx|md|json|java|go|rs|sql|sh|txt|pdf|csv|"
    r"ya?ml|toml|png|jpg|xlsx|docx)\b", re.IGNORECASE)
_MENTION_RE = re.compile(r"@([A-Za-z][\w.-]{1,30})")
_CAP_RE = re.compile(r"\b([A-Z][a-z]{2,20})\b")


def extract_entities(entry) -> dict[str, set]:
    """Heuristic, local entity pull from one capture. 'person' is best-effort
    (capitalized tokens + @mentions) and gets denoised by co-occurrence weight."""
    text = f"{entry.window_title} {entry.text}"
    people = set(_MENTION_RE.findall(text))
    for w in _CAP_RE.findall(text):
        if w in _COMMON_CAP or w.lower() in STOP_WORDS:
            continue
        people.add(w)
    files = {f for f in _FILE_RE.findall(text)}
    apps = {entry.app_name} if entry.app_name else set()
    projects = {entry.project} if getattr(entry, "project", "") else set()
    return {"person": people, "file": files, "app": apps, "project": projects}


def build_graph(entries):
    """Co-occurrence graph: nodes are (type, value); an edge joins two entities
    seen in the same capture. Returns (node_count, edges)."""
    node_count: Counter = Counter()
    edges: dict = defaultdict(Counter)
    for e in entries:
        ents = []
        for typ, vals in extract_entities(e).items():
            for v in vals:
                ents.append((typ, v))
        for node in ents:
            node_count[node] += 1
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                a, b = ents[i], ents[j]
                if a == b:
                    continue
                edges[a][b] += 1
                edges[b][a] += 1
    return node_count, edges


def connections(entity: str, limit: int = 15, store=None, span: Optional[tuple] = None) -> dict:
    """What's connected to `entity` across your memory — people, projects, apps,
    files that co-occur with it, ranked by how often."""
    store = store or get_store()
    pool = _candidate_pool(store, span)
    node_count, edges = build_graph(pool)

    needle = entity.lower()
    matches = [node for node in node_count if needle in node[1].lower()]
    if not matches:
        return {"entity": entity, "found": False, "mentions": 0, "related": []}

    related: Counter = Counter()
    mentions = 0
    match_set = set(matches)
    for m in matches:
        mentions += node_count[m]
        for other, c in edges[m].items():
            if other not in match_set:
                related[other] += c

    return {
        "entity": entity,
        "found": True,
        "mentions": mentions,
        "related": [{"entity": v, "type": t, "weight": c}
                    for (t, v), c in related.most_common(limit)],
    }

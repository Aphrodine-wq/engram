"""
cognition.py — Engram's cognitive memory layer (the brain on top of the eyes).

Raw perception lives in `frames` (store.py): every line of text Engram has seen
or heard. That's episodic, append-only, and huge. Cognition adds a *second*
layer that behaves like human memory rather than a log:

  * Two tiers   — a small, always-loaded WORKING set (what's relevant now) over
                  a large LONG-TERM store, with promotion/demotion under a token
                  budget. This is the answer to the track's "recall critical
                  memories within limited context windows."
  * Association — memories link to memories (memory_edges), so recall can spread
                  along connections, not just match keywords. (Phase 2b)
  * Metabolism  — a sleep/consolidation loop decays, merges, reconciles, and
                  reality-checks memories so the store forgets on purpose.
                  (Phase 2c)

This module owns the *schema* for all three (one migration) and the two-tier
logic. The edges + consolidation_log tables are created here so Phases 2b/2c
are pure additions with no second migration.

The schema is additive: it never touches `frames`, `frames_fts`, or their
triggers. It attaches to the same SQLite database via PRAGMA user_version so an
existing Engram install upgrades in place on first open.
"""

from __future__ import annotations

import json
import time
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from engram.store import get_store, DB_PATH

# Cognitive schema version, tracked independently of the perception store via
# PRAGMA user_version. Bump when the cognitive tables change.
COGNITION_SCHEMA_VERSION = 1

# Default working-set token budget — the "limited context window" the agent
# recalls into. Tunable per call; this is the headline knob.
DEFAULT_WORKING_BUDGET_TOKENS = 4000

# Rough chars-per-token for budgeting without a tokenizer dependency. Qwen's
# tokenizer can replace this in Phase 1 for exactness.
_CHARS_PER_TOKEN = 4

TIER_WORKING = "working"
TIER_LONG_TERM = "long_term"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
-- A consolidated unit of memory, distinct from a raw `frames` capture.
-- Derived from one or more frames and reshaped by the consolidation loop.
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,                  -- the memory statement
    kind          TEXT NOT NULL DEFAULT 'episodic', -- episodic|semantic|preference|fact
    tier          TEXT NOT NULL DEFAULT 'working',  -- working|long_term
    salience      REAL NOT NULL DEFAULT 0.5,       -- importance 0..1 (promotion + eviction)
    strength      REAL NOT NULL DEFAULT 1.0,       -- current strength; decays over time
    embedding     BLOB,                            -- float32 vector (qwen.pack_vector); NULL until embedded
    embed_model   TEXT NOT NULL DEFAULT '',        -- model that produced `embedding`
    created_at    REAL NOT NULL,
    last_access   REAL NOT NULL,                   -- last recall (spaced-repetition refresh)
    access_count  INTEGER NOT NULL DEFAULT 0,
    decay_rate    REAL NOT NULL DEFAULT 0.05,      -- per-day forgetting rate
    provenance    TEXT NOT NULL DEFAULT '{}',      -- JSON: source frame ids, file/flag refs to verify
    verified_at   REAL NOT NULL DEFAULT 0,         -- last reality-check (consolidation loop)
    status        TEXT NOT NULL DEFAULT 'active',  -- active|stale|superseded|forgotten
    superseded_by INTEGER,                         -- memory id that replaced this one
    UNIQUE(content)
);

CREATE INDEX IF NOT EXISTS idx_mem_tier        ON memories(tier, status);
CREATE INDEX IF NOT EXISTS idx_mem_salience    ON memories(salience);
CREATE INDEX IF NOT EXISTS idx_mem_last_access ON memories(last_access);
CREATE INDEX IF NOT EXISTS idx_mem_status      ON memories(status);

-- Keyword recall over consolidated memories (parallel to frames_fts).
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

-- Associative graph edges (Phase 2b). Recall spreads along these.
CREATE TABLE IF NOT EXISTS memory_edges (
    src        INTEGER NOT NULL,
    dst        INTEGER NOT NULL,
    weight     REAL NOT NULL DEFAULT 0.0,        -- embedding sim + co-occurrence + co-recall
    kind       TEXT NOT NULL DEFAULT 'associative', -- associative|contradicts|supersedes|co_occurs
    updated_at REAL NOT NULL,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON memory_edges(src);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON memory_edges(dst);

-- Audit trail of the metabolic loop (Phase 2c). Doubles as the demo's
-- "watch the memory forget and self-correct" view.
CREATE TABLE IF NOT EXISTS consolidation_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at     REAL NOT NULL,
    action     TEXT NOT NULL,    -- decay|merge|reconcile|verify|demote|promote|forget
    memory_id  INTEGER,
    detail     TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_consolidation_ran ON consolidation_log(ran_at);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the cognitive tables if missing. Idempotent; safe on every open."""
    conn.executescript(_SCHEMA)
    # Track cognitive schema version separately from the perception store.
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < COGNITION_SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {COGNITION_SCHEMA_VERSION}")
    conn.commit()


# ---------------------------------------------------------------------------
# Memory record
# ---------------------------------------------------------------------------

@dataclass
class Memory:
    id: int
    content: str
    kind: str = "episodic"
    tier: str = TIER_WORKING
    salience: float = 0.5
    strength: float = 1.0
    created_at: float = 0.0
    last_access: float = 0.0
    access_count: int = 0
    decay_rate: float = 0.05
    provenance: dict = field(default_factory=dict)
    verified_at: float = 0.0
    status: str = "active"
    superseded_by: Optional[int] = None

    @property
    def est_tokens(self) -> int:
        return max(1, len(self.content) // _CHARS_PER_TOKEN)

    def current_strength(self, now: Optional[float] = None) -> float:
        """Exponential forgetting curve refreshed by access (spaced repetition).

        strength * exp(-decay_rate * days_since_access). Recall resets the clock,
        so frequently-recalled memories stay strong and unused ones fade — the
        basis for the consolidation loop's demote/forget decisions.
        """
        import math
        now = now if now is not None else time.time()
        days = max(0.0, (now - self.last_access) / 86400.0)
        return self.strength * math.exp(-self.decay_rate * days)


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        content=row["content"],
        kind=row["kind"],
        tier=row["tier"],
        salience=row["salience"],
        strength=row["strength"],
        created_at=row["created_at"],
        last_access=row["last_access"],
        access_count=row["access_count"],
        decay_rate=row["decay_rate"],
        provenance=_loads(row["provenance"]),
        verified_at=row["verified_at"],
        status=row["status"],
        superseded_by=row["superseded_by"],
    )


def _loads(s: str) -> dict:
    try:
        return json.loads(s) if s else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# CognitiveStore — the two-tier API
# ---------------------------------------------------------------------------

class CognitiveStore:
    """Two-tier memory over the same SQLite database as the perception store.

    Working tier  = the small, hot set the agent recalls into now.
    Long-term tier = everything retained but not currently in context.

    Promotion/demotion is governed by a token budget so the working set always
    fits the model's context window — the crux of the track's "limited context"
    requirement. Association-spreading (2b) and consolidation (2c) extend this
    same store.
    """

    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        # Reuse the perception store's connection so cognition and perception
        # share one database file and one WAL.
        self.conn = conn or get_store().conn
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)

    # -- writes -------------------------------------------------------------

    def remember(
        self,
        content: str,
        *,
        kind: str = "episodic",
        salience: float = 0.5,
        provenance: Optional[dict] = None,
        tier: str = TIER_WORKING,
    ) -> int:
        """Insert (or revive) a consolidated memory. Returns its id.

        Deduplicates on exact content via UNIQUE; a re-remember refreshes
        access + bumps salience rather than duplicating. (Near-duplicate
        merging by embedding is the consolidation loop's job, Phase 2c.)
        """
        now = time.time()
        cur = self.conn.execute(
            """
            INSERT INTO memories (content, kind, tier, salience, strength,
                                  created_at, last_access, access_count, provenance)
            VALUES (?, ?, ?, ?, 1.0, ?, ?, 0, ?)
            ON CONFLICT(content) DO UPDATE SET
                last_access  = excluded.last_access,
                access_count = access_count + 1,
                salience     = MAX(salience, excluded.salience),
                status       = 'active'
            """,
            (content, kind, tier, salience, now, now, json.dumps(provenance or {})),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM memories WHERE content = ?", (content,)).fetchone()
        return row["id"]

    def touch(self, memory_id: int) -> None:
        """Record a recall: refresh last_access (resets the forgetting clock) and
        increment access_count. Called whenever a memory is surfaced."""
        self.conn.execute(
            "UPDATE memories SET last_access = ?, access_count = access_count + 1 WHERE id = ?",
            (time.time(), memory_id),
        )
        self.conn.commit()

    # -- reads --------------------------------------------------------------

    def get(self, memory_id: int) -> Optional[Memory]:
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _row_to_memory(row) if row else None

    def working_set(self, budget_tokens: int = DEFAULT_WORKING_BUDGET_TOKENS) -> list[Memory]:
        """The memories currently in the working tier, capped at the token budget.

        Ordered by a recall score (salience × current strength) so that if the
        working tier overflows the budget, the least valuable ones are left out
        of the returned context — without being evicted from storage.
        """
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE tier = ? AND status = 'active'",
            (TIER_WORKING,),
        ).fetchall()
        mems = sorted(
            (_row_to_memory(r) for r in rows),
            key=lambda m: m.salience * m.current_strength(),
            reverse=True,
        )
        out, used = [], 0
        for m in mems:
            if used + m.est_tokens > budget_tokens:
                continue
            out.append(m)
            used += m.est_tokens
        return out

    # -- tier movement ------------------------------------------------------

    def promote(self, memory_id: int) -> None:
        """Move a memory into the working tier (it became relevant now)."""
        self._set_tier(memory_id, TIER_WORKING, action="promote")

    def demote(self, memory_id: int) -> None:
        """Move a memory out of the working tier into long-term storage."""
        self._set_tier(memory_id, TIER_LONG_TERM, action="demote")

    def _set_tier(self, memory_id: int, tier: str, *, action: str) -> None:
        self.conn.execute("UPDATE memories SET tier = ? WHERE id = ?", (tier, memory_id))
        self.conn.execute(
            "INSERT INTO consolidation_log (ran_at, action, memory_id, detail) VALUES (?, ?, ?, ?)",
            (time.time(), action, memory_id, f"-> {tier}"),
        )
        self.conn.commit()

    def rebalance_working_tier(self, budget_tokens: int = DEFAULT_WORKING_BUDGET_TOKENS) -> dict:
        """Keep the working tier within budget by demoting the weakest memories
        until it fits. Returns a small summary for logging/telemetry.

        This is the always-on half of tiering; the consolidation loop (2c) does
        the deeper merge/reconcile/forget pass on a slower cadence.
        """
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE tier = ? AND status = 'active'",
            (TIER_WORKING,),
        ).fetchall()
        mems = sorted(
            (_row_to_memory(r) for r in rows),
            key=lambda m: m.salience * m.current_strength(),
            reverse=True,
        )
        kept, used, demoted = 0, 0, 0
        for m in mems:
            if used + m.est_tokens <= budget_tokens:
                used += m.est_tokens
                kept += 1
            else:
                self.demote(m.id)
                demoted += 1
        return {"kept": kept, "demoted": demoted, "tokens_used": used, "budget": budget_tokens}


def get_cognitive_store() -> CognitiveStore:
    """Convenience accessor mirroring store.get_store()."""
    return CognitiveStore()

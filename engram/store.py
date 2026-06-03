"""
Engram store — SQLite storage with FTS5 full-text search.
Stores only parsed text, never images. ~2KB per entry.
"""

from __future__ import annotations

import sqlite3
import time
import os
import json
import zlib
import threading
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache


# All Engram data lives here. Self-contained — never shares a directory with
# any other tool. Override with $ENGRAM_HOME if you want it elsewhere.
def _data_dir() -> Path:
    override = os.environ.get("ENGRAM_HOME")
    return Path(override).expanduser() if override else Path.home() / ".engram"

DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "engram.db"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "ignore_apps": ["1Password", "Keychain Access", "LastPass", "Bitwarden"],
    "session_gap_minutes": 15,
    "capture_interval": 10,
}


_config_cache = None
_config_mtime = 0.0


def load_config() -> dict:
    """Load config from ~/.claude-eyes/config.json, with mtime-based caching."""
    global _config_cache, _config_mtime
    if CONFIG_PATH.exists():
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if _config_cache is not None and mtime == _config_mtime:
                return _config_cache
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            merged = {**DEFAULT_CONFIG, **cfg}
            _config_cache = merged
            _config_mtime = mtime
            return merged
        except Exception:
            return DEFAULT_CONFIG.copy()
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        _config_cache = DEFAULT_CONFIG.copy()
        return _config_cache


def is_app_ignored(app_name: str, config: dict = None) -> bool:
    """Check if an app is on the ignore list."""
    if config is None:
        config = load_config()
    ignored = [a.lower() for a in config.get("ignore_apps", [])]
    return app_name.lower() in ignored


def _parse_time_of_day(text: str) -> Optional[int]:
    """Extract hour (0-23) from natural language time references."""
    import re
    text = text.strip()
    # "3pm", "3 pm", "11am", "11 am"
    m = re.search(r'(\d{1,2})\s*(am|pm)', text)
    if m:
        hour = int(m.group(1))
        if m.group(2) == 'pm' and hour != 12:
            hour += 12
        elif m.group(2) == 'am' and hour == 12:
            hour = 0
        return hour
    # "midnight"
    if 'midnight' in text:
        return 0
    # "noon"
    if 'noon' in text:
        return 12
    return None


def parse_natural_time(expression: str) -> tuple[float, float]:
    """
    Parse natural language time expressions into (start, end) timestamps.
    Supports: 'this morning', 'yesterday at 4pm', 'last night at midnight',
    'last 2 hours', 'today', etc.
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    expression = expression.lower().strip()

    if expression in ("this morning", "morning"):
        start = today_start.replace(hour=6)
        end = today_start.replace(hour=12)
    elif expression in ("this afternoon", "afternoon"):
        start = today_start.replace(hour=12)
        end = today_start.replace(hour=17)
    elif expression in ("this evening", "evening", "tonight"):
        start = today_start.replace(hour=17)
        end = today_start.replace(hour=23, minute=59)
    elif expression == "today":
        start = today_start
        end = now
    elif expression.startswith("yesterday"):
        yesterday = today_start - timedelta(days=1)
        if expression == "yesterday":
            start = yesterday
            end = today_start
        elif expression == "yesterday morning":
            start = yesterday.replace(hour=6)
            end = yesterday.replace(hour=12)
        elif expression == "yesterday afternoon":
            start = yesterday.replace(hour=12)
            end = yesterday.replace(hour=17)
        elif expression == "yesterday evening" or expression == "yesterday night":
            start = yesterday.replace(hour=17)
            end = yesterday.replace(hour=23, minute=59)
        else:
            # "yesterday at 4pm", "yesterday around 3pm", etc.
            hour = _parse_time_of_day(expression)
            if hour is not None:
                start = yesterday.replace(hour=hour)
                end = start + timedelta(hours=1)
            else:
                start = yesterday
                end = today_start
    elif "last night" in expression:
        last_night = today_start - timedelta(days=1)
        hour = _parse_time_of_day(expression)
        if hour is not None:
            # "last night at midnight" -> midnight = start of today
            if hour == 0:
                start = today_start
                end = start + timedelta(hours=1)
            else:
                start = last_night.replace(hour=hour)
                end = start + timedelta(hours=1)
        else:
            # generic "last night"
            start = last_night.replace(hour=20)
            end = today_start
    elif expression.startswith("last "):
        # Parse "last N hours/minutes/days"
        parts = expression.split()
        if len(parts) >= 3:
            try:
                n = int(parts[1])
            except ValueError:
                n = 1
            unit = parts[2].rstrip("s")  # strip plural
            if unit == "hour":
                start = now - timedelta(hours=n)
            elif unit == "minute" or unit == "min":
                start = now - timedelta(minutes=n)
            elif unit == "day":
                start = now - timedelta(days=n)
            elif unit == "week":
                start = now - timedelta(weeks=n)
            else:
                start = now - timedelta(hours=1)
        else:
            start = now - timedelta(hours=1)
        end = now
    elif expression == "this week":
        # Monday of this week
        start = today_start - timedelta(days=now.weekday())
        end = now
    elif expression == "last week":
        this_monday = today_start - timedelta(days=now.weekday())
        start = this_monday - timedelta(weeks=1)
        end = this_monday
    else:
        # Try to extract a time-of-day for today ("at 3pm", "around noon")
        hour = _parse_time_of_day(expression)
        if hour is not None:
            start = today_start.replace(hour=hour)
            end = start + timedelta(hours=1)
        else:
            # Default: last hour
            start = now - timedelta(hours=1)
            end = now

    return (start.timestamp(), end.timestamp())


@dataclass
class ScreenEntry:
    id: int
    timestamp: float
    app_name: str
    window_title: str
    text: str
    extra_context: str
    project: str = ""

    def _decrypt(self) -> "ScreenEntry":
        """Decrypt encrypted fields in-place. Handles mixed encrypted/plaintext."""
        try:
            from engram.encryption import decrypt
            self.text = decrypt(self.text)
            self.window_title = decrypt(self.window_title)
            self.extra_context = decrypt(self.extra_context)
        except Exception:
            pass  # If decryption fails, return as-is
        return self


@dataclass
class Session:
    """A work session — contiguous period of activity."""
    start: float
    end: float
    duration_minutes: float
    apps: list[str]
    top_app: str
    frame_count: int
    summary: str


class EyesStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")       # faster concurrent reads
        self.conn.execute("PRAGMA synchronous=NORMAL")      # good enough durability
        self.conn.execute("PRAGMA cache_size=-8000")         # 8MB cache
        self.conn.execute("PRAGMA mmap_size=67108864")       # 64MB mmap for faster reads
        self._init_db()

        # Encryption — opt-in via config
        self._encryption_enabled = False
        try:
            cfg = load_config()
            if cfg.get("encryption", False):
                from engram.encryption import is_available, harden_permissions
                if is_available():
                    self._encryption_enabled = True
                    harden_permissions(str(self.db_path))
        except Exception:
            pass

    def _init_db(self):
        # Migration: add project column if table already exists without it
        try:
            self.conn.execute("SELECT project FROM frames LIMIT 1")
        except sqlite3.OperationalError:
            try:
                self.conn.execute("ALTER TABLE frames ADD COLUMN project TEXT DEFAULT ''")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # table doesn't exist yet, CREATE TABLE will handle it

        self.conn.executescript("""
            -- Profile tables (persistent user insights + mood/energy log)
            CREATE TABLE IF NOT EXISTS user_insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                sample_count INTEGER DEFAULT 1,
                first_seen REAL NOT NULL,
                last_updated REAL NOT NULL,
                UNIQUE(category, key)
            );

            CREATE INDEX IF NOT EXISTS idx_insights_category
                ON user_insights(category);

            CREATE TABLE IF NOT EXISTS mood_energy_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                energy_level REAL,
                mood_state TEXT,
                source TEXT DEFAULT 'behavioral',
                context_json TEXT DEFAULT '{}',
                webcam_path TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_mood_timestamp
                ON mood_energy_log(timestamp);
        """)
        self.conn.commit()

        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                app_name TEXT DEFAULT '',
                window_title TEXT DEFAULT '',
                text TEXT NOT NULL,
                extra_context TEXT DEFAULT '',
                phash TEXT DEFAULT '',
                project TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_frames_timestamp ON frames(timestamp);
            CREATE INDEX IF NOT EXISTS idx_frames_app ON frames(app_name);
            CREATE INDEX IF NOT EXISTS idx_frames_project ON frames(project);

            -- FTS5 virtual table for full-text search
            CREATE VIRTUAL TABLE IF NOT EXISTS frames_fts USING fts5(
                text,
                extra_context,
                app_name,
                window_title,
                content='frames',
                content_rowid='id',
                tokenize='porter unicode61'
            );

            -- Triggers to keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS frames_ai AFTER INSERT ON frames BEGIN
                INSERT INTO frames_fts(rowid, text, extra_context, app_name, window_title)
                VALUES (new.id, new.text, new.extra_context, new.app_name, new.window_title);
            END;

            CREATE TRIGGER IF NOT EXISTS frames_ad AFTER DELETE ON frames BEGIN
                INSERT INTO frames_fts(frames_fts, rowid, text, extra_context, app_name, window_title)
                VALUES ('delete', old.id, old.text, old.extra_context, old.app_name, old.window_title);
            END;

            -- UPDATE trigger: delete old FTS entry, insert new one
            CREATE TRIGGER IF NOT EXISTS frames_au AFTER UPDATE ON frames BEGIN
                INSERT INTO frames_fts(frames_fts, rowid, text, extra_context, app_name, window_title)
                VALUES ('delete', old.id, old.text, old.extra_context, old.app_name, old.window_title);
                INSERT INTO frames_fts(rowid, text, extra_context, app_name, window_title)
                VALUES (new.id, new.text, new.extra_context, new.app_name, new.window_title);
            END;
        """)
        self.conn.commit()

    def insert(self, timestamp: float, app_name: str, window_title: str,
               text: str, extra_context: str = "", phash: str = "",
               project: str = "") -> int:
        if self._encryption_enabled:
            from engram.encryption import encrypt_fields
            text, window_title, extra_context = encrypt_fields(text, window_title, extra_context)
        cur = self.conn.execute(
            "INSERT INTO frames (timestamp, app_name, window_title, text, extra_context, phash, project) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (timestamp, app_name, window_title, text, extra_context, phash, project)
        )
        self.conn.commit()
        return cur.lastrowid

    def insert_batch(self, entries: list[tuple]) -> int:
        """
        Batch insert multiple entries for performance.
        Each entry: (timestamp, app_name, window_title, text, extra_context, phash)
        Returns count inserted.
        """
        self.conn.executemany(
            "INSERT INTO frames (timestamp, app_name, window_title, text, extra_context, phash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            entries
        )
        self.conn.commit()
        return len(entries)

    def compress_old(self, days: int = 3, _skip_fts_rebuild: bool = False) -> dict:
        """
        Compress text in entries older than N days.
        Keeps only the first 500 chars of text for old entries,
        reducing storage by ~60% for aged data while preserving
        FTS search capability via window titles and keywords.
        """
        cutoff = time.time() - (days * 86400)

        # Get stats before update
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(text) - 500), 0) "
            "FROM frames WHERE timestamp < ? AND LENGTH(text) > 500",
            (cutoff,)
        ).fetchone()
        compressed, total_saved = row[0], row[1]

        if compressed == 0:
            return {"compressed": 0, "bytes_saved": 0, "bytes_saved_mb": 0}

        # Single bulk UPDATE
        self.conn.execute(
            "UPDATE frames SET text = SUBSTR(text, 1, 500) "
            "WHERE timestamp < ? AND LENGTH(text) > 500",
            (cutoff,)
        )

        if not _skip_fts_rebuild:
            self.conn.execute("INSERT INTO frames_fts(frames_fts) VALUES('rebuild')")
        self.conn.commit()

        return {
            "compressed": compressed,
            "bytes_saved": total_saved,
            "bytes_saved_mb": round(total_saved / (1024 * 1024), 2),
        }

    def deduplicate(self, hours: int = 24, _skip_fts_rebuild: bool = False) -> int:
        """
        Remove near-duplicate entries within a time window.
        Two entries are duplicates if same app + same phash within 60s.
        Returns count removed.
        """
        cutoff = time.time() - (hours * 3600)
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, phash FROM frames "
            "WHERE timestamp > ? AND phash != '' ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if len(rows) < 2:
            return 0

        to_delete = []
        prev = rows[0]

        for row in rows[1:]:
            # Same app, same phash, within 60s
            if (row[2] == prev[2] and row[3] == prev[3]
                    and abs(row[1] - prev[1]) < 60):
                to_delete.append(row[0])
            else:
                prev = row

        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            self.conn.execute(f"DELETE FROM frames WHERE id IN ({placeholders})", to_delete)
            if not _skip_fts_rebuild:
                self.conn.execute("INSERT INTO frames_fts(frames_fts) VALUES('rebuild')")
            self.conn.commit()

        return len(to_delete)

    def _entries(self, rows) -> list[ScreenEntry]:
        """Convert raw DB rows to ScreenEntry list, decrypting if needed."""
        entries = [ScreenEntry(*r) for r in rows]
        if self._encryption_enabled:
            for e in entries:
                e._decrypt()
        return entries

    def _entry(self, row) -> Optional[ScreenEntry]:
        """Convert a single DB row to ScreenEntry, decrypting if needed."""
        if row is None:
            return None
        e = ScreenEntry(*row)
        if self._encryption_enabled:
            e._decrypt()
        return e

    def get_recent(self, minutes: int = 30, limit: int = 50) -> list[ScreenEntry]:
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context, project "
            "FROM frames WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?",
            (cutoff, limit)
        ).fetchall()
        return self._entries(rows)

    def get_latest(self) -> Optional[ScreenEntry]:
        row = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context, project "
            "FROM frames ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        return self._entry(row)

    def search(self, query: str, limit: int = 20) -> list[ScreenEntry]:
        """Full-text search across all screen captures."""
        if self._encryption_enabled:
            # FTS indexes encrypted blobs — fall back to app-level search
            return self._encrypted_search(query, limit)
        rows = self.conn.execute(
            "SELECT f.id, f.timestamp, f.app_name, f.window_title, "
            "       snippet(frames_fts, 0, '>>>', '<<<', '...', 40) as text, "
            "       f.extra_context, f.project "
            "FROM frames_fts "
            "JOIN frames f ON f.id = frames_fts.rowid "
            "WHERE frames_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, limit)
        ).fetchall()
        return self._entries(rows)

    def _encrypted_search(self, query: str, limit: int = 20) -> list[ScreenEntry]:
        """Search encrypted DB by decrypting recent entries and filtering in Python."""
        # Search non-encrypted fields first (app_name, project)
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context, project "
            "FROM frames WHERE app_name LIKE ? OR project LIKE ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", limit * 5)
        ).fetchall()
        results = self._entries(rows)
        matched = [e for e in results if query.lower() in
                   f"{e.text} {e.window_title} {e.extra_context}".lower()]
        if len(matched) >= limit:
            return matched[:limit]

        # Broader scan — decrypt last 2 hours and search
        cutoff = time.time() - 7200
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context, project "
            "FROM frames WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 500",
            (cutoff,)
        ).fetchall()
        all_entries = self._entries(rows)
        seen = {e.id for e in matched}
        for e in all_entries:
            if e.id not in seen and query.lower() in \
               f"{e.text} {e.window_title} {e.extra_context}".lower():
                matched.append(e)
                if len(matched) >= limit:
                    break
        return matched[:limit]

    def smart_search(self, query: str, limit: int = 20,
                     project: str = None, exclude_heartbeats: bool = True) -> list[ScreenEntry]:
        """
        Enhanced search: prioritizes window title matches, excludes heartbeats,
        and optionally filters by project.
        """
        # Search with FTS, then re-rank
        all_results = self.search(query, limit=limit * 3)

        # Filter heartbeats
        if exclude_heartbeats:
            all_results = [e for e in all_results if "[heartbeat" not in (e.text or "")]

        # Filter by project
        if project:
            all_results = [
                e for e in all_results
                if project.lower() in (getattr(e, "project", "") or "").lower()
            ]

        # Re-rank: window title matches get 3x weight
        query_lower = query.lower()
        def rank_key(entry):
            score = 0
            if query_lower in (entry.window_title or "").lower():
                score += 3
            if query_lower in (entry.app_name or "").lower():
                score += 2
            if query_lower in (entry.text or "")[:200].lower():
                score += 1
            return -score  # negative for descending

        all_results.sort(key=rank_key)
        return all_results[:limit]

    def search_by_app(self, app_name: str, minutes: int = 60, limit: int = 20) -> list[ScreenEntry]:
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context, project "
            "FROM frames WHERE app_name LIKE ? AND timestamp > ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (f"%{app_name}%", cutoff, limit)
        ).fetchall()
        return self._entries(rows)

    def prune(self, days: int = 7) -> int:
        """Delete entries older than N days. Returns count deleted."""
        cutoff = time.time() - (days * 86400)
        cur = self.conn.execute("DELETE FROM frames WHERE timestamp < ?", (cutoff,))
        self.conn.execute("INSERT INTO frames_fts(frames_fts) VALUES('rebuild')")
        self.conn.commit()
        self.conn.execute("VACUUM")
        return cur.rowcount

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM frames"
        ).fetchone()
        db_size = os.path.getsize(self.db_path) if self.db_path.exists() else 0
        return {
            "total_frames": row[0],
            "oldest_timestamp": row[1],
            "newest_timestamp": row[2],
            "db_size_mb": round(db_size / (1024 * 1024), 2),
        }

    def get_by_time_range(self, start: float, end: float, limit: int = 100) -> list[ScreenEntry]:
        """Get entries within an absolute time range."""
        rows = self.conn.execute(
            "SELECT id, timestamp, app_name, window_title, text, extra_context, project "
            "FROM frames WHERE timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (start, end, limit)
        ).fetchall()
        return self._entries(rows)

    def get_focus_stats(self, minutes: int = 60) -> dict:
        """
        Get app focus time breakdown for the last N minutes.
        Returns time per app, context switches, and top apps.
        """
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT timestamp, app_name FROM frames "
            "WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if not rows:
            return {"apps": {}, "switches": 0, "total_frames": 0}

        app_frames = Counter()
        switches = 0
        prev_app = None

        for ts, app in rows:
            app_frames[app] += 1
            if prev_app and app != prev_app:
                switches += 1
            prev_app = app

        total = sum(app_frames.values())
        config = load_config()
        interval = config.get("capture_interval", 10)

        apps = {}
        for app, count in app_frames.most_common():
            est_minutes = round((count * interval) / 60, 1)
            apps[app] = {
                "frames": count,
                "estimated_minutes": est_minutes,
                "percent": round((count / total) * 100, 1),
            }

        return {
            "apps": apps,
            "switches": switches,
            "total_frames": total,
            "period_minutes": minutes,
        }

    def get_sessions(self, hours: int = 8) -> list[Session]:
        """
        Detect work sessions — contiguous periods of activity
        separated by gaps (default 5 min with no captures).
        """
        cutoff = time.time() - (hours * 3600)
        rows = self.conn.execute(
            "SELECT timestamp, app_name FROM frames "
            "WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if not rows:
            return []

        config = load_config()
        gap_threshold = config.get("session_gap_minutes", 5) * 60

        sessions = []
        session_start = rows[0][0]
        session_apps = [rows[0][1]]
        prev_ts = rows[0][0]

        for ts, app in rows[1:]:
            if ts - prev_ts > gap_threshold:
                # End current session, start new one
                sessions.append(self._build_session(session_start, prev_ts, session_apps))
                session_start = ts
                session_apps = [app]
            else:
                session_apps.append(app)
            prev_ts = ts

        # Final session
        sessions.append(self._build_session(session_start, prev_ts, session_apps))
        return sessions

    def _build_session(self, start: float, end: float, apps: list[str]) -> Session:
        duration = max((end - start) / 60, 0.5)
        app_counts = Counter(apps)
        top_app = app_counts.most_common(1)[0][0] if app_counts else "Unknown"
        unique_apps = list(app_counts.keys())

        # Build a short summary
        parts = []
        for app, count in app_counts.most_common(3):
            pct = round((count / len(apps)) * 100)
            parts.append(f"{app} ({pct}%)")
        summary = f"{duration:.0f}min — " + ", ".join(parts)

        return Session(
            start=start,
            end=end,
            duration_minutes=round(duration, 1),
            apps=unique_apps,
            top_app=top_app,
            frame_count=len(apps),
            summary=summary,
        )

    def get_project_stats(self, minutes: int = 60) -> dict:
        """
        Get project-level time breakdown for the last N minutes.
        Returns {project: {frames, estimated_minutes, percent}}.
        """
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT project, COUNT(*) as cnt FROM frames "
            "WHERE timestamp > ? GROUP BY project ORDER BY cnt DESC",
            (cutoff,)
        ).fetchall()

        if not rows:
            return {}

        config = load_config()
        interval = config.get("capture_interval", 10)
        total = sum(r[1] for r in rows)

        result = {}
        for project, count in rows:
            name = project if project else "(untagged)"
            result[name] = {
                "frames": count,
                "estimated_minutes": round((count * interval) / 60, 1),
                "percent": round((count / total) * 100, 1),
            }
        return result

    def get_activity_summary(self, minutes: int = 60) -> str:
        """
        Generate a narrative summary of recent activity.
        Groups by app and describes the flow of work.
        """
        cutoff = time.time() - (minutes * 60)
        rows = self.conn.execute(
            "SELECT timestamp, app_name, window_title, "
            "SUBSTR(text, 1, 200) as text_preview "
            "FROM frames WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()

        if not rows:
            return f"No activity in the last {minutes} minutes."

        # Group into app segments (consecutive same-app captures)
        segments = []
        current_app = rows[0][1]
        current_start = rows[0][0]
        current_windows = set()
        current_texts = []

        for ts, app, window, text in rows:
            if app != current_app:
                segments.append({
                    "app": current_app,
                    "start": current_start,
                    "end": ts,
                    "windows": list(current_windows),
                    "sample_text": current_texts[-1] if current_texts else "",
                })
                current_app = app
                current_start = ts
                current_windows = set()
                current_texts = []
            if window:
                current_windows.add(window)
            if text.strip():
                current_texts.append(text.strip()[:150])

        # Final segment
        segments.append({
            "app": current_app,
            "start": current_start,
            "end": rows[-1][0],
            "windows": list(current_windows),
            "sample_text": current_texts[-1] if current_texts else "",
        })

        # Build narrative
        lines = [f"Activity summary (last {minutes} min, {len(rows)} captures):\n"]
        for seg in segments:
            start_str = datetime.fromtimestamp(seg["start"]).strftime("%H:%M")
            dur = max((seg["end"] - seg["start"]) / 60, 0.15)
            line = f"  {start_str} | {seg['app']} ({dur:.0f}min)"
            if seg["windows"]:
                wins = [w for w in seg["windows"] if w][:2]
                if wins:
                    line += f" — {', '.join(wins)}"
            lines.append(line)

        # Focus stats
        app_counts = Counter(r[1] for r in rows)
        config = load_config()
        interval = config.get("capture_interval", 10)
        lines.append(f"\nFocus breakdown:")
        for app, count in app_counts.most_common(5):
            est_min = round((count * interval) / 60, 1)
            lines.append(f"  {app}: ~{est_min}min ({round(count/len(rows)*100)}%)")

        return "\n".join(lines)

    def auto_maintain(self, max_db_mb: int = 500, compress_after_days: int = 3,
                      dedup_hours: int = 24) -> dict:
        """
        Run maintenance: compress old text, deduplicate, and prune if DB is too large.
        Does a single FTS rebuild at the end instead of per-operation.
        Safe to call frequently — operations are fast and idempotent.
        """
        import logging
        log = logging.getLogger("claude-eyes.store")
        results = {"compressed": 0, "deduplicated": 0, "pruned": 0}
        needs_fts_rebuild = False

        try:
            # Compress old entries (skip individual FTS rebuild)
            compress = self.compress_old(days=compress_after_days, _skip_fts_rebuild=True)
            results["compressed"] = compress.get("compressed", 0)
            if results["compressed"] > 0:
                needs_fts_rebuild = True

            # Deduplicate (skip individual FTS rebuild)
            results["deduplicated"] = self.deduplicate(hours=dedup_hours, _skip_fts_rebuild=True)
            if results["deduplicated"] > 0:
                needs_fts_rebuild = True

            # Check DB size and prune if over limit
            stats = self.stats()
            if stats["db_size_mb"] > max_db_mb:
                results["pruned"] = self.prune(days=7)
                needs_fts_rebuild = False  # prune already rebuilds FTS
                log.info(
                    "Auto-prune triggered: DB was %.1fMB (limit %dMB), pruned %d entries",
                    stats["db_size_mb"], max_db_mb, results["pruned"]
                )

            # Single FTS rebuild for all operations
            if needs_fts_rebuild:
                self.conn.execute("INSERT INTO frames_fts(frames_fts) VALUES('rebuild')")
                self.conn.commit()

        except Exception as e:
            log.warning("Auto-maintenance error: %s", e)

        return results

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Singleton for connection reuse (thread-safe)
# ---------------------------------------------------------------------------
_store_instance: Optional['EyesStore'] = None
_store_lock = threading.Lock()


def get_store(db_path: Optional[str] = None) -> EyesStore:
    """
    Get or create a singleton EyesStore instance.
    Reuses the same connection across MCP calls instead of
    opening/closing on every tool invocation.
    """
    global _store_instance
    with _store_lock:
        if _store_instance is None:
            _store_instance = EyesStore(db_path)
        return _store_instance

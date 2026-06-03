"""
cli.py — the `engram` command.

    engram watch                 start the capture loop
    engram search "postgres error"
    engram recent --minutes 30
    engram activity --minutes 60
    engram stats                 db size, frame count, span
    engram serve                 REST API on 127.0.0.1:7890
    engram mcp                   run the MCP server (stdio)
"""

from __future__ import annotations

import sys
import argparse
import logging

from engram import __version__
from engram.store import get_store, DATA_DIR, DB_PATH


def _fmt_entry(e) -> str:
    when = __import__("datetime").datetime.fromtimestamp(e.timestamp).strftime("%Y-%m-%d %H:%M")
    where = e.app_name or "?"
    if e.window_title:
        where += f" — {e.window_title}"
    snippet = " ".join((e.text or "").split())[:240]
    return f"[{when}]  {where}\n    {snippet}"


def cmd_watch(a):
    from engram.watcher import watch
    watch(interval=a.interval, scale=a.scale, fast_ocr=not a.accurate)


def cmd_search(a):
    hits = get_store().search(a.query, a.limit)
    if not hits:
        print("no matches.")
        return
    for e in hits:
        print(_fmt_entry(e), "\n")


def cmd_recent(a):
    rows = get_store().get_recent(a.minutes, a.limit)
    for e in rows:
        print(_fmt_entry(e), "\n")


def cmd_activity(a):
    print(get_store().get_activity_summary(a.minutes))


def cmd_stats(a):
    store = get_store()
    n = store.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    span = store.conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM frames").fetchone()
    size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 2) if DB_PATH.exists() else 0
    print(f"engram {__version__}")
    print(f"  data dir : {DATA_DIR}")
    print(f"  db size  : {size_mb} MB")
    print(f"  frames   : {n}")
    if span and span[0]:
        import datetime
        lo = datetime.datetime.fromtimestamp(span[0]).strftime("%Y-%m-%d %H:%M")
        hi = datetime.datetime.fromtimestamp(span[1]).strftime("%Y-%m-%d %H:%M")
        print(f"  span     : {lo}  ->  {hi}")


def cmd_serve(a):
    from engram.rest import serve
    serve(host=a.host, port=a.port)


def cmd_mcp(a):
    from engram.mcp_server import main as mcp_main
    mcp_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engram", description="Your computer's photographic memory.")
    p.add_argument("--version", action="version", version=f"engram {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="start the capture loop")
    w.add_argument("--interval", type=float, default=None, help="seconds between captures")
    w.add_argument("--scale", type=float, default=0.5, help="screenshot downscale before OCR")
    w.add_argument("--accurate", action="store_true", help="Accurate OCR (slower) instead of Fast")
    w.set_defaults(func=cmd_watch)

    s = sub.add_parser("search", help="full-text search your screen history")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("recent", help="recent screen activity")
    r.add_argument("--minutes", type=int, default=30)
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(func=cmd_recent)

    ac = sub.add_parser("activity", help="human-readable activity summary")
    ac.add_argument("--minutes", type=int, default=60)
    ac.set_defaults(func=cmd_activity)

    sub.add_parser("stats", help="database stats").set_defaults(func=cmd_stats)

    sv = sub.add_parser("serve", help="run the REST API (loopback only)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=7890)
    sv.set_defaults(func=cmd_serve)

    sub.add_parser("mcp", help="run the MCP server (stdio)").set_defaults(func=cmd_mcp)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
cli.py — the `engram` command.

    engram watch                 start the screen capture loop
    engram listen                opt-in: mic → local transcript (never stores audio)
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
    kind = "heard" if getattr(e, "source", "screen") == "audio" else "seen "
    where = e.app_name or "?"
    if e.window_title:
        where += f" — {e.window_title}"
    snippet = " ".join((e.text or "").split())[:240]
    return f"[{when}] ({kind}) {where}\n    {snippet}"


def cmd_watch(a):
    from engram.watcher import watch
    watch(interval=a.interval, scale=a.scale, fast_ocr=not a.accurate)


def cmd_listen(a):
    from engram.audio import audio_watch
    audio_watch(seconds=a.seconds, model=a.model, device=a.device)


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


def cmd_ask(a):
    import datetime
    from engram.intelligence import ask
    res = ask(a.question, limit=a.limit)
    print(res.answer)
    print()
    for ev in res.evidence:
        when = datetime.datetime.fromtimestamp(ev.timestamp).strftime("%Y-%m-%d %H:%M")
        print(f"  [{when}] ({'heard' if ev.source == 'heard' else 'seen '}) {ev.where}")
        print(f"      {ev.snippet}")
    if res.entities:
        print("\n  entities:", ", ".join(res.entities[:12]))


def cmd_connections(a):
    from engram.intelligence import connections
    res = connections(a.entity, limit=a.limit)
    if not res["found"]:
        print(f"no memory of '{a.entity}' yet.")
        return
    print(f"{a.entity} — seen in {res['mentions']} captures, connected to:")
    for r in res["related"]:
        print(f"  {r['weight']:>3}  {r['entity']}  ({r['type']})")


def cmd_activity(a):
    print(get_store().get_activity_summary(a.minutes))


def cmd_stats(a):
    store = get_store()
    n = store.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    by_src = dict(store.conn.execute(
        "SELECT COALESCE(source, 'screen'), COUNT(*) FROM frames GROUP BY 1"
    ).fetchall())
    span = store.conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM frames").fetchone()
    size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 2) if DB_PATH.exists() else 0
    print(f"engram {__version__}")
    print(f"  data dir : {DATA_DIR}")
    print(f"  db size  : {size_mb} MB")
    print(f"  frames   : {n}  (seen {by_src.get('screen', 0)}, heard {by_src.get('audio', 0)})")
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

    ln = sub.add_parser("listen", help="OPT-IN: capture mic audio -> local transcript (raw audio never stored)")
    ln.add_argument("--seconds", type=float, default=15.0, help="segment length to record before transcribing")
    ln.add_argument("--model", default="base", help="faster-whisper model size (tiny/base/small/medium)")
    ln.add_argument("--device", default=None, help="input device index or name (default: system mic)")
    ln.set_defaults(func=cmd_listen)

    s = sub.add_parser("search", help="full-text search your screen history")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    ak = sub.add_parser("ask", help="ask your memory a question (spans screen + audio, cited)")
    ak.add_argument("question")
    ak.add_argument("--limit", type=int, default=8)
    ak.set_defaults(func=cmd_ask)

    cn = sub.add_parser("connections", help="entities connected to a person/project/app/file")
    cn.add_argument("entity")
    cn.add_argument("--limit", type=int, default=15)
    cn.set_defaults(func=cmd_connections)

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


def _force_utf8_output() -> None:
    """
    Windows consoles default to a legacy code page (e.g. cp1252) that raises
    UnicodeEncodeError on the non-ASCII text that OCR routinely produces.
    Reconfigure std streams to UTF-8 so printing search/recent/activity output
    never crashes. No-op where streams aren't reconfigurable.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

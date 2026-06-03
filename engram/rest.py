"""
rest.py — a small local REST API over the screen-memory store.

Binds to 127.0.0.1 only. Any agent or app on your machine can query your
screen history without speaking MCP. Endpoints:

    GET /health
    GET /search?q=<query>&limit=20
    GET /recent?minutes=30&limit=50
    GET /latest
    GET /activity?minutes=60
    GET /focus?minutes=60

Loopback-only by design — this is your memory, it does not leave the box.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from engram.store import get_store

log = logging.getLogger("engram.rest")


def _entries(rows):
    return [asdict(e) for e in rows]


class _Handler(BaseHTTPRequestHandler):
    def _send(self, payload, status=200):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet — we have our own logger

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        store = get_store()

        def arg(name, default, cast=str):
            try:
                return cast(q.get(name, [default])[0])
            except (TypeError, ValueError):
                return default

        try:
            if u.path == "/health":
                self._send({"ok": True, "service": "engram"})
            elif u.path == "/search":
                self._send(_entries(store.search(arg("q", ""), arg("limit", 20, int))))
            elif u.path == "/recent":
                self._send(_entries(store.get_recent(arg("minutes", 30, int), arg("limit", 50, int))))
            elif u.path == "/latest":
                latest = store.get_latest()
                self._send(asdict(latest) if latest else {})
            elif u.path == "/activity":
                self._send({"summary": store.get_activity_summary(arg("minutes", 60, int))})
            elif u.path == "/focus":
                self._send(store.get_focus_stats(arg("minutes", 60, int)))
            else:
                self._send({"error": "not found"}, status=404)
        except Exception as e:
            log.exception("request failed")
            self._send({"error": str(e)}, status=500)


def serve(host: str = "127.0.0.1", port: int = 7890) -> None:
    get_store()  # warm the singleton before first request
    httpd = HTTPServer((host, port), _Handler)
    print(f"engram: REST API on http://{host}:{port}  (loopback only)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nengram: REST API stopped.")
        httpd.shutdown()

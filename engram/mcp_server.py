"""
mcp_server.py — expose Engram's screen memory to any MCP client.

A focused tool surface over the store: search, recall recent context, the
latest frame, an activity summary, and focus stats. Point Claude Code (or any
MCP client) at `engram-mcp` and your agent can see what you've been doing
without you narrating it.

    {"mcpServers": {"engram": {"command": "engram-mcp"}}}
"""

from __future__ import annotations

import json
import asyncio
from dataclasses import asdict

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from engram.store import get_store

server = Server("engram")


def _dump(rows) -> str:
    return json.dumps([asdict(e) for e in rows], default=str, indent=2)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="engram_search",
            description="Full-text search across everything that's been on the screen. "
                        "Use to recall an error, a URL, a snippet, a conversation you saw earlier.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms (FTS5 syntax allowed)"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="engram_recent",
            description="The last N minutes of screen activity, newest first. "
                        "Use to catch up on what the user was just doing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "default": 30},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="engram_latest",
            description="The single most recent screen frame — what's on screen right now.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="engram_activity",
            description="A human-readable summary of the last N minutes: apps, projects, focus.",
            inputSchema={
                "type": "object",
                "properties": {"minutes": {"type": "integer", "default": 60}},
            },
        ),
        Tool(
            name="engram_focus_stats",
            description="Focus metrics for the last N minutes: app switches, time-on-task, scatter.",
            inputSchema={
                "type": "object",
                "properties": {"minutes": {"type": "integer", "default": 60}},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    store = get_store()
    args = arguments or {}

    if name == "engram_search":
        text = _dump(store.search(args["query"], args.get("limit", 20)))
    elif name == "engram_recent":
        text = _dump(store.get_recent(args.get("minutes", 30), args.get("limit", 50)))
    elif name == "engram_latest":
        latest = store.get_latest()
        text = json.dumps(asdict(latest), default=str, indent=2) if latest else "{}"
    elif name == "engram_activity":
        text = store.get_activity_summary(args.get("minutes", 60))
    elif name == "engram_focus_stats":
        text = json.dumps(store.get_focus_stats(args.get("minutes", 60)), default=str, indent=2)
    else:
        text = f"unknown tool: {name}"

    return [TextContent(type="text", text=text)]


async def _run() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()

# Engram

**Your computer's photographic memory.**

Engram watches your screen, reads the **text** off it with on-device OCR, redacts secrets before anything is written, and builds a full-text-searchable memory of everything you've seen — then hands that memory to any AI agent over REST or MCP.

**No screenshots are ever stored. Nothing leaves your machine.** It's what Microsoft Recall promised and what Rewind.ai abandoned, built local-first and open source.

```bash
pip install engram-memory
engram watch                      # start remembering
engram search "that postgres error"
```

---

## Why

Every AI assistant starts blind. You spend the first minutes of every session being a translator — "I'm in the marketplace repo, I just changed the payment path, here's the error from earlier." All of that was on your screen. The screen knew. You're transcribing reality to a tool standing right next to reality with its eyes closed.

Engram is the memory that ends the re-explanation tax. Your agent asks Engram what you were doing instead of asking you.

## What it does

- **Screen memory** — on-device OCR turns your screen into full-text-searchable history. Find the error, the URL, the snippet, the message you saw two hours ago.
- **Secrets never stored** — a privacy filter redacts passwords, tokens, keys, and PII *before* text is written. Password managers are skipped entirely.
- **Text only, never images** — Engram stores ~2KB of parsed text per frame. There is no screenshot album to leak.
- **AI-ready** — query your memory over a loopback REST API or an MCP server. Works with Claude, any MCP client, or anything that can hit `localhost`.
- **Encryption at rest** *(optional)* — Fernet AES with the key held in your OS keychain.
- **Local-first, no cloud, no account** — the whole thing runs on your machine. There's no server to breach because there's no server.

## Install

Engram is macOS-first today (it uses Apple's Vision framework for fast on-device OCR).

```bash
# Core
pip install engram-memory

# Recommended: native Apple Vision OCR (fast, on-device)
pip install "engram-memory[vision]"

# Optional: encryption at rest
pip install "engram-memory[encryption]"
```

No Apple Vision? Engram falls back to the `tesseract` CLI:

```bash
brew install tesseract
```

## Use

```bash
engram watch                          # capture loop — Ctrl-C to stop
engram watch --interval 5             # capture every 5s
engram watch --accurate               # slower, higher-quality OCR

engram search "kubernetes crashloop"  # full-text search your history
engram recent --minutes 30            # what was just on screen
engram activity --minutes 60          # human-readable summary of the last hour
engram stats                          # db size, frame count, time span
```

### Serve it to an AI

REST (loopback only):

```bash
engram serve                          # http://127.0.0.1:7890
curl "http://127.0.0.1:7890/search?q=postgres&limit=10"
```

MCP (Claude Code, or any MCP client) — add to your MCP config:

```json
{
  "mcpServers": {
    "engram": { "command": "engram-mcp" }
  }
}
```

Then your agent has tools: `engram_search`, `engram_recent`, `engram_latest`, `engram_activity`, `engram_focus_stats`.

### As a library

```python
from engram import get_store

store = get_store()
for hit in store.search("that stack trace"):
    print(hit.app_name, "—", hit.text[:200])
```

## How it works

```
 screencapture ──▶ OCR ──▶ OCR cleanup ──▶ privacy redact ──▶ SQLite + FTS5
 (temp jpg,        (Apple Vision /         (drop secrets        (text only,
  deleted          tesseract)               before storage)      ~2KB/frame)
  immediately)
                                                    │
                                   ┌────────────────┼────────────────┐
                                   ▼                                  ▼
                              REST (7890)                       MCP server
                              loopback only                   (Claude / clients)
```

- **Perceptual hashing** skips OCR on screens that haven't changed — most ticks do no work.
- **Threaded OCR** keeps the capture loop from stalling on a slow frame.
- **FTS5 + porter stemming** gives you ranked full-text search out of the box; a TF-IDF index adds semantic ranking.

## Privacy — the whole point

A screen-watching tool run by someone else's cloud is a surveillance product. Engram is the opposite by construction:

- **No images stored.** Ever. Only redacted text.
- **Secrets redacted before write**, and configured apps (password managers) are never captured.
- **No network.** The REST API binds to `127.0.0.1`. The MCP server speaks over stdio. Nothing phones home.
- **Optional encryption at rest** with an OS-keychain key.
- **Your data, your disk, your delete key** — it all lives under `~/.engram/`.

The architecture *is* the privacy stance. If you're going to give software eyes, the eyes have to be in a head you own.

## Configuration

`~/.engram/config.json`:

```json
{
  "ignore_apps": ["1Password", "Keychain Access", "Bitwarden"],
  "capture_interval": 10,
  "session_gap_minutes": 15
}
```

Edit it live — the watcher reloads it without a restart.

## Status

`0.1.0` — beta, macOS-first. The capture → OCR → redact → store → search → serve pipeline is the whole product and it runs today. Linux/Windows capture backends and a richer cognitive layer (flow detection, knowledge graph, focus coaching) are on the roadmap.

## License

MIT. Build on it, sell on it, fork it. Just keep the eyes in a head you own.

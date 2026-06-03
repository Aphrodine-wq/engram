"""
Engram — your computer's photographic memory.

Local-first screen OCR memory: captures the text on your screen, redacts
secrets before anything is stored, indexes it for full-text + semantic
search, and serves it to any AI agent over REST or MCP. No images are ever
saved. Everything stays on your machine.

    from engram import get_store
    store = get_store()
    for hit in store.search("that postgres error"):
        print(hit.app_name, hit.text)
"""

from engram.store import get_store, EyesStore, ScreenEntry, load_config, DATA_DIR, DB_PATH
from engram.capture import capture_frame, ScreenFrame

__all__ = [
    "get_store",
    "EyesStore",
    "ScreenEntry",
    "ScreenFrame",
    "capture_frame",
    "load_config",
    "DATA_DIR",
    "DB_PATH",
    "__version__",
]

__version__ = "0.1.0"

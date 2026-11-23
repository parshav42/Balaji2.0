"""Environment-driven local settings. No API keys required."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("BALAJI_DB", str(ROOT / "data" / "balaji.sqlite3"))).expanduser()
OLLAMA_URL = os.environ.get("BALAJI_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("BALAJI_MODEL", "qwen3:1.7b")
EMBED_MODEL = os.environ.get("BALAJI_EMBED_MODEL", "").strip()
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_HISTORY = 16


def validate_ollama_url() -> None:
    """Refuse remote Ollama endpoints to keep the default personal instance local."""
    from urllib.parse import urlsplit
    url = urlsplit(OLLAMA_URL)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("BALAJI_OLLAMA_URL must be a local HTTP address (localhost or 127.0.0.1).")

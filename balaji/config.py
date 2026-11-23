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

REMOTE_HOST = os.environ.get("BALAJI_REMOTE_HOST", "").lower().strip()
PROJECTS_FILE = os.environ.get("BALAJI_PROJECTS_FILE", "")
TIMEZONE = os.environ.get("BALAJI_TIMEZONE", "Asia/Kolkata")
GMAIL_CLIENT_FILE = os.environ.get("BALAJI_GMAIL_CLIENT_FILE", "")
GMAIL_REDIRECT_URI = os.environ.get("BALAJI_GMAIL_REDIRECT_URI", "")


def validate_remote_host() -> None:
    import re

    if REMOTE_HOST and not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+\.ts\.net", REMOTE_HOST):
        raise ValueError("BALAJI_REMOTE_HOST must be the full Tailscale Serve hostname, without a scheme or port.")

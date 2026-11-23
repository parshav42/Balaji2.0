"""Make a private SQLite snapshot before the additive v2 migration."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import tempfile

from . import config

SCHEMA_VERSION = 2


def backup_before_upgrade() -> Path | None:
    source = config.DB_PATH.resolve()
    if not source.is_file():
        return None
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live:
        if live.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return None
        parent = source.parent / "backups"
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or parent.stat().st_mode & 0o077:
            raise ValueError("The BALAJI backup directory must be private (mode 700).")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        folder = Path(tempfile.mkdtemp(prefix=f"before-v2-{stamp}-", dir=parent))
        destination = folder / source.name
        destination.touch(mode=0o600)
        with closing(sqlite3.connect(destination)) as backup:
            live.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("The SQLite snapshot did not pass integrity validation.")
        for name in (".env", ".env.example"):
            settings = config.ROOT / name
            if not settings.is_symlink() and settings.is_file():
                target = folder / name
                target.touch(mode=0o600)
                shutil.copyfile(settings, target)
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true", help="Back up and upgrade the configured database")
    args = parser.parse_args()
    if args.migrate:
        from . import storage
        backup = storage.init_db()
        print(f"Backup saved to {backup}" if backup else "Database is current or newly initialized.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Create a transactionally consistent SQLite backup without stopping Ember."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile


def backup_database(source: Path, destination: Path) -> None:
    source = source.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)

    try:
        source_uri = f"{source.as_uri()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
            with sqlite3.connect(temporary_path) as backup_db:
                source_db.backup(backup_db)
                result = backup_db.execute("PRAGMA quick_check").fetchone()
                if result != ("ok",):
                    raise RuntimeError(f"backup quick_check failed: {result!r}")
        os.replace(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} SOURCE_DB DESTINATION_DB", file=sys.stderr)
        return 2
    backup_database(Path(argv[1]), Path(argv[2]))
    print(f"SQLite snapshot ready: {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

import pytest


SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "sqlite_backup.py"
SPEC = importlib.util.spec_from_file_location("sqlite_backup", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
sqlite_backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sqlite_backup)


def test_backup_includes_committed_wal_data(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"

    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE memories (content TEXT NOT NULL)")
        connection.execute("INSERT INTO memories VALUES ('first')")
        connection.commit()
        connection.execute("INSERT INTO memories VALUES ('latest')")
        connection.commit()

        sqlite_backup.backup_database(source, destination)

    with sqlite3.connect(destination) as backup:
        assert backup.execute("SELECT content FROM memories ORDER BY rowid").fetchall() == [
            ("first",),
            ("latest",),
        ]
        assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_backup_refuses_to_overwrite_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (value INTEGER)")
    destination.write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        sqlite_backup.backup_database(source, destination)

    assert destination.read_text(encoding="utf-8") == "keep me"

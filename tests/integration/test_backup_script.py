from __future__ import annotations

import sqlite3

import pytest

from scripts.backup_db import create_backup


@pytest.mark.integration
def test_backup_script_creates_valid_sqlite_backup(tmp_path) -> None:  # type: ignore[no-untyped-def]
    src_db = tmp_path / "source.sqlite3"
    out_dir = tmp_path / "backups"

    with sqlite3.connect(str(src_db)) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample (value) VALUES (?)", ("backup-proof",))
        conn.commit()

    backup_path = create_backup(db_path=src_db, output_dir=out_dir, keep=5)

    assert backup_path.exists()
    assert backup_path.stat().st_size > 0

    with sqlite3.connect(str(backup_path)) as conn:
        row = conn.execute("SELECT value FROM sample WHERE id = 1").fetchone()
    assert row is not None
    assert str(row[0]) == "backup-proof"

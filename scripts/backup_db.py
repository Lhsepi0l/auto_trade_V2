from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable


def _iter_backups(output_dir: Path) -> Iterable[Path]:
    return sorted(output_dir.glob("auto_trader_backup_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)


def _enforce_retention(output_dir: Path, keep: int) -> int:
    removed = 0
    if keep <= 0:
        return removed
    for old in list(_iter_backups(output_dir))[keep:]:
        old.unlink(missing_ok=True)
        removed += 1
    return removed


def create_backup(*, db_path: Path, output_dir: Path, keep: int) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"DB file not found: {db_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = output_dir / f"auto_trader_backup_{stamp}.sqlite3"

    with sqlite3.connect(str(db_path)) as src_conn, sqlite3.connect(str(backup_path)) as dst_conn:
        src_conn.backup(dst_conn)

    _ = _enforce_retention(output_dir, keep)
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a timestamped SQLite backup with retention.")
    parser.add_argument("--db-path", default="./data/auto_trader.sqlite3", help="Source SQLite DB path")
    parser.add_argument("--output-dir", default="./backups", help="Backup directory")
    parser.add_argument("--keep", type=int, default=20, help="Number of newest backups to retain")
    args = parser.parse_args()

    backup_path = create_backup(
        db_path=Path(args.db_path).resolve(),
        output_dir=Path(args.output_dir).resolve(),
        keep=max(int(args.keep), 0),
    )
    print(f"BACKUP_OK {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

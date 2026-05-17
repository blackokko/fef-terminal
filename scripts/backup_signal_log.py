"""Daily backup of the FEF signal log.

Copies the SQLite DB to a dated file and exports a JSON snapshot for any
external analysis pipeline. Designed to be run as a Windows scheduled task
once a day (after market close).

Usage:
    python scripts/backup_signal_log.py [--dest DIR]

By default backs up to %USERPROFILE%\\.fef-terminal\\backups\\.

IMPROVEMENTS #23.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path.home() / ".fef-terminal" / "signal_log.db"
DEFAULT_BACKUP_DIR = Path.home() / ".fef-terminal" / "backups"


def backup(db_path: Path, dest_dir: Path) -> tuple[Path, Path]:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    db_dest = dest_dir / f"signal_log_{stamp}.db"
    json_dest = dest_dir / f"signal_log_{stamp}.json"

    shutil.copy2(db_path, db_dest)

    # JSON export of all 3 tables
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    payload = {}
    for table in ("signal_iter", "trade_ticket", "daily_session"):
        try:
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            payload[table] = rows
        except sqlite3.OperationalError:
            payload[table] = []
    conn.close()

    json_dest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    return db_dest, json_dest


def main() -> int:
    p = argparse.ArgumentParser(description="Back up the FEF signal log DB + JSON export")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"DB path (default: {DEFAULT_DB})")
    p.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help=f"Backup directory (default: {DEFAULT_BACKUP_DIR})",
    )
    args = p.parse_args()

    try:
        db_dest, json_dest = backup(args.db, args.dest)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    db_size = db_dest.stat().st_size
    json_size = json_dest.stat().st_size
    print(f"DB backup:   {db_dest}  ({db_size:,} bytes)")
    print(f"JSON export: {json_dest}  ({json_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

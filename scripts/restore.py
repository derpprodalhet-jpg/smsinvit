#!/usr/bin/env python3
"""Verify a backup and restore it into an explicitly selected empty database."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from database_tools import MaintenanceError, arguments, credentials, client_defaults, maintenance_lock, sha256_file


def restore(selected, source, binary="mysql"):
    source = Path(source).resolve()
    manifest = source.with_name(source.name + ".manifest.json")
    if not source.is_file() or not manifest.is_file():
        raise MaintenanceError("The backup and its manifest must both exist.")
    info = json.loads(manifest.read_text())
    if info.get("format") != "smsinvit-mysql-backup-v1" or sha256_file(source) != info.get("sha256") or source.stat().st_size != info.get("bytes"):
        raise MaintenanceError("Backup integrity verification failed; no restore was started.")
    # Our dumps deliberately omit CREATE DATABASE/USE; reject cross-database SQL.
    with source.open("r", encoding="utf-8", errors="strict") as input:
        for line in input:
            if re.match(r"\s*(?:USE\s|(?:CREATE|DROP|ALTER)\s+DATABASE\s)", line, flags=re.I):
                raise MaintenanceError("Cross-database statements are forbidden in restore files.")
    executable = shutil.which(binary)
    if executable is None:
        raise MaintenanceError("The mysql client is required for restoration; it is never installed automatically.")
    db = selected.connect()
    try:
        with maintenance_lock(db, selected.database):
            existing = db.all("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
            if existing:
                raise MaintenanceError("Restore target must be empty. Create a separate restore database; existing data is never overwritten.")
            with client_defaults(selected) as defaults, source.open("rb") as input:
                result = subprocess.run([executable, "--defaults-file=" + str(defaults), "--no-login-paths", "--binary-mode=1", "--default-character-set=utf8mb4", "--database=" + selected.database],
                                        stdin=input, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"}, check=False)
            if result.returncode:
                raise MaintenanceError("Restore failed; the target may be partial. Do not use it or retry over existing tables. Database error text is suppressed.")
            installed = {row["TABLE_NAME"] for row in db.all("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")}
            if installed != set(info.get("tables", [])):
                raise MaintenanceError("Restored table inventory differs from the backup manifest; do not switch application traffic.")
            from migrate import check_schema, migrations, validate_history
            history = validate_history(db, migrations()) if "flyway_schema_history" in installed else {}
            if history:
                check_schema(db, migrations()[:len(history)])
            return {"status": "restored_and_verified", "database": selected.database, "tables": len(installed), "sha256": info["sha256"]}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mysql", default="mysql", help="Explicit installed mysql executable.")
    args = parser.parse_args()
    try:
        print(json.dumps(restore(credentials(args), args.input, args.mysql), ensure_ascii=False))
    except MaintenanceError as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception:
        parser.exit(1, "Restore failed. Private connection and database errors are not printed.\n")


if __name__ == "__main__":
    main()

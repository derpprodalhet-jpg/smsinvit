#!/usr/bin/env python3
"""Create a private consistent MySQL dump with a checksum manifest."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

from database_tools import MaintenanceError, arguments, credentials, client_defaults, maintenance_lock, private_json, sha256_file


def backup(selected, output, binary="mysqldump"):
    output = Path(output).resolve()
    manifest = output.with_name(output.name + ".manifest.json")
    temporary = output.with_name(output.name + ".partial")
    if any(path.exists() for path in [output, manifest, temporary]):
        raise MaintenanceError("Backup output already exists; choose a new path.")
    executable = shutil.which(binary)
    if executable is None:
        raise MaintenanceError("mysqldump is required for backup; it is never installed automatically.")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = selected.connect()
    try:
        with maintenance_lock(db, selected.database):
            table_rows = db.all("SELECT TABLE_NAME,ENGINE,TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
            if any(row["TABLE_TYPE"] == "BASE TABLE" and row["ENGINE"] != "InnoDB" for row in table_rows):
                raise MaintenanceError("Consistent online backup requires all business tables to use InnoDB.")
            with client_defaults(selected) as defaults:
                args = [executable, "--defaults-file=" + str(defaults), "--no-login-paths", "--single-transaction", "--quick", "--routines", "--events", "--triggers",
                        "--set-gtid-purged=OFF", "--no-tablespaces", "--hex-blob", "--column-statistics=0", "--default-character-set=utf8mb4", selected.database]
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as destination:
                    result = subprocess.run(args, stdout=destination, stderr=subprocess.PIPE, env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"}, check=False)
                    destination.flush(); os.fsync(destination.fileno())
                if result.returncode:
                    raise MaintenanceError("mysqldump failed. Check backup privileges and private TLS/connection settings; stderr is suppressed to protect secrets.")
            if temporary.stat().st_size == 0:
                raise MaintenanceError("The dump was empty; no backup was published.")
            digest = sha256_file(temporary)
            info = {"format": "smsinvit-mysql-backup-v1", "database": selected.database,
                    "createdAt": datetime.now(timezone.utc).isoformat(), "sha256": digest, "bytes": temporary.stat().st_size,
                    "tables": sorted(row["TABLE_NAME"] for row in table_rows)}
            os.replace(temporary, output)
            private_json(manifest, info)
            return {"status": "complete", "output": str(output), "manifest": str(manifest), "sha256": digest}
    finally:
        temporary.unlink(missing_ok=True)
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser, default_user="smsinvit_backup")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mysqldump", default="mysqldump", help="Explicit installed mysqldump executable.")
    args = parser.parse_args()
    try:
        print(json.dumps(backup(credentials(args), args.output, args.mysqldump), ensure_ascii=False))
    except MaintenanceError as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception:
        parser.exit(1, "Backup failed. Private connection and database errors are not printed.\n")


if __name__ == "__main__":
    main()

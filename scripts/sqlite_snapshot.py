#!/usr/bin/env python3
"""Copy source SQLite bytes first; only the private copied database is opened."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from urllib.parse import quote

from database_tools import MaintenanceError, private_json, sha256_file


def fingerprint(path):
    stat = path.stat()
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, sha256_file(path))


def snapshot(source, destination, attempts=3):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise MaintenanceError("The source database file does not exist.")
    source_project = next((parent for parent in source.parents if parent.name == "codex_invit"), source.parent)
    if destination.exists() or destination == source_project or source_project in destination.parents:
        raise MaintenanceError("Snapshot destination must be new and outside the source project.")
    for attempt in range(attempts):
        destination.mkdir(parents=True, mode=0o700)
        copied = destination / source.name
        originals = [path for path in [source, Path(str(source) + "-wal"), Path(str(source) + "-shm"), Path(str(source) + "-journal")] if path.exists()]
        try:
            before = {path.name: fingerprint(path) for path in originals}
            for path in originals:
                target = destination / path.name
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with path.open("rb") as input, os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(input, output, length=1024 * 1024)
                    output.flush(); os.fsync(output.fileno())
            after_paths = [path for path in [source, Path(str(source) + "-wal"), Path(str(source) + "-shm"), Path(str(source) + "-journal")] if path.exists()]
            after = {path.name: fingerprint(path) for path in after_paths}
            if before != after or any(sha256_file(destination / name) != info[-1] for name, info in before.items()):
                raise MaintenanceError("Source files changed while copying.")
            # Never connect to source. WAL replay and any SHM writes happen in this copy.
            uri = "file:" + quote(str(copied), safe="/") + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as reader:
                if reader.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MaintenanceError("Copied SQLite integrity check failed.")
                consolidated = destination / "snapshot.sqlite3"
                if consolidated == copied:
                    consolidated = destination / "consolidated.sqlite3"
                fd = os.open(consolidated, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                with sqlite3.connect(consolidated) as output:
                    reader.backup(output)
            manifest = {"format": "smsinvit-sqlite-snapshot-v1", "sourceFiles": {name: info[-1] for name, info in before.items()},
                        "snapshot": consolidated.name, "sha256": sha256_file(consolidated), "sourceUnchangedDuringCopy": True}
            private_json(destination / "manifest.json", manifest)
            return {"status": "consistent_copy", "snapshot": str(consolidated), "sha256": manifest["sha256"]}
        except (MaintenanceError, FileNotFoundError, sqlite3.DatabaseError) as exc:
            shutil.rmtree(destination)
            if attempt + 1 == attempts:
                raise MaintenanceError("A stable consistent source snapshot could not be obtained. Quiesce source writers and retry; the source was never opened with SQLite or modified.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(snapshot(args.source, args.output_directory), ensure_ascii=False))
    except MaintenanceError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()

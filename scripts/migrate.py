#!/usr/bin/env python3
"""Explicit schema CLI: untouched V1–V6 and verified forward-only Python V7–V26."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import zlib

from database_tools import ROOT, MaintenanceError, arguments, credentials, maintenance_lock


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path

    @property
    def checksum(self):
        return flyway_checksum(self.path.read_bytes())


def flyway_checksum(raw):
    # Flyway ChecksumCalculator: BOM filtered from first line, no line endings,
    # UTF-8 CRC32 cast to signed Java int. Do not store the unsigned CRC32 value.
    source = raw.decode("utf-8-sig")
    checksum = 0
    for line in re.split(r"\r\n|\r|\n", source):
        checksum = zlib.crc32(line.encode("utf-8"), checksum)
    return checksum if checksum < 2**31 else checksum - 2**32


def split_sql(source):
    statements, output = [], []
    quote, line_comment, block_comment, depth = None, False, False, 0
    i = 0
    while i < len(source):
        char = source[i]
        following = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False; output.append(" ")
            i += 1; continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False; i += 2; output.append(" ")
            else:
                i += 1
            continue
        if quote:
            output.append(char)
            if char == "\\" and following:
                output.append(following); i += 2; continue
            if char == quote:
                if following == quote:
                    output.append(following); i += 2; continue
                quote = None
            i += 1; continue
        if char in "'\"`":
            quote = char; output.append(char); i += 1; continue
        if char == "#" or char == "-" and following == "-" and (i + 2 == len(source) or source[i + 2].isspace()):
            line_comment = True; i += 1 if char == "#" else 2; continue
        if char == "/" and following == "*":
            if i + 2 < len(source) and source[i + 2] == "!":
                raise MaintenanceError("Executable SQL comments require an explicitly supported migration parser.")
            block_comment = True; i += 2; continue
        if char == ";":
            statement = "".join(output).strip()
            if statement:
                statements.append(statement)
            output = []; i += 1; continue
        output.append(char); i += 1
    if quote or block_comment:
        raise MaintenanceError("Migration SQL has an unterminated quote or comment.")
    final = "".join(output).strip()
    if final:
        statements.append(final)
    return statements


def comma_parts(source):
    parts, start, depth, quote, i = [], 0, 0, None, 0
    while i < len(source):
        char = source[i]
        if quote:
            if char == "\\":
                i += 2; continue
            if char == quote:
                if i + 1 < len(source) and source[i + 1] == quote:
                    i += 2; continue
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(source[start:i].strip()); start = i + 1
        i += 1
    parts.append(source[start:].strip())
    return parts


def migrations():
    found = []
    for path in sorted((ROOT / "migrations" / "legacy").glob("V*.sql")) + sorted((ROOT / "migrations").glob("V*.sql")):
        match = re.fullmatch(r"V([0-9]+)__.+\.sql", path.name)
        if not match:
            raise MaintenanceError("Invalid migration filename.")
        found.append(Migration(int(match[1]), path))
    found.sort(key=lambda item: item.version)
    # 已随业务下线一并删除的迁移：V4/V5 与 V15/V16 是 X 模块，V18/V21/V23/V24 是
    # GPT 直充。它们对应的表与权限不再存在于任何环境，新库按剩余版本顺序建库即可。
    retired = {4, 5, 15, 16, 18, 21, 23, 24}
    expected = [version for version in range(1, 27) if version not in retired]
    if [item.version for item in found] != expected:
        raise MaintenanceError("This release requires exactly migrations V1-V26 minus the retired V4,V5,V15,V16,V18,V21,V23,V24.")
    return found


def upto(items, version):
    """Versions are no longer contiguous, so schema snapshots are taken by version."""
    return [item for item in items if item.version <= version]


def expected_schema(items):
    expected = {}
    skip = {"PRIMARY", "UNIQUE", "KEY", "INDEX", "CONSTRAINT", "FOREIGN", "CHECK"}
    def columns(parts, target, *, alter=False):
        for part in parts:
            if alter:
                part = re.sub(r"^ADD\s+(?:COLUMN\s+)?", "", part, flags=re.I)
            match = re.match(r"`?([A-Za-z0-9_]+)`?\s+([A-Za-z]+)(?:\s*\(([^)]+)\))?", part)
            if match and match[1].upper() not in skip:
                target[match[1]] = {"type": match[2].lower(), "size": match[3], "not_null": bool(re.search(r"\bNOT\s+NULL\b", part, flags=re.I))}
    for item in items:
        for statement in split_sql(item.path.read_text(encoding="utf-8-sig")):
            create = re.match(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([A-Za-z0-9_]+)`?\s*\((.*)\)\s*ENGINE", statement, flags=re.I | re.S)
            if create:
                target = expected.setdefault(create[1], {})
                columns(comma_parts(create[2]), target)
                continue
            alter = re.match(r"ALTER\s+TABLE\s+`?([A-Za-z0-9_]+)`?\s+(.*)", statement, flags=re.I | re.S)
            if alter and alter[1] in expected:
                columns([part for part in comma_parts(alter[2]) if re.match(r"ADD\s+(?:COLUMN\s+)?`?[a-z_]+`?\s+[A-Za-z]+", part, flags=re.I)], expected[alter[1]], alter=True)
    return expected


def check_schema(db, items):
    expected = expected_schema(items)
    rows = db.all("SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()")
    actual = {(row["TABLE_NAME"], row["COLUMN_NAME"]): row for row in rows}
    issues = []
    aliases = {"integer": "int", "numeric": "decimal", "boolean": "tinyint"}
    for table, columns in expected.items():
        for name, spec in columns.items():
            row = actual.get((table, name))
            if row is None:
                issues.append(table + "." + name + ":missing")
                continue
            kind = aliases.get(spec["type"], spec["type"])
            if row["DATA_TYPE"].lower() != kind:
                issues.append(table + "." + name + ":type")
            if kind in {"char", "varchar"} and spec["size"] and row["CHARACTER_MAXIMUM_LENGTH"] != int(spec["size"]):
                issues.append(table + "." + name + ":length")
            if spec["not_null"] and row["IS_NULLABLE"] != "NO":
                issues.append(table + "." + name + ":nullable")
    # Every still-present redemption business keeps its per-inventory uniqueness.
    unique_indexes = [
        ("invitation_v2_execution_permits", "PRIMARY"), ("invitation_v2_execution_permits", "invitation_v2_permit_email_unique"), ("invitation_v2_execution_permits", "invitation_v2_permit_owner_unique"),
        ("invitation_v2_sms_phone_uses", "PRIMARY"), ("invitation_v2_sms_budgets", "PRIMARY"), ("invitation_v2_manual_run_links", "PRIMARY"), ("invitation_v2_sms_orders", "invitation_v2_sms_order_activation"),
        ("invitation_v2_redemption_attempts", "PRIMARY"), ("invitation_v2_redemption_attempt_cards", "PRIMARY")]
    for table, name in unique_indexes:
        if table in expected and db.one("SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND INDEX_NAME=? AND NON_UNIQUE=0", table, name) is None:
            issues.append(table + ":unique-index:" + name)
    if 'invitation_v2_execution_permits' in expected:
        slots={row['slot_id'] for row in db.all('SELECT slot_id FROM invitation_v2_execution_permits')}
        if slots!=set(range(33)):issues.append('invitation_v2_execution_permits:fixed-slots')
    if issues:
        raise MaintenanceError("Schema validation failed: " + ", ".join(issues[:30]) + (" ..." if len(issues) > 30 else ""))
    return {"tables": len(expected), "columns": sum(map(len, expected.values()))}


HISTORY_DDL = """CREATE TABLE flyway_schema_history (
 installed_rank INT NOT NULL PRIMARY KEY, version VARCHAR(50), description VARCHAR(200) NOT NULL,
 type VARCHAR(20) NOT NULL, script VARCHAR(1000) NOT NULL, checksum INT,
 installed_by VARCHAR(100) NOT NULL, installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 execution_time INT NOT NULL, success TINYINT(1) NOT NULL,
 KEY flyway_schema_history_s_idx(success)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def tables(db):
    return {row["TABLE_NAME"] for row in db.all("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")}


def validate_history(db, items):
    installed = db.all("SELECT installed_rank,version,type,script,checksum,success FROM flyway_schema_history ORDER BY installed_rank")
    by_version = {}
    known = {str(item.version): item for item in items}
    for row in installed:
        if not row["success"]:
            raise MaintenanceError("A failed migration exists. Restore or explicitly repair it after review; automatic repair is forbidden.")
        if row["type"] != "SQL" or str(row["version"]) not in known:
            raise MaintenanceError("Unexpected or baseline-only Flyway history requires manual review.")
        version = int(row["version"])
        if version in by_version:
            raise MaintenanceError("Duplicate migration version in Flyway history.")
        item = known[str(version)]
        if row["script"] != item.path.name or row["checksum"] != item.checksum:
            raise MaintenanceError(f"Migration V{version} checksum or filename conflicts; no migration was applied.")
        by_version[version] = row
    if sorted(by_version) != [item.version for item in items[:len(by_version)]]:
        raise MaintenanceError("Flyway history is incomplete or out of order.")
    return by_version


def record(connection, item, rank, actor, execution_ms, success):
    description = item.path.stem.split("__", 1)[1].replace("_", " ")
    connection.exec_driver_sql("INSERT INTO flyway_schema_history(installed_rank,version,description,type,script,checksum,installed_by,execution_time,success) VALUES(%s,%s,%s,'SQL',%s,%s,%s,%s,%s)",
                               (rank, str(item.version), description, item.path.name, item.checksum, actor, execution_ms, success))
    connection.commit()


def run(selected, *, status=False, adopt_existing=False):
    items = migrations()
    db = selected.connect()
    try:
        with maintenance_lock(db, selected.database) as connection:
            present = tables(db)
            if "flyway_schema_history" in present:
                installed = validate_history(db, items)
                if installed:
                    check_schema(db, upto(items, items[len(installed) - 1].version))
            elif present:
                # Unrecorded legacy installs require an explicitly adopted full
                # V6 schema. Detect any later marker before validation so a
                # partially applied Python migration is never treated as older.
                markers={(row['TABLE_NAME'],row['COLUMN_NAME']) for row in db.all('SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()')}
                baseline=upto(items,6)
                previous=6
                for item in items:
                    if item.version <= 6:
                        continue
                    before=expected_schema(upto(items,previous));after=expected_schema(upto(items,item.version))
                    added={(table,column) for table,columns in after.items() for column in columns if column not in before.get(table,{})}
                    if added&markers:baseline=upto(items,item.version)
                    previous=item.version
                check_schema(db, baseline)
                if not adopt_existing:
                    if status:
                        return {"status": "verified_schema_without_history", "versions": [item.version for item in baseline], "requiresExplicitAdoption": True}
                    raise MaintenanceError("Verified schema exists without history; use --adopt-existing-schema to record it without changing business tables.")
                if status:
                    raise MaintenanceError("--status cannot also adopt a schema.")
                connection.exec_driver_sql(HISTORY_DDL); connection.commit()
                for item in baseline:
                    record(connection, item, item.version, selected.user, 0, True)
                installed = {item.version: {} for item in baseline}
            else:
                installed = {}
                if not status:
                    connection.exec_driver_sql(HISTORY_DDL); connection.commit()
            if status:
                return {"status": "ready" if len(installed) == len(items) else "migration_required", "installedVersions": sorted(installed), "pendingVersions": [item.version for item in items if item.version not in installed]}
            applied = []
            for item in items:
                if item.version in installed:
                    continue
                start = time.monotonic()
                try:
                    for statement in split_sql(item.path.read_text(encoding="utf-8-sig")):
                        connection.exec_driver_sql(statement)
                    connection.commit()
                    check_schema(db, upto(items, item.version))
                except Exception:
                    connection.rollback()
                    record(connection, item, item.version, selected.user, int((time.monotonic() - start) * 1000), False)
                    raise MaintenanceError(f"Migration V{item.version} failed; partial MySQL DDL may exist. Do not rerun automatically; restore the backup or review the failure.") from None
                record(connection, item, item.version, selected.user, int((time.monotonic() - start) * 1000), True)
                applied.append(item.version)
            return {"status": "ready", "appliedVersions": applied, "schema": check_schema(db, items)}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument("--status", action="store_true", help="Validate history and schema without modifying them.")
    parser.add_argument("--adopt-existing-schema", action="store_true", help="Explicitly record a completely verified pre-installed schema; never overwrite business tables.")
    args = parser.parse_args()
    try:
        print(json.dumps(run(credentials(args), status=args.status, adopt_existing=args.adopt_existing_schema), ensure_ascii=False))
    except MaintenanceError as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception:
        parser.exit(1, "Database maintenance failed. Check private connection settings and privileges; no driver secrets are printed.\n")


if __name__ == "__main__":
    main()

"""Offline V19 schema checks for the statistics cost columns and card indexes.

V19 is forward-only: it adds nullable columns (historical rows keep NULL and are
reported as unpriced) and two card-count indexes, and contains no statement that
reads or rewrites stored business data.
"""
import hashlib
from pathlib import Path
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database_tools import MaintenanceError
from migrate import check_schema, comma_parts, expected_schema, migrations, split_sql
from migration_fixtures import FROZEN_SHA256, SchemaMetadata


V19_VERSIONS = [1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14, 17, 19]
ADDED_COLUMNS = {
    'invitation_v2_sms_orders': {
        'activation_cost': {'type': 'decimal', 'size': '12,4', 'not_null': False},
        'currency': {'type': 'varchar', 'size': '8', 'not_null': False},
    },
}
ADDED_INDEXES = {
    ('invitation_v2_cdk_codes', 'invitation_v2_cdk_codes_created_idx'): ['created_at'],
    ('invitation_v2_cdk_codes', 'invitation_v2_cdk_codes_redeemed_idx'): ['redeemed_at'],
}
BEFORE_V19 = """
CREATE TABLE invitation_v2_sms_orders (
  rental_id TEXT PRIMARY KEY, run_id TEXT, platform TEXT, action TEXT, status TEXT,
  activation_id TEXT, phone_fingerprint TEXT, attempts INTEGER DEFAULT 0, created_at REAL, updated_at REAL);
CREATE TABLE invitation_v2_cdk_codes (
  code TEXT PRIMARY KEY, batch TEXT, status TEXT, created_at REAL, redeemed_at REAL, finished_at REAL);
"""


def v19():
    return next(item for item in migrations() if item.version == 19)


def through_v19():
    return [item for item in migrations() if item.version <= 19]


def sqlite_statements(source):
    """Translate the MySQL-only ADD KEY clause so SQLite can replay V19."""
    output = []
    for statement in split_sql(source):
        match = re.match(r'ALTER TABLE\s+`?(\w+)`?\s+(.*)$', statement, re.S)
        assert match, statement
        table, body = match[1], match[2]
        parts = [part.strip() for part in comma_parts(body)]
        columns = [re.sub(r'^ADD\s+(?:COLUMN\s+)?', '', part, flags=re.I) for part in parts
                   if re.match(r'ADD\s+(?:COLUMN\s+)?`?\w+`?\s+[A-Za-z]', part, flags=re.I)
                   and not re.match(r'ADD\s+(?:COLUMN\s+)?(?:KEY|INDEX|UNIQUE|CONSTRAINT|PRIMARY|FOREIGN|CHECK)\b', part, flags=re.I)]
        if columns:
            clean = [re.sub(r'\s+NULL\s*$', '', column, flags=re.I) for column in columns]
            # SQLite adds one column per ALTER TABLE statement.
            output.extend('ALTER TABLE %s ADD COLUMN %s' % (table, column) for column in clean)
        for part in parts:
            key = re.match(r'ADD\s+KEY\s+`?(\w+)`?\s*\(([^)]+)\)', part, re.I)
            if key:
                output.append('CREATE INDEX %s ON %s (%s)' % (key[1], table, key[2]))
    return output


def test_release_migrations_reach_v19_in_order():
    items = through_v19()
    assert [item.version for item in items] == V19_VERSIONS
    assert items[-1].path.name == 'V19__stats_cost_fields.sql'


def test_released_bytes_of_every_earlier_migration_stay_frozen():
    earlier = through_v19()[:-1]
    assert {item.path.name for item in earlier} <= set(FROZEN_SHA256)
    for item in earlier:
        assert hashlib.sha256(item.path.read_bytes()).hexdigest() == FROZEN_SHA256[item.path.name], item.path.name


def test_v19_adds_only_nullable_cost_columns_and_card_indexes():
    items = through_v19()
    before, after = expected_schema(items[:-1]), expected_schema(items)
    assert set(after) == set(before)
    for table, columns in before.items():
        for name, spec in columns.items():
            assert after[table][name] == spec, (table, name)
    for table, added in ADDED_COLUMNS.items():
        assert set(after[table]) == set(before[table]) | set(added)
        for name, spec in added.items():
            assert after[table][name] == spec, name
            assert spec['not_null'] is False


def test_v19_never_reads_or_rewrites_stored_business_data():
    statements = split_sql(v19().path.read_text())
    assert len(statements) == 2
    for statement in statements:
        assert statement.startswith('ALTER TABLE ')
        assert not any(word in statement for word in ('SELECT', 'UPDATE', 'DELETE', 'DROP', 'CREATE TABLE', 'INSERT'))
    assert sum(statement.count('ADD COLUMN') for statement in statements) == 2
    assert sum(statement.count('ADD KEY') for statement in statements) == 2


def test_v19_rolls_forward_over_populated_tables(tmp_path):
    """Historical rows survive V19 and read back NULL for the new cost columns."""
    connection = sqlite3.connect(':memory:')
    connection.executescript(BEFORE_V19)
    connection.execute("INSERT INTO invitation_v2_sms_orders(rental_id,run_id,status,activation_id,created_at,updated_at) VALUES('r1','run','created','aid',100.0,100.0)")
    connection.execute("INSERT INTO invitation_v2_cdk_codes(code,status,created_at,redeemed_at) VALUES('c1','used',50.0,60.0)")
    for statement in sqlite_statements(v19().path.read_text()):
        connection.execute(statement)
    assert connection.execute("SELECT run_id,status,activation_id,created_at,activation_cost,currency FROM invitation_v2_sms_orders").fetchall() == [('run', 'created', 'aid', 100.0, None, None)]
    assert connection.execute("SELECT code,redeemed_at FROM invitation_v2_cdk_codes").fetchall() == [('c1', 60.0)]
    indexes = {row[1] for row in connection.execute("PRAGMA index_list('invitation_v2_cdk_codes')")}
    assert {'invitation_v2_cdk_codes_created_idx', 'invitation_v2_cdk_codes_redeemed_idx'} <= indexes
    columns = {row[1] for row in connection.execute("PRAGMA table_info('invitation_v2_sms_orders')")}
    assert {'activation_cost', 'currency'} <= columns
    connection.close()


def test_schema_validation_still_works_across_the_v19_boundary():
    items = through_v19()
    complete = check_schema(SchemaMetadata(items), items)
    assert complete['tables'] == len(expected_schema(items))
    before, after = expected_schema(items[:-1]), expected_schema(items)
    for table in ADDED_COLUMNS:
        assert before[table] != after[table]
    with pytest.raises(MaintenanceError):
        check_schema(SchemaMetadata(items, missing_unique={('invitation_v2_execution_permits', 'invitation_v2_permit_email_unique')}), items)

"""Offline V17 schema checks for manual invitation task-hold review.

The review closure is the only production path that may release an uncertain
invitation task. These checks keep it forward-only and proof that it can never
rewrite a stored outcome: V17 adds nullable audit columns only, and contains no
statement that reads or writes task, job or card data.
"""
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrate import check_schema, expected_schema, migrations, split_sql
from migration_fixtures import FROZEN_SHA256, SchemaMetadata


V17_VERSIONS = [1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14, 17]
HOLD_AUDIT_COLUMNS = {
    'resolved_by': {'type': 'varchar', 'size': '64', 'not_null': False},
    'resolution': {'type': 'varchar', 'size': '32', 'not_null': False},
    'note': {'type': 'varchar', 'size': '160', 'not_null': False},
    'confirmed_executors_stopped': {'type': 'tinyint', 'size': '1', 'not_null': False},
}


def v17():
    return next(item for item in migrations() if item.version == 17)


def through_v17():
    return [item for item in migrations() if item.version <= 17]


def test_release_migrations_reach_v17_in_order():
    items = through_v17()
    assert [item.version for item in items] == V17_VERSIONS
    assert items[-1].path.name == 'V17__invitation_task_hold_review.sql'


def test_released_bytes_of_every_earlier_migration_stay_frozen():
    earlier = through_v17()[:-1]
    assert {item.path.name for item in earlier} <= set(FROZEN_SHA256)
    for item in earlier:
        assert hashlib.sha256(item.path.read_bytes()).hexdigest() == FROZEN_SHA256[item.path.name], item.path.name


def test_v17_adds_only_nullable_review_columns_and_no_new_table():
    items = through_v17()
    before, after = expected_schema(items[:-1]), expected_schema(items)
    assert set(after) == set(before)
    for table, columns in before.items():
        for name, spec in columns.items():
            if table == 'invitation_v2_task_holds':
                continue
            assert after[table][name] == spec, (table, name)
    holds_before = before['invitation_v2_task_holds']
    holds_after = after['invitation_v2_task_holds']
    assert set(holds_after) == set(holds_before) | set(HOLD_AUDIT_COLUMNS)
    for name, spec in holds_before.items():
        assert holds_after[name] == spec, name
    for name, spec in HOLD_AUDIT_COLUMNS.items():
        assert holds_after[name] == spec, name


def test_v17_never_touches_stored_task_job_or_card_outcomes():
    statements = split_sql(v17().path.read_text())
    assert len(statements) == 1
    statement = statements[0]
    assert statement.startswith('ALTER TABLE invitation_v2_task_holds')
    for forbidden in ('SELECT', 'UPDATE', 'DELETE', 'INSERT', 'DROP', 'CREATE TABLE',
                      'invitation_v2_cdk_jobs', 'invitation_v2_cdk_codes', 'resolved_at'):
        assert forbidden not in statement, forbidden
    assert statement.count('ADD COLUMN') == len(HOLD_AUDIT_COLUMNS)


def test_schema_validation_still_works_across_the_v17_boundary():
    items = through_v17()
    complete = check_schema(SchemaMetadata(items), items)
    assert complete['tables'] == len(expected_schema(items))
    incomplete_before, incomplete_after = expected_schema(items[:-1]), expected_schema(items)
    assert incomplete_before['invitation_v2_task_holds'] != incomplete_after['invitation_v2_task_holds']

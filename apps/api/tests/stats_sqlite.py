"""SQLite harness for the statistics SQL: real isolated transactions, MySQL syntax shim.

Only the tables and columns the statistics mappers touch are created. The MySQL
dialect pieces used by StatsMapper are translated here (`NOW(3)`,
`CAST(... AS SIGNED)`) so the production SQL runs unmodified against SQLite.
"""
from contextlib import contextmanager
from datetime import datetime
import re
import sqlite3

from sqlalchemy import create_engine

from smsinvit.db import Database


# Python 3.12 deprecates sqlite3's implicit datetime adapter. Register the same
# explicit MySQL DATETIME form that test_admin_cdk_detail_and_stop.py already
# installs, so the two registrations stay byte-compatible and bound window
# bounds compare with stored rows.
sqlite3.register_adapter(datetime, lambda value: value.strftime('%Y-%m-%d %H:%M:%S.%f'))


SCHEMA = """
CREATE TABLE sms_redeem_keys (
  id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'UNUSED', created_at TEXT, used_at TEXT,
  failed_at TEXT, expires_at TEXT);
CREATE TABLE sms_sessions (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, activation_cost NUMERIC, completed_at TEXT, updated_at TEXT);
CREATE TABLE invitation_v2_cdk_codes (
  code TEXT PRIMARY KEY, status TEXT, created_at REAL, redeemed_at REAL);
CREATE TABLE invitation_v2_sms_orders (
  rental_id TEXT PRIMARY KEY, run_id TEXT, platform TEXT, status TEXT NOT NULL, created_at REAL NOT NULL,
  updated_at REAL NOT NULL, activation_cost NUMERIC, currency TEXT);
CREATE TABLE invitation_runs (
  id TEXT PRIMARY KEY, status TEXT, invitation_status TEXT, verified_at TEXT);
CREATE TABLE inventory_items (
  id TEXT PRIMARY KEY, created_at TEXT);
CREATE TABLE durable_jobs (
  id TEXT PRIMARY KEY, status TEXT);
CREATE TABLE admin_ip_bans (
  ip_hash TEXT PRIMARY KEY, ip_address TEXT NOT NULL, failure_count INTEGER NOT NULL DEFAULT 0,
  first_failure_at DATETIME NOT NULL, last_failure_at DATETIME NOT NULL, last_identifier TEXT,
  banned_until DATETIME, ban_reason TEXT, created_at DATETIME, updated_at DATETIME);
"""


class StatsSqliteDatabase(Database):
    def _prepare(self, sql, params):
        sql = sql.replace('NOW(3)', 'CURRENT_TIMESTAMP')
        return super()._prepare(sql, params)


def isolated_stats_database(path):
    """Create an isolated statistics database; SQLite supplies write locking."""
    engine = create_engine('sqlite:///' + str(path),
                           connect_args={'check_same_thread': False, 'timeout': 30},
                           pool_size=5, max_overflow=5, hide_parameters=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
    return StatsSqliteDatabase(engine=engine)


@contextmanager
def schema_only():
    """In-memory schema for metadata-only checks."""
    connection = sqlite3.connect(':memory:')
    connection.executescript(SCHEMA)
    try:
        yield connection
    finally:
        connection.close()

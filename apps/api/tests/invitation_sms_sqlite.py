"""MySQL syntax adapter over real, isolated SQLite transactions for SMS tests."""
from pathlib import Path
import re
import sqlite3

from sqlalchemy import create_engine

from smsinvit.db import Database


ROOT = Path(__file__).resolve().parents[3]


class SmsSqliteDatabase(Database):
    def _prepare(self, sql, params):
        sql = sql.replace(' FOR UPDATE', '').replace('INSERT IGNORE', 'INSERT OR IGNORE')
        sql = sql.replace('ON DUPLICATE KEY UPDATE fingerprint=VALUES(fingerprint)',
                          'ON CONFLICT(fingerprint) DO NOTHING')
        sql = sql.replace('GREATEST(', 'MAX(')
        return super()._prepare(sql, params)


def _v19_sms_order_columns():
    """Replay V19's invitation_v2_sms_orders columns onto the V9 SQLite schema.

    The harness loads V9 only, so the later cost columns that sms_state writes
    are derived from the real migration instead of being duplicated here.
    """
    source = (ROOT / 'migrations/V19__stats_cost_fields.sql').read_text()
    statements = []
    for statement in re.split(r';', source):
        alter = re.search(r'ALTER TABLE\s+`?(\w+)`?\s+(.*)$', statement, re.S)
        if not alter or alter[1] != 'invitation_v2_sms_orders':
            continue
        body = alter[2].strip()
        assert body.startswith('ADD COLUMN '), body
        for column in re.split(r',\s*ADD\s+COLUMN\s+', body[len('ADD COLUMN '):]):
            statements.append('ALTER TABLE invitation_v2_sms_orders ADD COLUMN ' + ' '.join(column.split()))
    return statements


def isolated_sms_database(path):
    """Use V9's actual constraints; SQLite supplies database-wide write locking."""
    schema = (ROOT / 'migrations/V9__invitation_sms_and_manual_completion.sql').read_text()
    schema = re.sub(r'\) ENGINE=InnoDB[^;]*', ')', schema)
    schema = re.sub(r'UNIQUE KEY \w+\s*(\([^\n]+\))', r'UNIQUE \1', schema)
    schema = re.sub(r'^\s*KEY \w+[^\n]*\n', '', schema, flags=re.M)
    schema = re.sub(r',\s*\)', '\n)', schema)
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.executescript(schema)
        for statement in _v19_sms_order_columns():
            connection.execute(statement)
        connection.executescript('''
            CREATE TABLE invitation_v2_secret_values (
              fingerprint TEXT PRIMARY KEY, ciphertext TEXT NOT NULL,
              plaintext_length INTEGER NOT NULL
            );
            CREATE TABLE invitation_v2_settings (key TEXT PRIMARY KEY, value TEXT);
        ''')
    engine = create_engine('sqlite:///' + str(path),
                           connect_args={'check_same_thread': False, 'timeout': 30},
                           pool_size=20, max_overflow=20, hide_parameters=True)
    return SmsSqliteDatabase(engine=engine)

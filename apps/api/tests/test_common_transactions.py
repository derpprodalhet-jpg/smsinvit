from contextvars import copy_context
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import create_engine
from smsinvit.db import Database

@pytest.fixture
def database(tmp_path):
    db=Database(engine=create_engine('sqlite:///'+str(tmp_path/'transactions.db')))
    db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,value TEXT)')
    yield db
    db.close()

def test_outer_rollback_includes_nested_required_writes(database):
    with pytest.raises(ValueError):
        with database.transaction():
            database.execute('INSERT INTO records VALUES(?,?)',1,'outer')
            with database.transaction():database.execute('INSERT INTO records VALUES(?,?)',2,'inner')
            raise ValueError('request fails')
    assert database.all('SELECT * FROM records')==[]

def test_caught_inner_failure_still_prevents_outer_commit(database):
    with pytest.raises(RuntimeError,match='rollback-only'):
        with database.transaction():
            database.execute('INSERT INTO records VALUES(?,?)',1,'outer')
            try:
                with database.transaction():
                    database.execute('INSERT INTO records VALUES(?,?)',2,'inner');raise ValueError('failure')
            except ValueError:pass
    assert database.all('SELECT * FROM records')==[]

def test_requires_new_commit_survives_outer_failure(database):
    with pytest.raises(ValueError):
        with database.transaction():
            with database.transaction(requires_new=True):database.execute('INSERT INTO records VALUES(?,?)',1,'durable')
            raise ValueError('outer failed')
    assert database.one('SELECT value FROM records WHERE id=?',1)['value']=='durable'

def test_requires_new_failure_does_not_rollback_outer(database):
    with database.transaction():
        try:
            with database.transaction(requires_new=True):database.execute('INSERT INTO records VALUES(?,?)',1,'discard');raise ValueError('new fails')
        except ValueError:pass
        database.execute('INSERT INTO records VALUES(?,?)',2,'outer')
    assert database.all('SELECT * FROM records')==[{'id':2,'value':'outer'}]

def test_connection_context_cannot_cross_concurrent_threads(database):
    with database.transaction():
        inherited=copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(inherited.run,database.all,'SELECT * FROM records')
            with pytest.raises(RuntimeError,match='concurrent'):future.result()

def test_binding_never_substitutes_literal_question_marks(database):
    database.execute('INSERT INTO records VALUES(?,?)',1,"x' OR 1=1 --")
    assert database.one("SELECT '?' AS literal,value FROM records WHERE id=%s",1)=={'literal':'?','value':"x' OR 1=1 --"}
    assert database.one('SELECT * FROM records WHERE value=?','no match') is None

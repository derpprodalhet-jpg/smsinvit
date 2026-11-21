"""Manual completion state and rollback tests; no provider or live database calls."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from smsinvit.errors import Error
from smsinvit.invitation import mysql
from smsinvit.invitation.service import InvitationService


class LocalDatabase:
    """Real SQLite transactions; only MySQL row-lock syntax is omitted here."""
    def __init__(self):
        self.con = sqlite3.connect(':memory:', isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.depth = 0

    @property
    def in_transaction(self):return self.depth > 0

    @contextmanager
    def transaction(self):
        outer = self.depth == 0
        if outer:
            self.con.execute('BEGIN')
        self.depth += 1
        try:
            yield self
        except BaseException:
            if outer:
                self.con.rollback()
            raise
        else:
            if outer:
                self.con.commit()
        finally:
            self.depth -= 1

    def execute(self, sql, *args):
        return self.con.execute(sql.replace(' FOR UPDATE', ''), args)

    def all(self, sql, *args):
        return [dict(row) for row in self.execute(sql, *args)]

    def one(self, sql, *args):
        rows = self.all(sql, *args)
        return rows[0] if rows else None

    def scalar(self, sql, *args):
        row = self.one(sql, *args)
        return next(iter(row.values())) if row else None


class LocalRepository:
    def __init__(self, database):
        self.db = database
        self.finish_count = 0
        self.event_failure = False

    def create_cdk_job(self, job, code, email='', attempt=1):
        self.db.execute("INSERT INTO invitation_v2_cdk_jobs(job_id,code,email,attempt,status,created_at) VALUES (?,?,?,?,'queued',999)",
                        mysql.protected(job), mysql.marker(code), email, attempt)

    def set_cdk_job(self, job, **fields):
        self.db.execute('UPDATE invitation_v2_cdk_jobs SET ' + ','.join(key + '=?' for key in fields) + ' WHERE job_id=?',
                        *fields.values(), mysql.marker(job))

    def set_cdk_status(self, code, status, **fields):
        values = {'status': status, **fields}
        result = self.db.execute('UPDATE invitation_v2_cdk_codes SET ' + ','.join(key + '=?' for key in values) + ' WHERE code=?',
                                *[mysql.protected(value) if key == 'result_json' else value for key, value in values.items()], mysql.marker(code))
        return result.rowcount > 0

    def get_cdk_code(self, code):
        return self.db.one('SELECT * FROM invitation_v2_cdk_codes WHERE code=?',mysql.marker(code))

    def add_cdk_event(self, code, job, event, detail):
        if self.event_failure:
            raise ConnectionError('injected event write failure')
        self.db.execute('INSERT INTO invitation_v2_cdk_events(code,job_id,event,detail) VALUES (?,?,?,?)',
                        mysql.marker(code), mysql.marker(job), event, mysql.protected(detail))

    def finish_run(self, run_id, status, error='', category=''):
        self.finish_count += 1
        self.db.execute('UPDATE invitation_v2_runs SET status=?,finished_at=300,error=?,error_category=? WHERE run_id=?',
                        status, mysql.protected(error), category, mysql.marker(run_id))


class LocalOutbox:
    """Durable local UNIQUE(event_key) writes share the actual card transaction."""
    def __init__(self, db):
        self.db = db
        self.chat = '-100123'
        self.failure = False

    def value(self, name):
        assert name == 'telegram_ops_chat_id'
        return self.chat

    def enqueue(self, event, channel, kind, reference, payload, expires):
        assert self.db.in_transaction
        code_id = event.removeprefix('invitation.card-redeemed:')
        assert self.db.scalar('SELECT status FROM invitation_v2_cdk_codes WHERE code=?',code_id) == 'done'
        self.db.execute('INSERT OR IGNORE INTO completion_outbox VALUES (?,?,?,?,?,?)',
                        event,channel,kind,reference,json.dumps(payload),expires)
        if self.failure:raise ConnectionError('injected outbox write failure')
        return event


@pytest.fixture
def inv(monkeypatch):
    secrets = {}

    def marker(value):
        if not value or str(value).startswith('iv2$'):
            return value
        return 'iv2$' + hashlib.sha256(str(value).encode()).hexdigest()

    def protected(value, **kwargs):
        key = marker(value)
        if key:
            secrets[key] = str(value)
        return key

    monkeypatch.setattr(mysql, 'marker', marker)
    monkeypatch.setattr(mysql, 'protected', protected)
    monkeypatch.setattr(mysql, 'revealed', lambda value, **kwargs: secrets.get(value, value))
    s = InvitationService.__new__(InvitationService)
    s._executor_lock = threading.RLock()
    s.db = LocalDatabase()
    s.repo = LocalRepository(s.db)
    s.s = SimpleNamespace(db=s.db,invitation=s,outbox=LocalOutbox(s.db))
    monkeypatch.setattr(mysql,'services',lambda:s.s)
    s.repo.set_cdk_status = mysql.transactional(s.repo.set_cdk_status)
    s.db.con.executescript('''
      CREATE TABLE invitation_v2_outlook_accounts(email TEXT PRIMARY KEY,cdk_code TEXT,cdk_bound_at REAL);
      CREATE TABLE invitation_v2_cdk_codes(code TEXT PRIMARY KEY,email TEXT,status TEXT,redeemed_at REAL,finished_at REAL,invite_processed_at REAL,error TEXT,result_json TEXT,fail_count INTEGER DEFAULT 0);
      CREATE TABLE invitation_v2_runs(run_id TEXT PRIMARY KEY,email TEXT,kind TEXT,status TEXT,started_at REAL,finished_at REAL,error TEXT,error_category TEXT);
      CREATE TABLE invitation_v2_codex_auth(email TEXT PRIMARY KEY,account_id TEXT,cli_completed_account_id TEXT,invite_processed_at REAL,cli_completed_at REAL);
      CREATE TABLE invitation_v2_cdk_jobs(job_id TEXT PRIMARY KEY,code TEXT,email TEXT,attempt INTEGER,status TEXT,stage TEXT,progress INTEGER,created_at REAL,started_at REAL,finished_at REAL,error TEXT);
      CREATE UNIQUE INDEX active_card_job ON invitation_v2_cdk_jobs(code) WHERE status IN ('queued','running');
      CREATE TABLE invitation_v2_cdk_events(code TEXT,job_id TEXT,event TEXT,detail TEXT);
      CREATE TABLE invitation_v2_task_holds(job_id TEXT PRIMARY KEY,resolved_at REAL);
      CREATE TABLE invitation_v2_platform_keys(code_id TEXT,inventory_item_id TEXT,expires_at REAL);
      CREATE TABLE inventory_items(id TEXT PRIMARY KEY,status TEXT);
      CREATE TABLE invitation_v2_execution_permits(slot_id INTEGER PRIMARY KEY,email TEXT,owner_token TEXT);
      CREATE TABLE invitation_v2_manual_run_links(run_id TEXT PRIMARY KEY,code_id TEXT,completed_job_id TEXT,created_at REAL,sync_status TEXT DEFAULT 'pending',sync_error TEXT);
      CREATE TABLE completion_outbox(event_key TEXT PRIMARY KEY,channel TEXT,kind TEXT,reference_id TEXT,payload TEXT,expires_at REAL);
    ''')
    s.email = 'manual@example.test'
    s.code = 'PRIVATE-CARD'
    s.run_id = 'aabbccddeeff'
    m = protected(s.code)
    s.db.execute('INSERT INTO invitation_v2_outlook_accounts VALUES (?,?,10)', s.email, m)
    s.db.execute("INSERT INTO invitation_v2_cdk_codes(code,email,status,redeemed_at,finished_at,invite_processed_at,error,result_json) VALUES (?,?,'failed',10,150,90,'original failure',NULL)", m, s.email)
    s.db.execute("INSERT INTO invitation_v2_runs VALUES (?,?,'codex_auth','running',100,NULL,NULL,NULL)", protected(s.run_id), s.email)
    s.db.execute('INSERT INTO invitation_v2_codex_auth VALUES (?,\'account-1\',\'account-1\',90,200)', s.email)
    s.db.execute("INSERT INTO inventory_items VALUES ('item-1','ISSUED')")
    s.db.execute("INSERT INTO invitation_v2_platform_keys VALUES (?,'item-1',NULL)", m)
    s.db.execute("INSERT INTO invitation_v2_cdk_jobs(job_id,code,email,status,created_at) VALUES (?,?,?,'failed',20)",
                 protected('old-failed-job'), m, s.email)
    yield s
    s.db.con.close()


def success():
    return {'ok': True, 'result': {'account_id': 'account-1', 'export': {'attempted': False}}}


def test_success_is_bound_before_business_and_finishes_once_preserving_history(inv):
    calls = []

    def original(email, *, run_id):
        calls.append(email)
        assert inv._manual_link(run_id)['code_id'] == mysql.marker(inv.code)
        return success()

    result = inv.managed_manual_authorizer(original)(inv.email, run_id=inv.run_id)
    assert result['ok'] and result['cdk_sync_status'] == 'done' and result['_run_finalized']
    assert calls == [inv.email] and inv.repo.finish_count == 1
    assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes') == 'done'
    assert inv.db.scalar('SELECT error FROM invitation_v2_cdk_codes') is None
    assert inv.db.scalar("SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE status='failed'") == 1
    assert inv.db.scalar("SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE status='done' AND progress=100") == 1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_events') == 1
    assert inv.db.scalar('SELECT status FROM inventory_items') == 'ISSUED'
    stored = mysql.revealed(inv.db.scalar('SELECT result_json FROM invitation_v2_cdk_codes'))
    assert json.loads(stored)['source_run_id'] == inv.run_id
    repeated = inv.finalize_manual_authorization(inv.email, inv.run_id, success())
    assert repeated['cdk_sync_status'] == 'done' and inv.repo.finish_count == 1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 2
    assert inv.db.scalar('SELECT COUNT(*) FROM completion_outbox') == 1


def test_unbound_email_completes_run_without_changing_cards(inv):
    inv.db.execute('UPDATE invitation_v2_outlook_accounts SET cdk_code=NULL')
    result = inv.managed_manual_authorizer(lambda *a, **kw: success())(inv.email, run_id=inv.run_id)
    assert result['ok'] and result['cdk_sync_status'] == 'unbound'
    assert inv.db.scalar('SELECT status FROM invitation_v2_runs') == 'done'
    assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes') == 'failed'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 1
    assert inv.db.scalar('SELECT COUNT(*) FROM completion_outbox') == 0


@pytest.mark.parametrize('change', ['binding', 'inventory', 'hold', 'active_job', 'account', 'stale_cli'])
def test_card_sync_rejection_preserves_authorization_success_and_never_repeats_business(inv, change):
    calls = []

    def business(*args, **kwargs):
        calls.append(1)
        if change == 'binding':
            inv.db.execute('UPDATE invitation_v2_outlook_accounts SET cdk_code=?', mysql.protected('OTHER-CARD'))
        elif change == 'inventory':
            inv.db.execute("UPDATE inventory_items SET status='INVALID'")
        elif change == 'hold':
            inv.db.execute('INSERT INTO invitation_v2_task_holds VALUES (?,NULL)', mysql.marker('old-failed-job'))
        elif change == 'active_job':
            inv.db.execute("UPDATE invitation_v2_cdk_jobs SET status='queued'")
        elif change == 'account':
            inv.db.execute("UPDATE invitation_v2_codex_auth SET cli_completed_account_id='another-account'")
        elif change == 'stale_cli':
            inv.db.execute('UPDATE invitation_v2_codex_auth SET cli_completed_at=99')
        return success()

    result = inv.managed_manual_authorizer(business)(inv.email, run_id=inv.run_id)
    assert result['ok'] and result['cdk_sync_status'] == 'failed'
    assert result['cdk_sync_error'] and calls == [1]
    assert inv.db.scalar('SELECT status FROM invitation_v2_runs') == 'done'
    assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes') == 'failed'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 1
    assert inv._manual_link(inv.run_id)['completed_job_id'] is None
    assert inv.db.scalar('SELECT COUNT(*) FROM completion_outbox') == 0


def test_card_event_write_failure_rolls_back_card_and_job_but_completes_run(inv):
    inv.repo.event_failure = True
    result = inv.managed_manual_authorizer(lambda *a, **kw: success())(inv.email, run_id=inv.run_id)
    assert result['ok'] and result['cdk_sync_status'] == 'failed'
    assert inv.db.scalar('SELECT status FROM invitation_v2_runs') == 'done'
    assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes') == 'failed'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_events') == 0
    assert inv._manual_link(inv.run_id)['completed_job_id'] is None
    assert inv.db.scalar('SELECT COUNT(*) FROM completion_outbox') == 0


def test_persistent_sync_storage_failure_returns_upstream_success_for_run_only_fallback(inv,monkeypatch):
    inv.begin_manual_authorization(inv.email, inv.run_id)
    original = inv.db.execute
    def unavailable(sql, *args):
        if sql.startswith('UPDATE invitation_v2_manual_run_links'):
            raise ConnectionError('fixture sync persistence unavailable')
        return original(sql, *args)
    monkeypatch.setattr(inv.db, 'execute', unavailable)
    result = inv.finalize_manual_authorization(inv.email, inv.run_id, success())
    assert result['ok'] and result['cdk_sync_status'] == 'failed' and not result['_run_finalized']
    assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes') == 'failed'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 1


@pytest.mark.parametrize('raised', [False, True])
def test_failed_business_finishes_only_failed_run(inv, raised):
    def business(*args, **kwargs):
        if raised:
            raise RuntimeError('upstream failed')
        return {'ok': False, 'reason': 'upstream failed'}

    result = inv.managed_manual_authorizer(business)(inv.email, run_id=inv.run_id)
    assert not result['ok'] and result['_run_finalized']
    assert inv.db.scalar('SELECT status FROM invitation_v2_runs') == 'failed'
    assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes') == 'failed'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 1
    assert inv.repo.finish_count == 1


def test_invalid_card_rejected_before_business_and_association_creation(inv):
    inv.db.execute("UPDATE inventory_items SET status='INVALID'")
    calls = []
    with pytest.raises(Error, match='停用'):
        inv.managed_manual_authorizer(lambda *a, **kw: calls.append(1))(inv.email, run_id=inv.run_id)
    assert not calls and inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_manual_run_links') == 0


def test_preview_does_not_write_and_historical_apply_is_idempotent(inv):
    inv.db.execute("UPDATE invitation_v2_runs SET status='done',finished_at=300")
    preview = inv.reconcile_manual_completion(inv.email, inv.run_id)
    assert preview['eligible'] and not preview['applied']
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_manual_run_links') == 0
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 1
    assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes') == 'failed'
    assert inv.reconcile_manual_completion(inv.email, inv.run_id, apply=True)['applied']
    assert inv.reconcile_manual_completion(inv.email, inv.run_id, apply=True)['applied']
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 2
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_events') == 1
    assert inv.db.scalar('SELECT finished_at FROM invitation_v2_runs') == 300
    assert inv.repo.finish_count == 0


@pytest.mark.parametrize('change', ['running_run', 'wrong_email', 'new_binding', 'new_failure', 'active_permit'])
def test_historical_repair_requires_same_run_binding_and_idle_mailbox(inv, change):
    inv.db.execute("UPDATE invitation_v2_runs SET status='done',finished_at=300")
    if change == 'running_run':
        inv.db.execute("UPDATE invitation_v2_runs SET status='running'")
    elif change == 'wrong_email':
        inv.db.execute("UPDATE invitation_v2_runs SET email='other@example.test'")
    elif change == 'new_binding':
        inv.db.execute('UPDATE invitation_v2_outlook_accounts SET cdk_bound_at=250')
    elif change == 'new_failure':
        inv.db.execute('UPDATE invitation_v2_cdk_codes SET finished_at=250')
    elif change == 'active_permit':
        inv.db.execute("INSERT INTO invitation_v2_execution_permits VALUES (1,?,'owner')", inv.email)
    result = inv.reconcile_manual_completion(inv.email, inv.run_id, apply=True)
    assert not result['eligible'] and not result['applied']
    assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes') == 'failed'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 1


def test_manual_permit_blocks_enqueue_before_controller_or_queue_action(inv):
    calls = []
    controller = SimpleNamespace(_lock=threading.RLock(), enqueue=lambda code: calls.append(code),
                                 _q=SimpleNamespace(put=lambda value: calls.append(value)))
    inv.cdk = SimpleNamespace(CONTROLLER=controller)
    inv.db.execute("INSERT INTO invitation_v2_execution_permits VALUES (1,?,'owner')", inv.email)
    inv._secure_controller()
    with pytest.raises(Error) as error:
        controller.enqueue(inv.code)
    assert error.value.code == 'INVITATION_RUNNING' and not calls


def test_run_wire_fields_show_sync_failure_without_exposing_card(inv):
    inv.begin_manual_authorization(inv.email, inv.run_id)
    inv.repo.event_failure = True
    inv.finalize_manual_authorization(inv.email, inv.run_id, success())
    row = inv.attach_manual_sync_status({'items': [{'run_id': inv.run_id, 'kind': 'codex_auth', 'status': 'done'}]})['items'][0]
    assert row['status'] == 'done' and row['cdk_sync_status'] == 'failed' and row['cdk_sync_error']
    assert inv.code not in json.dumps(row) and 'code_id' not in row


@pytest.mark.parametrize('manual', [False, True])
@pytest.mark.parametrize('failure', ['unknown', 'budget', 'wrapped_unknown'])
def test_rental_uncertainty_and_run_budget_stop_proxy_retry(monkeypatch, tmp_path, manual, failure):
    monkeypatch.setenv('SMSINVIT_INVITATION_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('CODEX_HOMES_ROOT', str(tmp_path / 'homes'))
    from webui import codex_auth
    from smsinvit.invitation.sms_state import RentalUncertainError, SmsBudgetExhausted

    attempts = []
    picked = []
    card = {'email': 'sms-limit@example.test', 'invite_link': 'https://chatgpt.com/accept-referral?referral_context=fixture',
            'invite_processed_at': 1, 'status': 'failed', 'fail_count': 0}
    monkeypatch.setattr(codex_auth.db, 'get_cdk_code', lambda code: dict(card))
    monkeypatch.setattr(codex_auth.db, 'get_cdk_job', lambda job: {'job_id': job})
    monkeypatch.setattr(codex_auth.db, 'get_registered', lambda email: {'email': email})
    for name in ('add_cdk_event', 'set_cdk_status', 'set_cdk_job', 'update_cdk_fields'):
        monkeypatch.setattr(codex_auth.db, name, lambda *a, **kw: None)
    monkeypatch.setattr(codex_auth.mail_alloc, 'build_provider_for_account', lambda *a, **kw: (object(), 'outlook', {}))
    monkeypatch.setattr(codex_auth.proxy_pool, 'attempts_limit', lambda: 3)

    def pick(**kwargs):
        picked.append(1)
        return {'url': 'http://proxy.example.test:80'}

    monkeypatch.setattr(codex_auth.proxy_pool, 'pick', pick)
    monkeypatch.setattr(codex_auth.proxy_pool, 'record_fail', lambda *a: None)
    monkeypatch.setattr(codex_auth.proxy_pool, 'record_409', lambda *a: {})

    def fail(*args, **kwargs):
        attempts.append(1)
        if failure == 'budget':
            raise SmsBudgetExhausted('fixture rental budget exhausted')
        if failure == 'wrapped_unknown':
            try:
                raise RentalUncertainError('fixture order result uncertain')
            except RentalUncertainError as cause:
                raise codex_auth.JobFailure('wrapped fixture uncertainty') from cause
        raise RentalUncertainError('fixture order result uncertain')

    target = '_manual_attempt' if manual else '_authorize_bound_account'
    monkeypatch.setattr(codex_auth, target, fail)
    if manual:
        result = codex_auth.authorize_registered_email(card['email'], run_id='fixture-run')
    else:
        result = codex_auth.run_invite_job('fixture-card', run_id='fixture-run', job_id='fixture-job')
    assert not result['ok'] and result['attempts'] == 1
    assert attempts == [1] and picked == [1]


@pytest.mark.parametrize('finalized', [False, True])
def test_manual_worker_notification_failure_preserves_success_without_second_run_write(monkeypatch,tmp_path,finalized):
    monkeypatch.setenv('SMSINVIT_INVITATION_LOG_DIR',str(tmp_path/'logs'))
    monkeypatch.setenv('CODEX_HOMES_ROOT',str(tmp_path/'homes'))
    from webui import app
    finishes=[];notifications=[]
    monkeypatch.setattr(app.registrar,'begin_run',lambda *a,**kw:('fixture-run','',None,object()))
    monkeypatch.setattr(app.registrar,'end_run',lambda *a:None)
    monkeypatch.setattr(app.codex_auth,'authorize_registered_email',lambda *a,**kw:{'ok':True,'_run_finalized':finalized})
    monkeypatch.setattr(app.db,'finish_run',lambda *a,**kw:finishes.append((a,kw)))
    def emit(*args):
        notifications.append(args)
        if len(notifications)==1:
            raise RuntimeError('fixture notification failure')
    monkeypatch.setattr(app.registrar,'_emit_status',emit)
    app._codex_auth_worker('fixture-run',None,'fixture@example.test','')
    assert len(finishes)==(0 if finalized else 1)
    assert all(args[1]=='done' for args,kwargs in finishes)

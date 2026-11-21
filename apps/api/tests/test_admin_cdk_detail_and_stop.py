"""后台卡密明细与停止/取消入口的回归测试。

分两部分，都是**真实执行**而不是快照：

1. 邀请侧用真正的 SQLite 事务库（与 `test_invitation_manual_sync.py` 同一套夹具思路，
   只把 MySQL 的 `FOR UPDATE` / `UTC_TIMESTAMP(3)` 换成 SQLite 等价写法）：
   - `attach_cdk_details()` 给运行记录行补卡密明细，三条取值优先级都要对；
   - 补完之后经 `cleaned()` 必须只在持有凭据时给出明文 —— 这是需求 3 的权限边界；
   - `stop_job()` 只把作业置失败、写 ADMIN_STOPPED 复核、释放占位，**绝不写成功**；
   - `stop_pool_round()` 停本轮并关开关。
2. 其余模块走契约断言（DTO / SQL / 源码里的字段名与端点路径），防止后来删掉字段或端点。

停止端点的完整 HTTP 行为（权限、幂等）在 `docs/validation/2026-10-10-batch-cardkey-stop-zh-ui.md`
里逐条记录了验证方式与未覆盖项。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from smsinvit.common.cdk import mask_cdk
from smsinvit.errors import Error
from smsinvit.invitation import mysql
from smsinvit.invitation.router import cleaned
from smsinvit.invitation.service import InvitationService

ROOT = Path(__file__).resolve().parents[1]

# Python 3.12 弃用了 sqlite3 的默认 datetime 适配/转换器，会往测试输出里刷
# DeprecationWarning。这里显式注册等价实现：格式与 MySQL DATETIME 一致，
# 且不改变其它测试对 datetime 的行为。
sqlite3.register_adapter(datetime, lambda value: value.strftime('%Y-%m-%d %H:%M:%S.%f'))
sqlite3.register_converter(
    'TIMESTAMP',
    lambda raw: datetime.strptime(raw.decode() if isinstance(raw, bytes) else raw,
                                  '%Y-%m-%d %H:%M:%S.%f'),
)

PLAIN = 'LC-CDK-TESTPLAIN123456'
RUN_ID = 'run-0001'
# 平台键上的有效期：MySQL 里是 DATETIME，所以断言也用 datetime 推算，
# 不写死一个可能与转换规则不符的常数。
EXPIRES = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


# ──────────────────────── SQLite 夹具 ────────────────────────

class LocalDatabase:
    """真事务的 SQLite；只把 MySQL 专用语法换成等价写法。"""

    def __init__(self):
        # PARSE_DECLTYPES + TIMESTAMP 列让 SQLite 返回 datetime，与 MySQL 驱动一致
        # （`epoch()` 只接受 datetime，不认字符串或数字）。
        self.con = sqlite3.connect(':memory:', isolation_level=None,
                                   detect_types=sqlite3.PARSE_DECLTYPES)
        self.con.row_factory = sqlite3.Row
        self.depth = 0
        self.statements = []

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

    @staticmethod
    def _sqlite(sql):
        # 这些是 MySQL 方言：行锁在单连接测试里无意义，时间函数与 INSERT IGNORE 换成
        # SQLite 等价写法。生产库仍是 MySQL，这里只让同一段业务代码能在测试里真跑。
        return (sql.replace(' FOR UPDATE', '')
                   .replace('UTC_TIMESTAMP(3)', 'CURRENT_TIMESTAMP')
                   .replace('INSERT IGNORE INTO', 'INSERT OR IGNORE INTO'))

    def execute(self, sql, *args):
        statement = self._sqlite(sql)
        self.statements.append(' '.join(statement.split()))
        return self.con.execute(statement, args)

    def all(self, sql, *args):
        return [dict(row) for row in self.execute(sql, *args)]

    def one(self, sql, *args):
        rows = self.all(sql, *args)
        return rows[0] if rows else None

    def scalar(self, sql, *args):
        row = self.one(sql, *args)
        return next(iter(row.values())) if row else None


class RecordingAudit:
    def __init__(self):
        self.records = []

    def record(self, actor, action, resource, target, meta=None):
        self.records.append({'actor': actor, 'action': action, 'resource': resource,
                             'target': target, 'meta': meta})


SCHEMA = '''
CREATE TABLE invitation_v2_outlook_accounts(email TEXT PRIMARY KEY,cdk_code TEXT,cdk_bound_at REAL);
CREATE TABLE invitation_v2_cdk_codes(code TEXT PRIMARY KEY,email TEXT,status TEXT,batch TEXT,
                                     fail_count INTEGER DEFAULT 0,redeemed_at REAL,finished_at REAL,error TEXT);
CREATE TABLE invitation_v2_cdk_jobs(job_id TEXT PRIMARY KEY,code TEXT,email TEXT,attempt INTEGER,
                                    status TEXT,stage TEXT,progress INTEGER,created_at REAL,
                                    started_at REAL,finished_at REAL,error TEXT);
CREATE TABLE invitation_v2_task_holds(job_id TEXT PRIMARY KEY,original_status TEXT,reason TEXT,
                                      created_at TEXT,resolved_at TEXT,resolved_by TEXT,
                                      resolution TEXT,note TEXT,confirmed_executors_stopped INTEGER);
CREATE TABLE invitation_v2_execution_permits(slot_id INTEGER PRIMARY KEY,owner_token TEXT,
                                             instance_id TEXT,email TEXT,task_id TEXT,acquired_at REAL);
CREATE TABLE invitation_v2_manual_run_links(run_id TEXT PRIMARY KEY,code_id TEXT,created_at REAL,
                                            sync_status TEXT,sync_error TEXT,completed_job_id TEXT);
CREATE TABLE invitation_v2_platform_keys(code_id TEXT PRIMARY KEY,inventory_item_id TEXT,expires_at TIMESTAMP);
CREATE TABLE invitation_v2_secret_values(fingerprint TEXT PRIMARY KEY,ciphertext TEXT,plaintext_length INTEGER);
CREATE TABLE invitation_v2_settings(key TEXT PRIMARY KEY,value TEXT);
'''


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

    service = InvitationService.__new__(InvitationService)
    service._executor_lock = threading.RLock()
    service.db = LocalDatabase()
    service.db.con.executescript(SCHEMA)
    service.s = SimpleNamespace(audit=RecordingAudit())
    return service


def seed_run_with_card(service, *, link=True, mailbox=True, job_code=True):
    # 走被 monkeypatch 的 protected()，这样 revealed() 才能把它解回明文 —— 与生产
    # 路径（卡片密文存 secret_values，列里只留 iv2$ 标记）一致。
    marker = mysql.protected(PLAIN)
    service.db.execute('INSERT INTO invitation_v2_cdk_codes(code,email,status,batch,fail_count) VALUES (?,?,?,?,?)',
                       marker, 'user@example.test', 'bound', 'BATCH-20261010-9A3E47AC', 2)
    service.db.execute('INSERT INTO invitation_v2_platform_keys(code_id,expires_at) VALUES (?,?)',
                       marker, EXPIRES.replace(tzinfo=None))
    if link:
        service.db.execute('INSERT INTO invitation_v2_manual_run_links(run_id,code_id,sync_status) VALUES (?,?,?)',
                           mysql.marker(RUN_ID), marker, 'pending')
    if mailbox:
        service.db.execute('INSERT INTO invitation_v2_outlook_accounts(email,cdk_code) VALUES (?,?)',
                           'user@example.test', marker)
    if job_code:
        service.db.execute('INSERT INTO invitation_v2_cdk_jobs(job_id,code,email,status,created_at) VALUES (?,?,?,?,?)',
                           'job-1', marker, 'user@example.test', 'running', 100.0)
    return marker


def run_payload():
    return {'ok': True, 'items': [{ 'run_id': RUN_ID, 'kind': 'codex_auth',
                                    'email': 'user@example.test', 'status': 'done'}]}


# ──────────────────────── 运行记录的卡密明细 ────────────────────────

def test_run_rows_gain_bound_card_detail(inv):
    seed_run_with_card(inv)
    enriched = inv.attach_cdk_details(run_payload())
    row = enriched['items'][0]
    assert row['cdk_code'] == PLAIN
    assert row['cdk_code_masked'] == mask_cdk(PLAIN)
    assert row['cdk_code_masked'] != PLAIN
    assert row['cdk_batch'] == 'BATCH-20261010-9A3E47AC'
    assert row['cdk_status'] == 'bound'
    assert row['cdk_fail_count'] == 2
    assert row['cdk_valid_until'] == EXPIRES.timestamp()


def test_card_detail_falls_back_to_mailbox_then_latest_job(inv):
    seed_run_with_card(inv, link=False)
    row = inv.attach_cdk_details(run_payload())['items'][0]
    assert row['cdk_code'] == PLAIN, '没有 manual_run_links 时要退到邮箱绑定'

    mailbox_row = inv.db.one('SELECT cdk_code FROM invitation_v2_outlook_accounts WHERE email=?',
                             'user@example.test')
    inv.db.execute('UPDATE invitation_v2_outlook_accounts SET cdk_code=NULL WHERE email=?', 'user@example.test')
    assert mailbox_row is not None
    row = inv.attach_cdk_details(run_payload())['items'][0]
    assert row['cdk_code'] == PLAIN, '邮箱没绑卡时要退到该邮箱最近的卡密作业'


def test_card_detail_is_empty_rather_than_wrong_when_nothing_is_bound(inv):
    # 账号工厂补号没有卡密：字段留空，不猜、不报错。
    enriched = inv.attach_cdk_details({'ok': True, 'items': [
        {'run_id': 'run-pool', 'kind': 'register_pool', 'email': 'pool@example.test'},
        {'run_id': RUN_ID, 'kind': 'codex_auth', 'email': 'nobody@example.test'},
    ]})
    assert 'cdk_code' not in enriched['items'][0], '非 codex_auth 的行不该被补字段'
    row = enriched['items'][1]
    assert row['cdk_code'] == ''
    assert row['cdk_code_masked'] == '—'
    assert row['cdk_batch'] is None


def test_card_plaintext_survives_only_with_credentials(inv):
    """补出来的 cdk_code 是明文，必须由既有 cleaned() 掩码兜住。"""
    seed_run_with_card(inv)
    enriched = inv.attach_cdk_details(run_payload())
    assert enriched['items'][0]['cdk_code'] == PLAIN

    without = cleaned(enriched, credentials=False)
    assert without['items'][0]['cdk_code'] == '***'
    assert PLAIN not in json.dumps(without, ensure_ascii=False)
    # 脱敏值仍然可用，列表不会因为掩码变成空白。
    assert without['items'][0]['cdk_code_masked'] == mask_cdk(PLAIN)

    with_credentials = cleaned(enriched, credentials=True)
    assert with_credentials['items'][0]['cdk_code'] == PLAIN


# ──────────────────────── 停止邀请作业 ────────────────────────

def seed_job(inv, status, *, email='user@example.test', slot=7):
    marker = mysql.marker('job-1')
    code = mysql.marker(PLAIN)
    inv.db.execute('INSERT INTO invitation_v2_cdk_jobs(job_id,code,email,status) VALUES (?,?,?,?)',
                   marker, code, email, status)
    inv.db.execute('INSERT INTO invitation_v2_cdk_codes(code,email,status) VALUES (?,?,?)',
                   code, email, status)
    if slot:
        inv.db.execute('INSERT INTO invitation_v2_execution_permits(slot_id,owner_token,email) VALUES (?,?,?)',
                       slot, 'token-1', email)
    return marker, code


def test_stopping_a_queued_job_never_writes_success(inv):
    marker, code = seed_job(inv, 'queued')
    result = InvitationService.stop_job(inv, 'job-1', SimpleNamespace(id='admin-1'))

    assert result['stopped'] is True and result['status'] == 'failed'
    assert result['reviewRequired'] is False, '排队中的作业确定没有外部动作，复核可直接关闭'
    assert result['released'] == {'email': 'user@example.test', 'permitSlot': 7}

    job = inv.db.one('SELECT status,error FROM invitation_v2_cdk_jobs WHERE job_id=?', marker)
    assert job['status'] == 'failed' and '手动停止' in job['error']
    card = inv.db.one('SELECT status FROM invitation_v2_cdk_codes WHERE code=?', code)
    assert card['status'] == 'failed'

    hold = inv.db.one('SELECT original_status,reason,resolved_at FROM invitation_v2_task_holds WHERE job_id=?', marker)
    assert hold['reason'] == 'ADMIN_STOPPED' and hold['original_status'] == 'queued'
    assert hold['resolved_at'] is not None, '未启动的作业复核应被关掉，卡密可立即重置复用'

    permit = inv.db.one('SELECT owner_token,email FROM invitation_v2_execution_permits WHERE slot_id=7')
    assert permit['owner_token'] is None and permit['email'] is None

    # 停止绝不能把任何东西写成成功。
    assert not any('done' in statement.lower() and 'set' in statement.lower() for statement in inv.db.statements)
    assert [row['action'] for row in inv.s.audit.records] == ['invitation.job.stop']


def test_stopping_a_running_job_keeps_the_review_open(inv):
    marker, _code = seed_job(inv, 'running')
    result = InvitationService.stop_job(inv, 'job-1', SimpleNamespace(id='admin-1'))

    assert result['reviewRequired'] is True, '已在跑的作业可能已产生外部动作，必须留复核'
    assert '复核' in result['message']
    hold = inv.db.one('SELECT resolved_at FROM invitation_v2_task_holds WHERE job_id=?', marker)
    assert hold['resolved_at'] is None


def test_stopping_a_finished_job_is_idempotent(inv):
    marker, code = seed_job(inv, 'done', slot=0)
    before = inv.db.one('SELECT status FROM invitation_v2_cdk_jobs WHERE job_id=?', marker)
    result = InvitationService.stop_job(inv, 'job-1', SimpleNamespace(id='admin-1'))

    assert result['stopped'] is True and result['status'] == 'done'
    after = inv.db.one('SELECT status FROM invitation_v2_cdk_jobs WHERE job_id=?', marker)
    assert after['status'] == before['status'] == 'done', '终态不得被停止改写'
    assert inv.db.one('SELECT status FROM invitation_v2_cdk_codes WHERE code=?', code)['status'] == 'done'


def test_stopping_an_unknown_job_is_a_404(inv):
    with pytest.raises(Error) as error:
        InvitationService.stop_job(inv, 'job-does-not-exist', SimpleNamespace(id='admin-1'))
    assert error.value.code == 'NOT_FOUND'


def test_stop_pool_round_stops_the_round_and_closes_the_switch(inv):
    calls = []

    class FakePool:
        def stop_round(self):
            calls.append('stop_round')
            return 3

    def set_setting(key, value):
        calls.append((key, value))

    inv.pool = FakePool()
    inv.repo = SimpleNamespace(set_setting=set_setting)
    result = InvitationService.stop_pool_round(inv, SimpleNamespace(id='admin-1'))

    assert result == {'stopped': 3}
    assert calls == ['stop_round', ('pool_auto_enabled', 'false')], '停本轮必须同时关掉补号开关'
    assert inv.s.audit.records[0]['action'] == 'invitation.pool.stop'


# ──────────────────────── 两个 SMS 后台 worker 开关 ────────────────────────

@pytest.mark.parametrize('name, attribute', [('sms-monitor', 'MONITOR'), ('sms-refund', 'WORKER')])
def test_sms_workers_can_be_read_and_switched(inv, monkeypatch, name, attribute):
    import importlib

    class FakeWorker:
        def __init__(self):
            self.started = self.stopped = 0
            self._thread = None

        def start(self):
            self.started += 1
            self._thread = SimpleNamespace(is_alive=lambda: True)

        def stop(self):
            self.stopped += 1
            self._thread = None

    worker = FakeWorker()
    module_name = InvitationService.BACKGROUND_WORKERS[name][1]
    monkeypatch.setattr(importlib.import_module(module_name), attribute, worker)
    # 开关状态真的落库，所以这里用可变字典而不是固定返回值。
    settings = {'sms_monitor_enabled': 'false', 'sms_refund_enabled': 'false'}
    inv.repo = SimpleNamespace(
        get_setting=lambda key, default=None: settings.get(key, default),
        set_setting=lambda key, value: settings.__setitem__(key, value),
    )

    status = InvitationService.background_worker_status(inv, name)
    assert status == {'enabled': False, 'running': False}

    started = InvitationService.set_background_worker(inv, name, True, SimpleNamespace(id='admin-1'))
    assert started == {'enabled': True, 'running': True} and worker.started == 1
    enabled_key = InvitationService.BACKGROUND_WORKERS[name][0]
    assert settings[enabled_key] == 'true'

    stopped = InvitationService.set_background_worker(inv, name, False, SimpleNamespace(id='admin-1'))
    assert stopped == {'enabled': False, 'running': False} and worker.stopped == 1
    assert settings[enabled_key] == 'false'

    with pytest.raises(Error):
        InvitationService.set_background_worker(inv, 'not-a-worker', True, SimpleNamespace(id='admin-1'))


# ──────────────────────── 卡密字段与端点契约 ────────────────────────

CARD_FIELDS = ('inventoryItemId', 'codeFingerprint', 'codeMasked')


def test_list_sql_and_dto_expose_the_card_key_fields():
    """DTO 白名单若漏字段，前端连 SQL 里查出来的列都拿不到。"""
    module = 'sms'
    dto = json.loads((ROOT / 'smsinvit' / module / 'dto_fields.json').read_text())
    declared = json.dumps(dto, ensure_ascii=False)
    # 这两列来自 SQL，必须出现在白名单里。
    for field in ('inventoryItemId', 'codeFingerprint'):
        assert field in declared, f'{module} 的 DTO 缺少 {field}'
    # codeMasked 是白名单之后由 attach_code_details() 注入的，所以断言服务层真的注入了。
    source = (ROOT / 'smsinvit' / module / 'service.py').read_text()
    assert "row['codeMasked']" in source or "'codeMasked'" in source, f'{module} 未注入 codeMasked'
    assert 'reveal' in source, f'{module} 缺少明文只在导出权限下返回的分支'


def test_invitation_run_rows_declare_every_card_field():
    source = (ROOT / 'smsinvit' / 'invitation' / 'manual_sync.py').read_text()
    for field in ('cdk_code', 'cdk_code_masked', 'cdk_batch', 'cdk_status',
                  'cdk_fail_count', 'cdk_valid_until'):
        assert f"out['{field}']" in source, f'运行记录缺少 {field}'




NEW_ENDPOINTS = [
    ('smsinvit.common.inventory', 'GET', '/api/v1/admin/inventory/batches/{id}/cards'),
    ('smsinvit.common.jobs', 'POST', '/api/v1/admin/jobs/{id}/cancel'),
    ('smsinvit.common.notifications', 'POST', '/api/v1/admin/notifications/outbox/{id}/cancel'),
    ('smsinvit.sms.routes', 'GET', '/api/v1/admin/sms/redeems'),
]


@pytest.mark.parametrize('module, method, path', NEW_ENDPOINTS)
def test_new_endpoints_are_registered(module, method, path):
    import importlib

    router = importlib.import_module(module).router
    registered = {(getattr(route, 'path', ''), m)
                  for route in router.routes
                  for m in getattr(route, 'methods', set())}
    assert (path, method) in registered, f'{method} {path} 未注册'


@pytest.mark.parametrize('method, path', [
    ('POST', '/api/v1/admin/invitation/jobs/{job_id}/stop'),
    ('POST', '/api/v1/admin/invitation/pool/stop'),
    ('GET', '/api/v1/admin/invitation/sms-monitor'),
    ('PUT', '/api/v1/admin/invitation/sms-monitor'),
    ('GET', '/api/v1/admin/invitation/sms-refund'),
    ('PUT', '/api/v1/admin/invitation/sms-refund'),
])
def test_invitation_control_endpoints_are_declared(method, path):
    """邀请的执行器由 build_router() 动态构建，且 mysql.configure 每进程只允许一次绑定，
    所以这里断言声明而不是再建一个 app（后者会破坏进程内的单例绑定）。"""
    source = (ROOT / 'smsinvit' / 'invitation' / 'router.py').read_text()
    normalized = ' '.join(source.split())
    if '{job_id}' in path or '{job_id}' in path:
        assert "'/api/v1/admin/invitation/jobs/{job_id}/stop'" in normalized
        return
    suffix = path.rsplit('/invitation/', 1)[1]
    if '{' in suffix:
        assert suffix in normalized, f'缺少 {path}'
        return
    # sms-monitor / sms-refund 由循环注册，两处 add_api_route 的 methods 必须都在。
    assert "'/api/v1/admin/invitation/'+name" in normalized, f'缺少 {path} 的动态注册'
    assert f"methods=['{method}']" in normalized, f'sms-monitor / sms-refund 缺少 {method}'
    assert "'sms-monitor'" in normalized and "'sms-refund'" in normalized



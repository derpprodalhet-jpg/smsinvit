"""账号池 SQL 语义：可兑换=registered 且未被占用；绑定唯一；解绑回到正确状态。

真实的 MySQL 端到端测试仍在 test_invitation_mysql.py（需要隔离库）。
这里用 SQLite + MySQL 语法适配器，走**真正的** mysql 翻译层，
所以 vendor/codex_invit/webui/db.py 里那几条 SQL 是被真执行的，不是打桩。
"""
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from smsinvit.db import Database
from smsinvit.invitation import mysql
from smsinvit.security import SecretCodec
from webui import db as pooldb


ROOT = Path(__file__).resolve().parents[3]

SCHEMA = """
CREATE TABLE invitation_v2_outlook_accounts (
  email TEXT PRIMARY KEY, password TEXT, client_id TEXT, refresh_token TEXT,
  relay_url TEXT, kind TEXT NOT NULL DEFAULT 'outlook',
  status TEXT NOT NULL DEFAULT 'available', imported_at REAL, claimed_at REAL,
  finished_at REAL, fail_reason TEXT, proxy TEXT, cdk_code TEXT UNIQUE,
  cdk_bound_at REAL, mail_kind TEXT,
  phone_bound_at REAL, phone_bound_ok INTEGER NOT NULL DEFAULT -1,
  used_proxy INTEGER NOT NULL DEFAULT -1
);
CREATE TABLE invitation_v2_registered (
  email TEXT PRIMARY KEY, password TEXT, access_token TEXT, session_token TEXT,
  refresh_token TEXT, id_token TEXT, device_id TEXT, csrf_token TEXT,
  cookie_header TEXT, totp_secret TEXT, totp_factor_id TEXT, extra_json TEXT,
  created_at REAL, proxy TEXT, source TEXT);
CREATE TABLE invitation_v2_secret_values (
  fingerprint TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, plaintext_length INTEGER NOT NULL);
CREATE TABLE invitation_v2_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE invitation_v2_proxy_pool (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, label TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, use_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0, fail_count INTEGER NOT NULL DEFAULT 0,
  consecutive_409 INTEGER NOT NULL DEFAULT 0, total_409 INTEGER NOT NULL DEFAULT 0,
  frozen_once INTEGER NOT NULL DEFAULT 0, frozen_until REAL, freeze_count INTEGER NOT NULL DEFAULT 0,
  permanent_dead INTEGER NOT NULL DEFAULT 0, last_used_at REAL, last_409_at REAL,
  consecutive_409_register INTEGER NOT NULL DEFAULT 0, total_409_register INTEGER NOT NULL DEFAULT 0,
  frozen_once_register INTEGER NOT NULL DEFAULT 0, frozen_until_register REAL,
  freeze_count_register INTEGER NOT NULL DEFAULT 0, permanent_dead_register INTEGER NOT NULL DEFAULT 0,
  last_409_at_register REAL,
  consecutive_409_codex INTEGER NOT NULL DEFAULT 0, total_409_codex INTEGER NOT NULL DEFAULT 0,
  frozen_once_codex INTEGER NOT NULL DEFAULT 0, frozen_until_codex REAL,
  freeze_count_codex INTEGER NOT NULL DEFAULT 0, permanent_dead_codex INTEGER NOT NULL DEFAULT 0,
  last_409_at_codex REAL,
  last_error TEXT, added_at REAL, updated_at REAL);
CREATE TABLE invitation_v2_proxy_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, proxy_id INTEGER, url TEXT, event TEXT, detail TEXT, created_at REAL);
"""


class PoolSqliteDatabase(Database):
    """MySQL 语法 → SQLite：只做必要的等价改写，不改语义。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen = []

    def _prepare(self, sql, params):
        # 先记下 MySQL 原样（行锁断言要用），再降级成 SQLite 能认的写法。
        self.seen.append(sql)
        sql = sql.replace(' FOR UPDATE SKIP LOCKED', '').replace(' FOR UPDATE', '')
        sql = sql.replace(' ON DUPLICATE KEY UPDATE fingerprint=VALUES(fingerprint)',
                          ' ON CONFLICT(fingerprint) DO NOTHING')
        sql = sql.replace('INSERT IGNORE', 'INSERT OR IGNORE')
        sql = re.sub(r'\bRAND\(\)', 'RANDOM()', sql)
        return super()._prepare(sql, params)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    path = tmp_path / 'pool.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
    engine = create_engine('sqlite:///' + str(path),
                           connect_args={'check_same_thread': False, 'timeout': 30},
                           pool_size=10, max_overflow=10, hide_parameters=True)
    db = PoolSqliteDatabase(engine=engine)
    services = SimpleNamespace(db=db, codec=SecretCodec('a1' * 32, 'pool-test-fingerprints'))
    monkeypatch.setattr(mysql, '_services', None)
    monkeypatch.setattr(mysql, '_runtime_owner', None)
    monkeypatch.setattr(mysql, '_cache', {})
    monkeypatch.setattr(mysql, '_redaction_values', set())
    mysql.configure(services)
    yield SimpleNamespace(db=db, services=services)
    db.close()


def seed(pool, email, status='registered', cdk_code=None, kind='outlook', registered=False):
    # cdk_code 是机密列：真实写入总是经由 translate → protected，这里照做，
    # 否则 WHERE cdk_code=? 比对的是标记而表里是明文，配不上。
    stored = mysql.protected(cdk_code) if cdk_code else None
    with sqlite3.connect(pool.db.engine.url.database) as connection:
        connection.execute(
            'INSERT INTO invitation_v2_outlook_accounts(email,kind,status,cdk_code,imported_at) VALUES (?,?,?,?,?)',
            (email, kind, status, stored, 1.0))
        if registered or status in ('registered', 'done'):
            connection.execute('INSERT INTO invitation_v2_registered(email,password,totp_secret) VALUES (?,?,?)',
                               (email, 'pw-' + email, 'totp-' + email))


def status_of(pool, email):
    return pool.db.scalar('SELECT status FROM invitation_v2_outlook_accounts WHERE email=?', email)


def cdk_of(pool, email):
    return mysql.revealed(pool.db.scalar('SELECT cdk_code FROM invitation_v2_outlook_accounts WHERE email=?', email))


def test_only_registered_unbound_accounts_are_redeemable(pool):
    seed(pool, 'available@example.test', status='available')
    seed(pool, 'ready@example.test', status='registered')
    seed(pool, 'taken@example.test', status='in_use', cdk_code='CDK-TAKEN')
    seed(pool, 'finished@example.test', status='done')
    assert pooldb.count_available_mailboxes('outlook') == 1
    assert [row['email'] for row in pooldb.list_available_mailboxes('outlook')] == ['ready@example.test']


def test_claim_binds_a_registered_account_and_marks_it_in_use(pool):
    seed(pool, 'ready@example.test', status='registered')
    claimed = pooldb.claim_cdk_mailbox('CDK-ONE', 'outlook', pooled=True)
    assert claimed and claimed['email'] == 'ready@example.test'
    assert status_of(pool, 'ready@example.test') == 'in_use'
    assert cdk_of(pool, 'ready@example.test') == 'CDK-ONE'
    assert pooldb.count_available_mailboxes('outlook') == 0


def test_claim_never_hands_out_unregistered_or_bound_mailboxes(pool):
    seed(pool, 'available@example.test', status='available')
    assert pooldb.claim_cdk_mailbox('CDK-ONE', 'outlook', pooled=True) is None
    seed(pool, 'taken@example.test', status='in_use', cdk_code='CDK-OTHER')
    assert pooldb.claim_cdk_mailbox('CDK-TWO', 'outlook', pooled=True) is None


def test_one_account_can_only_ever_serve_one_cdk(pool):
    """需求 7：使用 CDK 后立即绑定，其余 CDK 不能再取这个邮箱。"""
    seed(pool, 'only@example.test', status='registered')
    assert pooldb.claim_cdk_mailbox('CDK-FIRST', 'outlook', pooled=True)['email'] == 'only@example.test'
    assert pooldb.claim_cdk_mailbox('CDK-SECOND', 'outlook', pooled=True) is None
    assert pool.db.scalar('SELECT COUNT(*) FROM invitation_v2_outlook_accounts WHERE cdk_code IS NOT NULL') == 1


def test_release_sends_registered_accounts_back_to_the_pool(pool):
    seed(pool, 'ready@example.test', status='registered')
    pooldb.claim_cdk_mailbox('CDK-ONE', 'outlook', pooled=True)
    assert pooldb.release_cdk_mailbox('CDK-ONE') == 1
    assert status_of(pool, 'ready@example.test') == 'registered'
    assert cdk_of(pool, 'ready@example.test') is None
    # 回到池子后可以被另一张卡密取走
    assert pooldb.claim_cdk_mailbox('CDK-TWO', 'outlook', pooled=True)['email'] == 'ready@example.test'


def test_release_keeps_legacy_unregistered_mailboxes_available_not_redeemable(pool):
    """老数据（绑定但没有 registered 行）解绑后回 available，绝不能变成"可兑换空号"。"""
    seed(pool, 'legacy@example.test', status='in_use', cdk_code='CDK-LEGACY')
    assert pooldb.release_cdk_mailbox('CDK-LEGACY') == 1
    assert status_of(pool, 'legacy@example.test') == 'available'
    assert pooldb.count_available_mailboxes('outlook') == 0


def test_release_leaves_finished_accounts_done(pool):
    seed(pool, 'done@example.test', status='done', cdk_code='CDK-DONE')
    assert pooldb.release_cdk_mailbox('CDK-DONE') == 1
    assert status_of(pool, 'done@example.test') == 'done'
    assert cdk_of(pool, 'done@example.test') is None


def test_mark_registered_opens_the_pool_and_clears_failures(pool):
    seed(pool, 'fresh@example.test', status='in_use')
    with sqlite3.connect(pool.db.engine.url.database) as connection:
        connection.execute("UPDATE invitation_v2_outlook_accounts SET fail_reason='boom' WHERE email='fresh@example.test'")
    pooldb.mark_registered('fresh@example.test')
    assert status_of(pool, 'fresh@example.test') == 'registered'
    assert pool.db.scalar('SELECT fail_reason FROM invitation_v2_outlook_accounts WHERE email=?', 'fresh@example.test') is None
    assert pool.db.scalar('SELECT finished_at FROM invitation_v2_outlook_accounts WHERE email=?', 'fresh@example.test') is not None
    assert pooldb.count_available_mailboxes('outlook') == 1


def test_mark_done_removes_the_account_from_the_pool(pool):
    seed(pool, 'used@example.test', status='registered')
    pooldb.mark_done('used@example.test')
    assert status_of(pool, 'used@example.test') == 'done'
    assert pooldb.count_available_mailboxes('outlook') == 0


def test_pooled_allocation_takes_a_row_lock_inside_a_transaction(pool):
    """并发抢号靠 SQL 行锁：这条断言盯住"条件被改过就悄悄失效"的回归。

    vendor 的每个公开函数都被 smsinvit 的 transactional 包了一层，
    所以取号本来就在事务里，FOR UPDATE SKIP LOCKED 必须挂在这条 SELECT 上。
    """
    seed(pool, 'ready@example.test', status='registered')
    pooldb.claim_cdk_mailbox('CDK-LOCK', 'outlook', pooled=True)
    assert any(sql.rstrip().upper().endswith('FOR UPDATE SKIP LOCKED') for sql in pool.db.seen), pool.db.seen


def test_stats_and_pool_counts_expose_the_registered_bucket(pool):
    seed(pool, 'a@example.test', status='available')
    seed(pool, 'b@example.test', status='registered')
    seed(pool, 'c@example.test', status='done')
    stats = pooldb.stats()
    assert stats['registered'] == 1 and stats['available'] == 1 and stats['done'] == 1
    by_kind = pooldb.stats_by_kind()['outlook']
    assert by_kind['registered'] == 1


# ──────────────────────── 非池化来源：注册时现造地址入池 ────────────────────────


def test_ensure_pool_account_registers_a_freshly_created_address(pool):
    """CF 临时邮箱这类来源的地址是注册那一刻才有的，必须能当场登记进号池。"""
    assert pooldb.ensure_pool_account('fresh@cf.test', 'cf_temp', proxy='http://p:1') is True
    assert status_of(pool, 'fresh@cf.test') == 'registered'
    assert pool.db.scalar('SELECT kind FROM invitation_v2_outlook_accounts WHERE email=?', 'fresh@cf.test') == 'cf_temp'
    assert mysql.revealed(pool.db.scalar('SELECT proxy FROM invitation_v2_outlook_accounts WHERE email=?', 'fresh@cf.test')) == 'http://p:1'
    # 登记完就是可兑换账号
    assert pooldb.count_available_mailboxes('cf_temp') == 1
    assert pooldb.claim_cdk_mailbox('CDK-CF', 'cf_temp', pooled=True)['email'] == 'fresh@cf.test'


def test_ensure_pool_account_never_clobbers_bound_or_finished_rows(pool):
    seed(pool, 'taken@cf.test', status='in_use', cdk_code='CDK-TAKEN')
    assert pooldb.ensure_pool_account('taken@cf.test', 'cf_temp') is False
    assert status_of(pool, 'taken@cf.test') == 'in_use'
    seed(pool, 'finished@cf.test', status='done')
    assert pooldb.ensure_pool_account('finished@cf.test', 'cf_temp') is False
    assert status_of(pool, 'finished@cf.test') == 'done'


def test_ensure_pool_account_ignores_blank_addresses(pool):
    assert pooldb.ensure_pool_account('', 'cf_temp') is False
    assert pool.db.scalar('SELECT COUNT(*) FROM invitation_v2_outlook_accounts') == 0


def test_mixed_sources_share_one_redeemable_pool(pool):
    """补号来源与兑换来源可以不同：兑换按"谁先注册好"取，不按当前配置过滤。"""
    seed(pool, 'outlook@example.test', status='registered', kind='outlook')
    pooldb.ensure_pool_account('cf@cf.test', 'cf_temp')
    # 不筛选 kind 的取号（兑换走的就是这条）能拿到任意一种来源
    assert pooldb.count_available_mailboxes('') == 2
    first = pooldb.claim_cdk_mailbox('CDK-A', '', pooled=True)
    second = pooldb.claim_cdk_mailbox('CDK-B', '', pooled=True)
    assert {first['email'], second['email']} == {'outlook@example.test', 'cf@cf.test'}
    assert pooldb.claim_cdk_mailbox('CDK-C', '', pooled=True) is None


def test_run_email_can_be_retargeted_to_the_created_address(pool):
    """注册时先给占位地址，成功后要把运行记录改成真实地址，否则事后对不上号。"""
    with sqlite3.connect(pool.db.engine.url.database) as connection:
        connection.execute(
            'CREATE TABLE IF NOT EXISTS invitation_v2_runs(run_id TEXT PRIMARY KEY,email TEXT,status TEXT,kind TEXT,started_at REAL,finished_at REAL)')
        # run_id 是机密列，真实写入总是经 translate → marker，这里照做
        connection.execute("INSERT INTO invitation_v2_runs(run_id,email,status,kind) VALUES (?,?,?,?)",
                           (mysql.marker('rid-plain'), 'cf_temp_placeholder_1@placeholder.local', 'running', 'register_pool'))
    pooldb.retarget_run('rid-plain', 'Real@CF.test')
    assert pool.db.scalar('SELECT email FROM invitation_v2_runs WHERE run_id=?', mysql.marker('rid-plain')) == 'real@cf.test'


# ──────────────────────── 代理 409 生命周期（真实现） ────────────────────────


def test_proxy_409_lifecycle_is_per_purpose(pool):
    """同一用途维度：连续 2 次 409 → 冻结 20 分钟；解冻后再 1 次 → 该维度永久不可用。"""
    from webui import proxy_pool

    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_409_threshold','2')")
    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_freeze_minutes','20')")
    proxy_pool.add_from_text("http://p1:8080")
    proxy = 'http://p1:8080'
    assert pool.db.scalar('SELECT COUNT(*) FROM invitation_v2_proxy_pool') == 1

    assert proxy_pool.record_409(proxy, 'register')['action'] == 'count'
    frozen = proxy_pool.record_409(proxy, 'register')
    assert frozen['action'] == 'freeze' and frozen['purpose'] == 'register'
    assert pool.db.scalar('SELECT frozen_once_register FROM invitation_v2_proxy_pool') == 1
    # 冻结的是注册维度：注册挑不到，激活仍然挑得到
    assert proxy_pool.has_available(purpose='register') is False
    assert proxy_pool.has_available(purpose='codex') is True

    # 模拟解冻后再来一次 → 该维度永久不可用
    pool.db.execute('UPDATE invitation_v2_proxy_pool SET frozen_until_register=0')
    assert proxy_pool.has_available(purpose='register') is True
    assert proxy_pool.record_409(proxy, 'register')['action'] == 'permanent'
    assert pool.db.scalar('SELECT permanent_dead_register FROM invitation_v2_proxy_pool') == 1
    assert pool.db.scalar('SELECT permanent_dead_codex FROM invitation_v2_proxy_pool') == 0
    assert proxy_pool.has_available(purpose='register') is False
    assert proxy_pool.has_available(purpose='codex') is True


def test_the_two_purpose_locks_never_interfere(pool):
    """注册锁与激活锁必须完全独立：一边冻死，另一边照样能用。"""
    from webui import proxy_pool

    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_409_threshold','2')")
    proxy_pool.add_from_text("http://p2:8080")
    proxy = 'http://p2:8080'
    for _ in range(2):
        proxy_pool.record_409(proxy, 'codex')
    assert proxy_pool.has_available(purpose='codex') is False
    assert proxy_pool.has_available(purpose='register') is True
    # 激活维度解冻后再一次 → 只有激活维度永久不可用
    pool.db.execute('UPDATE invitation_v2_proxy_pool SET frozen_until_codex=0')
    assert proxy_pool.record_409(proxy, 'codex')['action'] == 'permanent'
    assert proxy_pool.has_available(purpose='register') is True
    # 注册维度这时才用两次 → 各自冻结，互不覆盖
    assert proxy_pool.record_409(proxy, 'register')['action'] == 'count'
    assert proxy_pool.record_409(proxy, 'register')['action'] == 'freeze'
    # 手动只重置一个维度：激活仍是永久不可用
    assert proxy_pool.reset(proxy, 'register') is True
    assert proxy_pool.has_available(purpose='register') is True
    assert proxy_pool.has_available(purpose='codex') is False
    assert proxy_pool.reset(proxy, 'codex') is True
    assert proxy_pool.has_available(purpose='codex') is True


def test_warmup_success_resets_only_its_own_dimension(pool):
    """同维度成功清零该维度连续计数；另一个维度的计数保持不动。"""
    from webui import proxy_pool

    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_409_threshold','2')")
    proxy_pool.add_from_text("http://p3:8080")
    proxy = 'http://p3:8080'
    proxy_pool.record_409(proxy, 'register')
    proxy_pool.record_409(proxy, 'codex')
    proxy_pool.record_success(proxy, 'register')
    assert pool.db.scalar('SELECT consecutive_409_register FROM invitation_v2_proxy_pool') == 0
    assert pool.db.scalar('SELECT consecutive_409_codex FROM invitation_v2_proxy_pool') == 1


# ──────────────────────── 代理自身不可用（健康维度） ────────────────────────


def test_transport_failure_freezes_then_kills_the_proxy_for_both_purposes(pool):
    """代理连不上/隧道被拒 → 2 次冻结 20 分钟（pick 跳过它），解冻后再 1 次永久不可用。

    这条与用途无关：隧道被拒时注册与激活都不可用，所以两种用途一起被跳过。
    """
    from webui import proxy_pool

    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_409_threshold','2')")
    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_freeze_minutes','20')")
    proxy_pool.add_from_text("http://t1:8080")
    proxy = 'http://t1:8080'

    assert proxy_pool.record_transport_failure(proxy)['action'] == 'count'
    assert proxy_pool.has_available(purpose='register') is True
    assert proxy_pool.record_transport_failure(proxy)['action'] == 'freeze'
    # 冻结后两种用途都挑不到它 —— 于是"切换代理"能真正生效
    assert proxy_pool.has_available(purpose='register') is False
    assert proxy_pool.has_available(purpose='codex') is False

    pool.db.execute('UPDATE invitation_v2_proxy_pool SET frozen_until=0')
    assert proxy_pool.record_transport_failure(proxy)['action'] == 'permanent'
    assert proxy_pool.has_available(purpose='register') is False
    # 单独重置健康维度即可恢复（不需要清业务 409 账）
    assert proxy_pool.reset(proxy, 'health') is True
    assert proxy_pool.has_available(purpose='register') is True
    assert proxy_pool.has_available(purpose='codex') is True


def test_transport_failures_do_not_touch_the_business_409_locks(pool):
    from webui import proxy_pool

    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_409_threshold','2')")
    proxy_pool.add_from_text("http://t2:8080")
    proxy = 'http://t2:8080'
    proxy_pool.record_transport_failure(proxy)
    proxy_pool.record_transport_failure(proxy)
    assert pool.db.scalar('SELECT consecutive_409_register FROM invitation_v2_proxy_pool') == 0
    assert pool.db.scalar('SELECT consecutive_409_codex FROM invitation_v2_proxy_pool') == 0
    assert pool.db.scalar('SELECT permanent_dead_register FROM invitation_v2_proxy_pool') == 0
    assert pool.db.scalar('SELECT permanent_dead_codex FROM invitation_v2_proxy_pool') == 0


def test_pick_skips_unavailable_proxies_and_reports_none_when_all_are_down(pool):
    """所有代理都不可用 → pick 返回 None（调用方据此停下并显示「代理不可用」）。"""
    from webui import proxy_pool

    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_409_threshold','2')")
    proxy_pool.add_from_text("http://t3:8080\nhttp://t4:8080")
    assert proxy_pool.available_count(purpose='register') == 2
    for _ in range(4):                       # 两个代理各失败 2 次 → 都冻结
        picked = proxy_pool.pick(purpose='register')
        assert picked is not None
        proxy_pool.record_transport_failure(picked['url'])
    assert proxy_pool.available_count(purpose='register') == 0
    assert proxy_pool.pick(purpose='register') is None
    assert proxy_pool.pick(purpose='codex') is None


def test_a_proxy_serves_only_one_account_at_a_time(pool):
    """一条代理 = 一个出口 IP = 同一时刻只服务一个号。

    两个号共用一条代理时，它们的会话罐 / session_token 都建在同一个出口 IP 上、
    互相踩，兑换阶段必然出问题。所以严格模式（补号）下，被别的号占着的那条绝不再
    发出去 —— 宁可这轮不补号；但占用者自己重试时还能拿回自己那条。
    """
    from webui import proxy_pool

    pool.db.execute("INSERT INTO invitation_v2_settings(`key`,value) VALUES ('proxy_409_threshold','2')")
    proxy_pool.add_from_text("http://only:8080")
    only = 'http://only:8080'

    # A 号注册时占住唯一那条
    seed(pool, 'a@cf.test', status='in_use', kind='cf_temp')
    pooldb.mark_registered('a@cf.test', proxy=only)

    # 严格模式（补号）：被别人占着 → 拿不到，宁可不补号
    assert proxy_pool.pick(purpose='register', allow_shared=False) is None
    # 别的号：严格模式同样拿不到
    assert proxy_pool.pick(purpose='codex', exclude_bound_to='b@cf.test',
                           allow_shared=False) is None
    # 占用者自己：自己那条不算被占用 → 重试时还能拿回来
    got = proxy_pool.pick(purpose='codex', exclude_bound_to='a@cf.test',
                          allow_shared=False)
    assert got is not None and got['url'] == only

    # 走到终态（done）后释放，别的号才能拿
    pooldb.mark_done('a@cf.test')
    assert proxy_pool.pick(purpose='register', allow_shared=False) is not None


# ──────────────────────── 注册结果要能看到实际用的代理 ────────────────────────


def test_registration_records_the_proxy_that_was_actually_used(pool):
    """注册完成时把**实际代理**写进账号行。

    以前 mark_registered/mark_done 只改状态，注册结果与邮箱管理里就显示成
    「直连」—— 看起来像没走代理，其实是数据没落库。
    """
    seed(pool, 'withproxy@cf.test', status='in_use', kind='cf_temp')
    pooldb.mark_registered('withproxy@cf.test', proxy='http://u:p@10.0.0.1:8080')
    assert status_of(pool, 'withproxy@cf.test') == 'registered'
    assert mysql.revealed(
        pool.db.scalar('SELECT proxy FROM invitation_v2_outlook_accounts WHERE email=?', 'withproxy@cf.test')
    ) == 'http://u:p@10.0.0.1:8080'


def test_recording_without_a_proxy_does_not_erase_the_old_one(pool):
    """直传空值不得抹掉已有代理（手动单次注册不传代理时尤其重要）。"""
    seed(pool, 'keep@cf.test', status='available', kind='cf_temp')
    pool.db.execute("UPDATE invitation_v2_outlook_accounts SET proxy=? WHERE email=?",
                    mysql.protected('http://u:p@10.0.0.2:8080'), 'keep@cf.test')
    pooldb.mark_registered('keep@cf.test', proxy='')
    assert mysql.revealed(
        pool.db.scalar('SELECT proxy FROM invitation_v2_outlook_accounts WHERE email=?', 'keep@cf.test')
    ) == 'http://u:p@10.0.0.2:8080'


def test_done_registration_also_records_the_proxy(pool):
    seed(pool, 'manual@cf.test', status='in_use', kind='outlook')
    pooldb.mark_done('manual@cf.test', proxy='http://u:p@10.0.0.3:8080')
    assert status_of(pool, 'manual@cf.test') == 'done'
    assert mysql.revealed(
        pool.db.scalar('SELECT proxy FROM invitation_v2_outlook_accounts WHERE email=?', 'manual@cf.test')
    ) == 'http://u:p@10.0.0.3:8080'


# ──────────────────────── 是否接码（号池标签） ────────────────────────


def test_phone_binding_status_is_recorded_and_counted(pool):
    """接码状态要能落库、能被统计：已接码的号兑换时免 add-phone。"""
    seed(pool, 'bound@cf.test', status='registered', kind='cf_temp')
    seed(pool, 'unbound@cf.test', status='registered', kind='cf_temp')
    seed(pool, 'failed@cf.test', status='registered', kind='cf_temp')
    seed(pool, 'done@cf.test', status='done', kind='cf_temp')

    pooldb.mark_phone_bound('bound@cf.test', True)
    pooldb.mark_phone_bound('failed@cf.test', False, '号码在 80 秒内未收到验证码')

    assert pool.db.scalar('SELECT phone_bound_ok FROM invitation_v2_outlook_accounts WHERE email=?',
                          'bound@cf.test') == 1
    assert pool.db.scalar('SELECT phone_bound_ok FROM invitation_v2_outlook_accounts WHERE email=?',
                          'failed@cf.test') == 0
    assert pool.db.scalar('SELECT phone_bound_ok FROM invitation_v2_outlook_accounts WHERE email=?',
                          'unbound@cf.test') == -1
    # 失败原因要留痕，便于排查
    assert '80 秒' in pool.db.scalar('SELECT fail_reason FROM invitation_v2_outlook_accounts WHERE email=?',
                                    'failed@cf.test')
    # 统计只看池内（registered/in_use），已完成的号不算
    assert pooldb.count_phone_bound('', True) == 1
    assert pooldb.count_phone_bound('', False) == 2
    assert pooldb.count_phone_bound('cf_temp', True) == 1
    assert pooldb.count_phone_bound('outlook', True) == 0


def test_used_proxy_is_recorded_so_the_ui_never_claims_direct(pool):
    """代理列必须能区分"真直连"和"没记录"（历史行 -1）。"""
    pooldb.ensure_pool_account('withproxy@cf.test', 'cf_temp', proxy='http://u:p@1.2.3.4:8080',
                               used_proxy=True)
    pooldb.ensure_pool_account('direct@cf.test', 'cf_temp', used_proxy=False)
    seed(pool, 'legacy@cf.test', status='registered', kind='cf_temp')   # 历史行，没记录

    def flag(email):
        return pool.db.scalar('SELECT used_proxy FROM invitation_v2_outlook_accounts WHERE email=?', email)

    assert flag('withproxy@cf.test') == 1
    assert flag('direct@cf.test') == 0
    assert flag('legacy@cf.test') == -1
    # 代理地址只对"有代理"的行存在
    assert mysql.revealed(pool.db.scalar(
        'SELECT proxy FROM invitation_v2_outlook_accounts WHERE email=?', 'withproxy@cf.test'))
    assert not (pool.db.scalar(
        'SELECT proxy FROM invitation_v2_outlook_accounts WHERE email=?', 'direct@cf.test') or '')


def test_mark_registered_records_proxy_usage(pool):
    seed(pool, 'r1@cf.test', status='in_use', kind='cf_temp')
    pooldb.mark_registered('r1@cf.test', proxy='http://u:p@9.9.9.9:1', used_proxy=True)
    assert pool.db.scalar('SELECT used_proxy FROM invitation_v2_outlook_accounts WHERE email=?',
                          'r1@cf.test') == 1
    assert mysql.revealed(pool.db.scalar('SELECT proxy FROM invitation_v2_outlook_accounts WHERE email=?',
                                         'r1@cf.test')) == 'http://u:p@9.9.9.9:1'


def test_claim_prefers_phone_verified_accounts(pool):
    """用户兑换 CDK 时优先拿"注册时已接码"的号：省掉 add-phone 与再租号等码。"""
    seed(pool, 'unverified-a@cf.test', status='registered', kind='cf_temp')
    seed(pool, 'verified-1@cf.test', status='registered', kind='cf_temp')
    seed(pool, 'unverified-b@cf.test', status='registered', kind='cf_temp')
    seed(pool, 'verified-2@cf.test', status='registered', kind='cf_temp')
    pooldb.mark_phone_bound('verified-1@cf.test', True)
    pooldb.mark_phone_bound('verified-2@cf.test', True)

    claimed = []
    for i in range(4):
        row = pooldb.claim_cdk_mailbox(f'CDK-{i}', 'cf_temp', pooled=True)
        assert row, f'第 {i+1} 次取号不该失败'
        claimed.append(row['email'])
    # 前两个必须是已接码的（顺序随机），后两个才是未接码的
    assert set(claimed[:2]) == {'verified-1@cf.test', 'verified-2@cf.test'}, claimed
    assert set(claimed[2:]) == {'unverified-a@cf.test', 'unverified-b@cf.test'}, claimed
    # 取完就没有了
    assert pooldb.claim_cdk_mailbox('CDK-x', 'cf_temp', pooled=True) is None


def test_claim_keeps_kind_and_single_binding_guarantees(pool):
    """优先接码不能破坏原有约束：kind 过滤 + 一个号只绑一个 CDK。"""
    seed(pool, 'o1@outlook.test', status='registered', kind='outlook')
    seed(pool, 'c1@cf.test', status='registered', kind='cf_temp')
    pooldb.mark_phone_bound('o1@outlook.test', True)      # 已接码但类型不匹配

    row = pooldb.claim_cdk_mailbox('CDK-1', 'cf_temp', pooled=True)
    assert row and row['email'] == 'c1@cf.test'           # 不能跨类型抢
    assert pooldb.claim_cdk_mailbox('CDK-2', 'cf_temp', pooled=True) is None
    # 同一个号不会被第二个 CDK 抢走
    row2 = pooldb.claim_cdk_mailbox('CDK-3', 'outlook', pooled=True)
    assert row2 and row2['email'] == 'o1@outlook.test'


def test_retry_of_a_failed_cdk_reuses_the_same_account_and_never_returns_it_to_the_pool(pool):
    """主人的要求：失败后**不许回池子**，用同一个卡密重试时继续用这个号。

    卡密非 unused 时（bound/failed），兑换流程只认"这个号还绑着这个卡密"，
    所以绑定关系必须保留 —— 这条测试把它钉死，防止以后被改成回池。
    """
    seed(pool, 'retry@cf.test', status='registered', kind='cf_temp')
    pooldb.mark_phone_bound('retry@cf.test', True)
    claimed = pooldb.claim_cdk_mailbox('CDK-RETRY', 'cf_temp', pooled=True)
    assert claimed and claimed['email'] == 'retry@cf.test'

    # 模拟授权失败：账号仍是 in_use，绑定保留（不回 registered、不解除绑定）
    bind = pool.db.one('SELECT status, cdk_code FROM invitation_v2_outlook_accounts WHERE email=?',
                       'retry@cf.test')
    assert bind['status'] == 'in_use'
    # revealed() 解出的是明文；marker() 是给 SQL 比较用的指纹
    assert mysql.revealed(bind['cdk_code']) == 'CDK-RETRY'
    assert bind['cdk_code'] == mysql.marker('CDK-RETRY')

    # 重试：卡密仍解析到同一个号；同时它不会出现在池子的可领取池里
    assert pool.db.scalar(
        "SELECT COUNT(*) FROM invitation_v2_outlook_accounts "
        "WHERE status='registered' AND coalesce(cdk_code,'')=''") == 0
    # 机密列在库里存的是**指纹/密文**，SQL 比较要用 marker()
    again = pool.db.one('SELECT email, cdk_code FROM invitation_v2_outlook_accounts WHERE cdk_code=?',
                        mysql.marker('CDK-RETRY'))
    assert again and again['email'] == 'retry@cf.test'
    assert mysql.revealed(again['cdk_code']) == 'CDK-RETRY'

"""后台登录爆破防护：连续 5 次密码错误封 IP 24 小时。

覆盖三件事：
  1. 阈值与时长——第 5 次才封、封 24 小时、生效中的封禁不会被后续失败不断顺延；
  2. 计数语义——登录成功清零、不同 IP 互不影响、窗口过期后从 1 重新计数；
  3. 请求边界——被封 IP 在登录类接口和已认证的后台接口上都被 403 拦住并给出 Retry-After，
     而未带凭证的请求仍按 401 处理（不为一次注定失败的请求去查封禁表）。
"""
from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from smsinvit.app import Services,create_app
from smsinvit.common.auth import AdminIpGuard,remaining_seconds,utc_instant
from smsinvit.config import Config
from smsinvit.db import Database
from smsinvit.errors import Error
from smsinvit.security import Principal


class FakeBanDB:
    """只记录语句的最小替身：真正要验的是「什么时候发封禁 UPDATE」。"""

    def __init__(self,row=None):
        self.row=row
        self.calls=[]

    @contextmanager
    def transaction(self,requires_new=False):
        yield self

    def one(self,sql,*params):
        self.calls.append(('one',sql,params));return self.row

    def all(self,sql,*params):
        self.calls.append(('all',sql,params));return []

    def execute(self,sql,*params):
        self.calls.append(('execute',sql,params));return SimpleNamespace(rowcount=1)

    def statements(self,prefix):
        return [call for call in self.calls if call[0]=='execute' and call[1].strip().upper().startswith(prefix)]


@pytest.fixture
def config():
    return Config({'smsinvit.inventory-encryption-key':'a1'*32,
                   'smsinvit.inventory-fingerprint-key':'ip-ban-test-fingerprint',
                   'smsinvit.jwt-secret':'ip-ban-test-jwt-secret-at-least-32-characters',
                   'runtime.secure_cookies':False,'runtime.totp_enabled':False,
                   'runtime.cors_origins':'http://localhost:3001'},environ={})


@pytest.fixture
def codec(config):
    from smsinvit.security import SecretCodec
    return SecretCodec(config)


def guard(config,codec,row=None):
    return AdminIpGuard(FakeBanDB(row),codec)


# ---------------------------------------------------------------- 阈值与时长

def test_fifth_password_failure_issues_a_24_hour_ban(config,codec):
    future=(datetime.now(timezone.utc)+timedelta(hours=23)).replace(tzinfo=None)
    # 4 次不算，第 5 次才封：这里直接给出计数已达阈值的行。
    under=guard(config,codec,{'failureCount':4,'bannedUntil':None})
    assert under.record_failure('203.0.113.7','admin')=={'banned':False,'failureCount':4,'banHours':24}
    assert not under.mapper.db.statements('UPDATE')

    over=guard(config,codec,{'failureCount':5,'bannedUntil':None})
    outcome=over.record_failure('203.0.113.7','admin')
    assert outcome=={'banned':True,'failureCount':5,'banHours':24}
    bans=over.mapper.db.statements('UPDATE')
    assert len(bans)==1 and bans[0][2]==(24,'PASSWORD_FAILURES',over.key('203.0.113.7'))
    # 封禁时长是 24 小时：同一个常量既用于写库也用于对外展示。
    assert AdminIpGuard.BAN_HOURS==24 and AdminIpGuard.FAILURE_LIMIT==5
    assert AdminIpGuard.WINDOW_SECONDS==24*60*60


def test_an_already_active_ban_is_not_extended_by_later_failures(config,codec):
    # 封禁还剩 23 小时时又被记一次失败：不能再写一条 UPDATE 把到期时间往后推。
    active=(datetime.now(timezone.utc)+timedelta(hours=23)).replace(tzinfo=None)
    held=guard(config,codec,{'failureCount':9,'bannedUntil':active})
    assert held.record_failure('203.0.113.7','admin')['banned'] is False
    assert not held.mapper.db.statements('UPDATE')


def test_successful_login_clears_the_failure_counter(config,codec):
    cleared=guard(config,codec,None)
    cleared.clear('203.0.113.7')
    deletes=cleared.mapper.db.statements('DELETE')
    assert len(deletes)==1 and deletes[0][2]==(cleared.key('203.0.113.7'),)


def test_each_ip_has_its_own_row(config,codec):
    first,second=guard(config,codec,None),guard(config,codec,None)
    assert first.key('203.0.113.7')!=second.key('203.0.113.8')
    # 键里带前缀，避免与其它限流/审计用的哈希桶相撞。
    assert first.key('203.0.113.7')!=codec.sha256('203.0.113.7')


def test_remaining_seconds_parses_both_drivers(config,codec):
    soon=datetime.now(timezone.utc)+timedelta(minutes=30)
    assert 1700<remaining_seconds(soon.replace(tzinfo=None))<1810
    assert 1700<remaining_seconds(soon.isoformat().replace('+00:00','Z'))<1810
    assert remaining_seconds(None)==0
    assert remaining_seconds(datetime.now(timezone.utc)-timedelta(minutes=1))==0
    assert utc_instant('2026-10-11 08:00:00.000')==datetime(2026,10,11,8,0,tzinfo=timezone.utc)


# ---------------------------------------------------------------- 读取与解封

class BanSqlite(Database):
    """只读路径是普通 SELECT，不需要 MySQL 语法 shim。"""

    def _prepare(self,sql,params):
        return super()._prepare(sql,params)


@pytest.fixture
def sqlite_ban(tmp_path):
    path=tmp_path/'bans.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE admin_ip_bans (ip_hash TEXT PRIMARY KEY, ip_address TEXT NOT NULL,'
                           ' failure_count INTEGER NOT NULL DEFAULT 0, first_failure_at DATETIME NOT NULL,'
                           ' last_failure_at DATETIME NOT NULL, last_identifier TEXT, banned_until DATETIME,'
                           ' ban_reason TEXT, created_at DATETIME, updated_at DATETIME)')
    engine=create_engine('sqlite:///'+str(path),connect_args={'check_same_thread':False,'timeout':30},pool_size=5,max_overflow=5)
    return BanSqlite(engine=engine)


def seed_ban(db,codec,ip,*,seconds,count=5,identifier='admin'):
    stamp=datetime.now(timezone.utc)
    until=(stamp+timedelta(seconds=seconds)).replace(tzinfo=None) if seconds is not None else None
    db.execute('INSERT INTO admin_ip_bans(ip_hash,ip_address,failure_count,first_failure_at,last_failure_at,last_identifier,banned_until,ban_reason,created_at,updated_at)'
               ' VALUES (?,?,?,?,?,?,?,?,?,?)',
               codec.sha256('admin-ip-ban:'+ip),ip,count,stamp.replace(tzinfo=None),stamp.replace(tzinfo=None),identifier,
               until,'PASSWORD_FAILURES' if until else None,stamp.replace(tzinfo=None),stamp.replace(tzinfo=None))
    return codec.sha256('admin-ip-ban:'+ip)


def test_state_reports_active_ban_and_expired_rows_are_not_bans(sqlite_ban,config,codec):
    seed_ban(sqlite_ban,codec,'203.0.113.9',seconds=3600)
    seed_ban(sqlite_ban,codec,'203.0.113.10',seconds=-60)
    fresh=AdminIpGuard(sqlite_ban,codec)
    active=fresh.state('203.0.113.9')
    assert active['banned'] is True and 3500<active['secondsRemaining']<=3600
    expired=fresh.state('203.0.113.10')
    assert expired['banned'] is False and expired['secondsRemaining']==0
    assert fresh.state('203.0.113.11')['banned'] is False


def test_assert_allowed_raises_ip_banned_with_retry_after(sqlite_ban,config,codec):
    seed_ban(sqlite_ban,codec,'203.0.113.12',seconds=7200)
    fresh=AdminIpGuard(sqlite_ban,codec)
    with pytest.raises(Error) as failure:
        fresh.assert_allowed('203.0.113.12')
    error=failure.value
    assert (error.code,error.status)==('IP_BANNED',403)
    assert 7100<error.retry_after<=7200
    fresh.assert_allowed('203.0.113.13')


def test_unban_removes_the_row_and_rejects_a_malformed_key(sqlite_ban,config,codec):
    key=seed_ban(sqlite_ban,codec,'203.0.113.14',seconds=7200)
    fresh=AdminIpGuard(sqlite_ban,codec)
    assert fresh.unban(key)=={'removed':1}
    assert fresh.state('203.0.113.14')['banned'] is False
    with pytest.raises(Error):fresh.unban('not-a-hash')
    with pytest.raises(Error):fresh.unban(None)


# ---------------------------------------------------------------- 请求边界

def boundary_client(tmp_path,config,db,client_ip):
    static=tmp_path/'static';(static/'admin').mkdir(parents=True,exist_ok=True)
    (static/'index.html').write_text('<html>public</html>')
    (static/'admin/index.html').write_text('<html>admin</html>')
    config.runtime.update({'SMSINVIT_STATIC_DIR':str(static)})
    services=Services(db,config)
    services.limiter=SimpleNamespace(check=lambda *args:None)
    services.audit=SimpleNamespace(record=lambda *args:None)
    services.auth=SimpleNamespace(authenticate=lambda token:Principal('fixture-admin','fixture-session',('ADMIN',),False))
    app=create_app(config,db,services=services,include_domains=False,load_runtime=False)
    # TestClient 默认来源是 "testclient"；这里显式指定 IP，封禁判定才看得到真实的 key。
    return TestClient(app,client=(client_ip,51234)),services


def test_banned_ip_is_refused_on_login_endpoints_with_retry_after(tmp_path,config,codec,sqlite_ban):
    seed_ban(sqlite_ban,codec,'203.0.113.20',seconds=3600)
    client,_=boundary_client(tmp_path,config,sqlite_ban,'203.0.113.20')
    # 登录接口本身就是爆破入口，必须连验证码都拿不到。
    response=client.get('/api/v1/admin/auth/captcha')
    assert response.status_code==403
    assert response.json()['error']['code']=='IP_BANNED'
    assert response.headers['retry-after'].isdigit() and 3500<int(response.headers['retry-after'])<=3600


def test_banned_ip_is_refused_on_authenticated_admin_routes(tmp_path,config,codec,sqlite_ban):
    seed_ban(sqlite_ban,codec,'203.0.113.21',seconds=3600)
    client,_=boundary_client(tmp_path,config,sqlite_ban,'203.0.113.21')
    response=client.get('/api/v1/admin/plans',headers={'Authorization':'Bearer fixture'})
    assert response.status_code==403 and response.json()['error']['code']=='IP_BANNED'


def test_unauthenticated_requests_still_get_401_without_touching_the_ban_table(tmp_path,config,codec,sqlite_ban):
    # 顺序是「先看有没有凭证，再判封禁」：没带 token 的请求连封禁表都不查。
    seed_ban(sqlite_ban,codec,'203.0.113.22',seconds=3600)
    client,_=boundary_client(tmp_path,config,sqlite_ban,'203.0.113.22')
    assert client.get('/api/v1/admin/plans').status_code==401
    # 带上凭证后同一个 IP 才会被 403 挡住。
    assert client.get('/api/v1/admin/plans',headers={'Authorization':'Bearer fixture'}).status_code==403


def test_other_ips_are_unaffected(tmp_path,config,codec,sqlite_ban):
    seed_ban(sqlite_ban,codec,'203.0.113.23',seconds=3600)
    client,_=boundary_client(tmp_path,config,sqlite_ban,'203.0.113.24')
    response=client.get('/api/v1/admin/plans',headers={'Authorization':'Bearer fixture'})
    assert response.status_code!=403


# ---------------------------------------------------------------- 真实 MySQL 写入路径

ROOT=Path(__file__).resolve().parents[3]


@pytest.fixture
def mysql_guard():
    """隔离 MySQL 上验证写入 SQL：阈值、24 小时、窗口重置与封禁不顺延。

    `bumpFailure` 用 `ON DUPLICATE KEY UPDATE` 加 `IF()`，SQLite 替身跑不了，
    所以这一段只在隔离库可用时执行；没有凭据就跳过。
    """
    import os
    from sqlalchemy import URL
    path=Path(os.environ.get('SMSINVIT_TEST_MYSQL_CREDENTIALS_FILE',str(ROOT/'data/test-private/mysql.json')))
    if not path.exists():pytest.skip('isolated MySQL credentials not configured')
    selected=json.loads(path.read_text())
    config=Config({'smsinvit.inventory-encryption-key':'a1'*32,'smsinvit.inventory-fingerprint-key':'ip-ban-mysql-key','smsinvit.jwt-secret':'ip-ban-mysql-jwt-over32-characters'},environ={})
    db=Database(URL.create('mysql+pymysql',username=selected.get('user','root'),password=selected['password'],host=selected.get('host','127.0.0.1'),port=selected.get('port',13308),database=selected.get('database','smsinvit_test')))
    from smsinvit.security import SecretCodec
    codec=SecretCodec(config)
    fresh=AdminIpGuard(db,codec)
    # 198.51.100.0/24 是 RFC 5737 的文档网段，永远不会是真实来源地址。
    address='198.51.100.77'
    fresh.clear(address)
    try:yield fresh,codec,address
    finally:fresh.clear(address);db.close()


def test_mysql_counts_to_five_then_bans_for_24_hours(mysql_guard):
    fresh,codec,address=mysql_guard
    for expected in (1,2,3,4):
        outcome=fresh.record_failure(address,'admin')
        assert outcome=={'banned':False,'failureCount':expected,'banHours':24}
    assert fresh.state(address)['banned'] is False
    fifth=fresh.record_failure(address,'admin')
    assert fifth=={'banned':True,'failureCount':5,'banHours':24}
    state=fresh.state(address)
    assert state['banned'] is True and 86_000<state['secondsRemaining']<=86_400


def test_mysql_does_not_extend_an_active_ban(mysql_guard):
    fresh,codec,address=mysql_guard
    for _ in range(5):outcome=fresh.record_failure(address,'admin')
    assert outcome['banned'] is True
    fresh.forget()  # 丢掉内存缓存，强制回库读真实到期时间
    before=fresh.state(address)['secondsRemaining']
    # 封禁生效期间再来三次失败：计数继续涨，但到期时间不能被推后。
    for _ in range(3):fresh.record_failure(address,'other-account')
    fresh.forget()
    after=fresh.state(address)
    assert after['banned'] is True
    assert after['secondsRemaining']<=before
    assert after['failureCount']==8


def test_mysql_resets_the_counter_once_the_window_expires(mysql_guard):
    fresh,codec,address=mysql_guard
    for _ in range(4):fresh.record_failure(address,'admin')
    fresh.forget()
    # 把最后一次失败挪到窗口之外：下一次失败应当从 1 重新计数，而不是直接封禁。
    fresh.mapper.db.execute('UPDATE admin_ip_bans SET last_failure_at=DATE_SUB(NOW(3),INTERVAL 25 HOUR) WHERE ip_hash=?',fresh.key(address))
    assert fresh.record_failure(address,'admin')=={'banned':False,'failureCount':1,'banHours':24}


def test_mysql_unban_removes_the_row_entirely(mysql_guard):
    fresh,codec,address=mysql_guard
    for _ in range(5):fresh.record_failure(address,'admin')
    key=fresh.key(address)
    assert fresh.state(address)['banned'] is True
    assert fresh.unban(key)=={'removed':1}
    fresh.forget()
    assert fresh.state(address)=={'key':key,'banned':False,'secondsRemaining':0,'failureCount':0}

"""SOCKS5 支持：录入归一、按 id 原地切换、故障记账、CLI 代理变量分流。

`url` 是代理池的唯一键，所以"改协议"如果靠重写整池文本，等于删旧行插新行：
use_count / 409 生命周期 / permanent_dead 全部归零；而且没有凭证权限时前端拿到的
地址是脱敏的，根本重写不出来。这里验证按 id 原地切换保留了同一个行与统计。
"""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine

# registrar/codex_cli 在模块级读这两个目录，必须在导入 webui.* 之前存在。
os.environ.setdefault('SMSINVIT_INVITATION_LOG_DIR', tempfile.mkdtemp(prefix='invitation-logs-'))
os.environ.setdefault('CODEX_HOMES_ROOT', tempfile.mkdtemp(prefix='codex-homes-'))

from smsinvit.db import Database  # noqa: E402
from smsinvit.invitation import mysql  # noqa: E402
from smsinvit.security import SecretCodec  # noqa: E402
from webui import db as pooldb  # noqa: E402
from webui import proxy_pool  # noqa: E402

SCHEMA = """
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


class ProxySqliteDatabase(Database):
    def _prepare(self, sql, params):
        sql = sql.replace(' FOR UPDATE SKIP LOCKED', '').replace(' FOR UPDATE', '')
        sql = sql.replace(' ON DUPLICATE KEY UPDATE fingerprint=VALUES(fingerprint)',
                          ' ON CONFLICT(fingerprint) DO NOTHING')
        sql = sql.replace('INSERT IGNORE', 'INSERT OR IGNORE')
        sql = sql.replace('UTC_TIMESTAMP(3)', 'CURRENT_TIMESTAMP')
        return super()._prepare(sql, params)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    path = tmp_path / 'proxy.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
    engine = create_engine('sqlite:///' + str(path),
                           connect_args={'check_same_thread': False, 'timeout': 30},
                           pool_size=10, max_overflow=10, hide_parameters=True)
    db = ProxySqliteDatabase(engine=engine)
    monkeypatch.setattr(mysql, '_services', None)
    monkeypatch.setattr(mysql, '_runtime_owner', None)
    monkeypatch.setattr(mysql, '_cache', {})
    monkeypatch.setattr(mysql, '_redaction_values', set())
    mysql.configure(SimpleServices(db))
    yield db
    db.close()


class SimpleServices:
    def __init__(self, db):
        self.db = db
        self.codec = SecretCodec('a1' * 32, 'proxy-scheme-fingerprint')


def row(pool, ident):
    # 直连数据库时要用真实表名；pooldb 走翻译层才会把 proxy_pool 改写成它。
    # url 是机密列（库里存的是加密标记），直连读要自己揭开。
    data = pool.one('SELECT * FROM invitation_v2_proxy_pool WHERE id=?', ident)
    if data:
        data['url'] = mysql.revealed(data['url'])
    return data


def events(pool, pid):
    return [{'event': item['event'], 'detail': mysql.revealed(item['detail'])}
            for item in pool.all('SELECT event,detail FROM invitation_v2_proxy_events '
                                 'WHERE proxy_id=? ORDER BY id', pid)]


def add(pool, url, **fields):
    # 走真实写入路径：url 会被登记成加密标记，和 proxy_add_many 一致。
    pool.execute('INSERT INTO invitation_v2_proxy_pool(url,enabled,added_at,updated_at) VALUES (?,1,?,?)',
                 mysql.protected(url), 1.0, 1.0)
    pid = pool.scalar('SELECT id FROM invitation_v2_proxy_pool WHERE url=?', mysql.marker(url))
    if fields:
        sets = ','.join(f'{key}=?' for key in fields)
        pool.execute(f'UPDATE invitation_v2_proxy_pool SET {sets} WHERE id=?', *fields.values(), pid)
    return pid


# ── 录入归一 ──


def test_socks5_input_is_normalized_to_remote_dns(pool):
    assert proxy_pool.parse_pool_text('socks5://user:pass@host.example:1080') == \
        ['socks5h://user:pass@host.example:1080']
    assert proxy_pool.parse_pool_text('socks5h://host.example:1080') == \
        ['socks5h://host.example:1080']
    # 不带协议的 legacy 四段式默认是 http，不会莫名其妙变成 SOCKS。
    assert proxy_pool.parse_pool_text('host.example:1080:user:pass') == \
        ['http://user:pass@host.example:1080']


# ── 按 id 原地切换协议 ──


def test_switching_scheme_keeps_the_same_row_and_its_statistics(pool):
    pid = add(pool, 'http://user:pass@host.example:8080',
              use_count=7, success_count=5, fail_count=2, consecutive_409=1, total_409=3,
              permanent_dead=1, enabled=0,
              consecutive_409_codex=1, permanent_dead_codex=1)
    result = pooldb.proxy_set_scheme(pid, 'socks5')
    assert result['id'] == pid and result['changed'] is True
    assert result['url'] == 'socks5h://user:pass@host.example:8080'
    current = row(pool, pid)
    assert current['url'] == 'socks5h://user:pass@host.example:8080'
    # 统计与生命周期都还在（换协议不是换行）。
    assert current['use_count'] == 7 and current['success_count'] == 5 and current['fail_count'] == 2
    assert current['total_409'] == 3
    # 但"HTTP 永久不可用"的锁不能带过去，否则切了也是死的。
    assert current['permanent_dead'] == 0 and current['permanent_dead_codex'] == 0
    assert current['enabled'] == 1 and current['frozen_until'] is None
    logged = events(pool, pid)
    assert [event['event'] for event in logged] == ['scheme']
    assert 'http://user:pass@host.example:8080 -> socks5h://' in logged[0]['detail']


def test_switching_scheme_can_keep_the_locks_when_asked(pool):
    pid = add(pool, 'http://host.example:8080', permanent_dead=1)
    pooldb.proxy_set_scheme(pid, 'https', clear_locks=False)
    current = row(pool, pid)
    assert current['url'] == 'https://host.example:8080' and current['permanent_dead'] == 1


def test_switching_to_the_same_scheme_is_a_noop_that_keeps_the_address(pool):
    pid = add(pool, 'socks5h://host.example:1080')
    result = pooldb.proxy_set_scheme(pid, 'socks5')
    assert result['changed'] is False and result['url'] == 'socks5h://host.example:1080'


def test_switching_scheme_refuses_an_unknown_protocol(pool):
    pid = add(pool, 'http://host.example:8080')
    with pytest.raises(ValueError):
        pooldb.proxy_set_scheme(pid, 'socks4')
    assert row(pool, pid)['url'] == 'http://host.example:8080'


def test_switching_scheme_refuses_a_colliding_address(pool):
    first = add(pool, 'http://host.example:8080')
    second = add(pool, 'socks5h://host.example:8080')
    with pytest.raises(sqlite3.IntegrityError):
        pooldb.proxy_set_scheme(first, 'socks5h')
    assert row(pool, first)['url'] == 'http://host.example:8080'
    assert row(pool, second)['url'] == 'socks5h://host.example:8080'


def test_switching_an_unknown_id_reports_missing(pool):
    assert pooldb.proxy_set_scheme(9999, 'socks5') is None


# ── 运行时把 SOCKS 故障认成传输失败 ──


@pytest.mark.parametrize('error', [
    'curl: (97) SOCKS closed',
    'socks5 handshake failed',
    'SOCKS handshake error',
    'curl: (7) Failed to connect to proxy port 1080',
    'codex 超时 timeout（60s，代理太慢或不可用）',
])
def test_socks_failures_are_transport_errors(error):
    assert proxy_pool.is_transport_error(error) is True


@pytest.mark.parametrize('error', [
    'authorize/continue 失败: HTTP 409 invalid_state',
    'model is not supported',
])
def test_business_rejections_are_still_not_transport_errors(error):
    assert proxy_pool.is_transport_error(error) is False


# ── CLI 子进程按协议注入代理变量 ──


def _jwt(exp_delta):
    import base64
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    import time as _time
    return f"{part({'alg':'none'})}.{part({'exp':int(_time.time())+exp_delta})}.sig"


@pytest.fixture
def turn_env(monkeypatch, tmp_path):
    from smsinvit.invitation import safety
    from webui import codex_turn
    (tmp_path / 'homes').mkdir()
    monkeypatch.setenv('CODEX_HOMES_ROOT', str(tmp_path / 'homes'))
    seen = {}

    def fake(cmd, *, env, cwd, timeout, **kwargs):
        seen.update(env)
        return ('{"type":"item.completed","item":{"type":"agent_message","text":"你好"}}\n'
                '{"type":"turn.completed"}\n', "", 0, False,
                {"thread_ms": 1, "turn_ms": 2, "completed_ms": 3, "reply_ms": 3, "exit_ms": 4})

    monkeypatch.setattr(codex_turn, '_run_codex_process', fake)
    monkeypatch.setattr(codex_turn, 'find_codex_binary', lambda: '/bin/true')
    monkeypatch.setattr(safety, 'require_binary', lambda binary: None)
    monkeypatch.setattr(safety, 'child_environment', lambda: {})
    validated = []
    monkeypatch.setattr(safety, 'require_proxy', lambda proxy: validated.append(proxy) or ('host.example', 1080, ('203.0.113.9',)))
    return codex_turn, seen, validated


def test_a_socks_proxy_reaches_the_cli_through_all_proxy_and_the_http_form(turn_env):
    """SOCKS 既走 ALL_PROXY，也要把同端口的 http:// 形式补进 HTTP(S)_PROXY。

    生产事故（2026-10-11）：codex CLI 是 Rust 二进制，**不认 ALL_PROXY 里的 SOCKS**，
    于是照样直连 → 启动期的模型目录刷新 / workspace routing discovery 被 Cloudflare
    秒拒 403（同一时刻 Python 侧走同一代理是通的），整轮以
    `workspace routing discovery failed` 结束。代理端口同时提供 HTTP 代理，
    补上 http:// 形式后同一轮立刻成功（退出码 0，拿到 agent_message）。

    注意：`http://` 形式是**转换后**的值，不是把 `socks5h://` 原样塞进 HTTP_PROXY——
    后者会被只认 HTTP 代理的客户端误解析，那一条仍然不成立。
    """
    codex_turn, seen, validated = turn_env
    proxy = 'socks5h://user:pass@host.example:1080'
    assert codex_turn.run_codex_turn(email='socks@example.test', access_token=_jwt(3600),
                                     refresh_token='rt', proxy=proxy)['ok'] is True
    # ALL_PROXY 保留 SOCKS 原文（标准入口）
    assert seen['ALL_PROXY'] == proxy and seen['all_proxy'] == proxy
    # HTTP(S)_PROXY 用转换后的 http:// 形式
    http_form = 'http://user:pass@host.example:1080'
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        assert seen[name] == http_form, name
    assert validated == [proxy]


def test_an_http_proxy_still_reaches_the_cli_through_every_variable(turn_env):
    codex_turn, seen, validated = turn_env
    proxy = 'http://user:pass@host.example:8080'
    assert codex_turn.run_codex_turn(email='http@example.test', access_token=_jwt(3600),
                                     refresh_token='rt', proxy=proxy)['ok'] is True
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy'):
        assert seen[name] == proxy, name
    assert validated == [proxy]

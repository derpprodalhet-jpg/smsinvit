"""并发注册的正确性：extra_json 合并、接码状态互不串行。

为什么单独测：账号工厂会**并发**注册多个号（pool_register_concurrency），
而这几处都是"读-改-写"或共享状态，写错就会互相覆盖：
  · `save_registered` 的 extra_json（会话罐 cookie_jar 就在里面）；
  · `mark_phone_bound` 的接码状态。
"""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from invitation_sms_sqlite import isolated_sms_database
from smsinvit.invitation import mysql
from smsinvit.security import SecretCodec
from webui import db as dbmod

EXTRA_SCHEMA = """
CREATE TABLE invitation_v2_registered (
  email TEXT PRIMARY KEY, password TEXT, access_token TEXT, session_token TEXT,
  refresh_token TEXT, id_token TEXT, device_id TEXT, csrf_token TEXT, cookie_header TEXT,
  totp_secret TEXT, totp_factor_id TEXT, proxy TEXT, source TEXT, extra_json TEXT, created_at REAL);
CREATE TABLE invitation_v2_outlook_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, kind TEXT, status TEXT,
  phone_bound_at REAL, phone_bound_ok INTEGER NOT NULL DEFAULT -1, fail_reason TEXT);
"""


@pytest.fixture
def regdb(tmp_path, monkeypatch):
    """带仓库层（事务 + 机密字段加密）的隔离库：这些函数都带 @transactional 包装。"""
    db = isolated_sms_database(tmp_path / "reg.sqlite3")
    services = SimpleNamespace(db=db, codec=SecretCodec("c1" * 32, "isolated-reg-fingerprints"))
    monkeypatch.setattr(mysql, "_services", None)
    monkeypatch.setattr(mysql, "_runtime_owner", None)
    monkeypatch.setattr(mysql, "_cache", {})
    monkeypatch.setattr(mysql, "_redaction_values", set())
    mysql.configure(services)
    with db.transaction():
        for statement in EXTRA_SCHEMA.strip().split(";"):
            if statement.strip():
                db.execute(statement)
    yield db


def extra_of(db, email):
    """extra_json 是机密列（整列加密），必须经 revealed 读回。"""
    raw = db.scalar("SELECT extra_json FROM invitation_v2_registered WHERE email=?", email)
    return json.loads(mysql.revealed(raw)) if raw else {}


def test_partial_save_keeps_the_cookie_jar(regdb):
    """只传部分字段的保存（_persist_restored_session / _complete_cli）不能把罐冲掉。

    这是真实会发生的顺序：注册落库时写入 cookie_jar → 之后授权阶段用部分字段
    再保存一次（会话回写/CLI 回写），以前那次保存会把 extra_json 整列覆盖掉。
    """
    dbmod.save_registered({"email": "a@example.test", "password": "pw",
                           "cookie_jar": '[{"name":"s"}]'})
    dbmod.save_registered({"email": "a@example.test", "password": "pw2",
                           "csrf_token": "csrf-1", "meta": "kept"})
    extra = extra_of(regdb, "a@example.test")
    assert extra.get("cookie_jar") == '[{"name":"s"}]'      # 罐必须活下来
    assert extra.get("meta") == "kept"
    assert mysql.revealed(regdb.scalar(
        "SELECT csrf_token FROM invitation_v2_registered WHERE email=?", "a@example.test")) == "csrf-1"


def test_partial_save_never_clears_existing_tokens(regdb):
    """注册成功后只回写 cookie 罐/2FA 的保存，绝不能把已拿到的凭证冲成空。

    真实事故（2026-10-08/09）：注册时 at=1986/st=4115 都拿到了，随后
    `_refresh_cookie_jar` 只传 email + cookie_jar 调 save_registered，
    整行 REPLACE 把 access_token/session_token/refresh_token 清空，
    之后 Codex 授权只能重走登录。这里锁死"空值不覆盖非空旧值"。
    """
    dbmod.save_registered({"email": "t@example.test", "password": "pw",
                           "access_token": "at-old", "session_token": "st-old",
                           "refresh_token": "rt-old", "cookie_header": "ck-old"})
    # 只回写会话罐（生产里 _refresh_cookie_jar 就是这个形状）
    dbmod.save_registered({"email": "t@example.test", "cookie_jar": "jar-1"})
    row = regdb.one("SELECT access_token, session_token, refresh_token, cookie_header "
                    "FROM invitation_v2_registered WHERE email=?", "t@example.test")
    assert (mysql.revealed(row["access_token"]), mysql.revealed(row["session_token"]),
            mysql.revealed(row["refresh_token"]), mysql.revealed(row["cookie_header"])) == \
           ("at-old", "st-old", "rt-old", "ck-old")
    assert extra_of(regdb, "t@example.test").get("cookie_jar") == "jar-1"
    # 传了新值就必须覆盖（本轮真的刷新到新 token 的场景）
    dbmod.save_registered({"email": "t@example.test", "access_token": "at-new",
                           "session_token": "st-new"})
    row = regdb.one("SELECT access_token, session_token, refresh_token "
                    "FROM invitation_v2_registered WHERE email=?", "t@example.test")
    assert mysql.revealed(row["access_token"]) == "at-new"
    assert mysql.revealed(row["session_token"]) == "st-new"
    assert mysql.revealed(row["refresh_token"]) == "rt-old"


def test_concurrent_saves_of_different_accounts_never_interfere(regdb):
    """账号工厂会**并发注册多个号**：每个号的会话罐/元数据必须各归各的。

    同一个邮箱的并发写不属于生产场景 —— 门卫按邮箱互斥（同一邮箱任何时刻只跑一个任务），
    所以这里只保证"不同账号并行互不影响"，这也是并发注册真正会走到的路径。
    """
    def save(i):
        dbmod.save_registered({"email": f"pool{i}@example.test",
                               "cookie_jar": f"jar-{i}", "meta": f"v{i}"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(8)))
    for i in range(8):
        extra = extra_of(regdb, f"pool{i}@example.test")
        assert extra.get("cookie_jar") == f"jar-{i}", (i, extra)
        assert extra.get("meta") == f"v{i}", (i, extra)


def test_concurrent_phone_bound_writes_only_touch_their_own_account(regdb):
    with regdb.transaction():
        for i in range(6):
            regdb.execute("INSERT INTO invitation_v2_outlook_accounts(email,kind,status) VALUES (?,?,?)",
                          f"a{i}@example.test", "cf_temp", "registered")

    def mark(i):
        dbmod.mark_phone_bound(f"a{i}@example.test", i % 2 == 0,
                               "" if i % 2 == 0 else f"fail-{i}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(mark, range(6)))
    for i in range(6):
        assert regdb.scalar("SELECT phone_bound_ok FROM invitation_v2_outlook_accounts WHERE email=?",
                            f"a{i}@example.test") == (1 if i % 2 == 0 else 0), i
        reason = mysql.revealed(regdb.scalar(
            "SELECT fail_reason FROM invitation_v2_outlook_accounts WHERE email=?", f"a{i}@example.test")) or ""
        assert reason == ("" if i % 2 == 0 else f"fail-{i}")


def test_phone_bound_counts_ignore_finished_accounts(regdb):
    with regdb.transaction():
        for email, kind, status, bound in (("r1@x", "cf_temp", "registered", 1),
                                           ("r2@x", "cf_temp", "registered", 0),
                                           ("d1@x", "cf_temp", "done", 1)):
            regdb.execute("INSERT INTO invitation_v2_outlook_accounts(email,kind,status,phone_bound_ok) VALUES (?,?,?,?)",
                          email, kind, status, bound)
    assert dbmod.count_phone_bound("", True) == 1
    assert dbmod.count_phone_bound("", False) == 1
    assert dbmod.count_phone_bound("cf_temp", True) == 1
    assert dbmod.count_phone_bound("outlook", True) == 0

"""会话罐（cookie 罐）与 Sentinel PoW 行为补齐。

背景：拆成"注册到 2FA"+"兑换再授权"后，Codex authorize 在 auth.openai.com 侧
没有会话（实测 SSO 0/3），每次都要重新登录。把登录成功时的整个 cookie 罐存进
`extra_json` 并在下次重放，authorize 才有可能直接过（省掉整轮登录 ~27s）。
PoW 那边：JS 的"模拟人类操作"是**补齐等待**，写死 4200ms 等于每次白等 1-2s。
"""
import json
import os
import tempfile
import threading
from types import SimpleNamespace

import pytest

# webui.registrar 在导入时读日志目录，必须在 import webui 之前设好
os.environ.setdefault("SMSINVIT_INVITATION_LOG_DIR", tempfile.mkdtemp(prefix="invitation-logs-"))
os.environ.setdefault("CODEX_HOMES_ROOT", tempfile.mkdtemp(prefix="codex-homes-"))

from smsinvit.invitation.runtime import TASK_SETTINGS  # noqa: E402
from sentinel_quickjs import behavior_duration_ms  # noqa: E402
from webui import codex_auth  # noqa: E402


class FakeJar:
    """最小 cookie 罐：既能被遍历（抓取），也能 set（重放）。"""

    def __init__(self, items=()):
        self.items = [SimpleNamespace(name=n, value=v, domain=d, path=p) for n, v, d, p in items]

    def __iter__(self):
        return iter(self.items)

    def set(self, name, value, domain=None, path="/"):
        self.items.append(SimpleNamespace(name=name, value=value, domain=domain or "", path=path))


def flow_with(jar):
    return SimpleNamespace(session=SimpleNamespace(cookies=jar))


# ──────────────────────── 会话罐 ────────────────────────


def test_cookie_jar_round_trip_keeps_domain_and_path():
    src = flow_with(FakeJar([
        ("__Secure-next-auth.session-token", "sess", ".openai.com", "/"),
        ("oai-did", "did-1", ".chatgpt.com", "/"),
    ]))
    jar = codex_auth.capture_cookie_jar(src)
    assert jar and "openai.com" in jar

    dst_jar = FakeJar()
    restored = codex_auth._replay_cookie_jar(flow_with(dst_jar), jar)
    assert restored == 2
    kept = {(c.name, c.value, c.domain) for c in dst_jar}
    assert ("__Secure-next-auth.session-token", "sess", ".openai.com") in kept
    assert ("oai-did", "did-1", ".chatgpt.com") in kept


def test_capture_skips_empty_values_and_survives_a_broken_jar():
    assert codex_auth.capture_cookie_jar(flow_with(FakeJar([("a", "", ".openai.com", "/")]))) == ""
    broken = SimpleNamespace(session=SimpleNamespace(cookies=SimpleNamespace(__iter__=None)))
    assert codex_auth.capture_cookie_jar(broken) == ""
    assert codex_auth._replay_cookie_jar(flow_with(FakeJar()), "not json") == 0


def test_probe_detects_the_auth_openai_session_only():
    """探针决定"先试 SSO"还是"这次 authorize 就是建登录会话"，必须只看 openai.com 侧。"""
    chatgpt_only = flow_with(FakeJar([("oai-did", "d", ".chatgpt.com", "/"),
                                      ("__Secure-next-auth.session-token", "s", ".chatgpt.com", "/")]))
    assert codex_auth._has_auth_openai_session(chatgpt_only) is False
    with_openai = flow_with(FakeJar([("__Secure-next-auth.session-token", "s", ".openai.com", "/")]))
    assert codex_auth._has_auth_openai_session(with_openai) is True
    assert codex_auth._has_auth_openai_session(flow_with(FakeJar())) is False


# ──────────────────────── PoW 行为补齐 ────────────────────────


def test_behavior_duration_defaults_and_clamps():
    token = TASK_SETTINGS.set({})
    try:
        assert behavior_duration_ms() == 1200
    finally:
        TASK_SETTINGS.reset(token)
    for raw, expected in (("0", 0), ("500", 500), ("99999", 10000), ("-5", 0), ("abc", 1200), ("", 1200)):
        token = TASK_SETTINGS.set({"sentinel_behavior_ms": raw})
        try:
            assert behavior_duration_ms() == expected, raw
        finally:
            TASK_SETTINGS.reset(token)


def test_behavior_duration_is_per_task_not_shared():
    """多个账号并发注册时，各自读自己的配置（contextvar 隔离，无全局缓存）。"""
    seen = {}

    def worker(name, value):
        token = TASK_SETTINGS.set({"sentinel_behavior_ms": value})
        try:
            threading.Event().wait(0.01)
            seen[name] = behavior_duration_ms()
        finally:
            TASK_SETTINGS.reset(token)

    threads = [threading.Thread(target=worker, args=(n, v)) for n, v in
               (("a", "0"), ("b", "3000"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"a": 0, "b": 3000}


def test_pow_payload_carries_the_configured_behavior_duration(monkeypatch, tmp_path):
    import sentinel_quickjs

    script = tmp_path / "sdk.js"
    script.write_text("// fixture")
    captured = {}

    def fake_action(**kwargs):
        captured.update(kwargs.get("payload") or {})
        return {"request_p": "rp"} if kwargs.get("action") == "requirements" else {"token": "t", "so_token": ""}

    monkeypatch.setattr(sentinel_quickjs, "_run_quickjs_action", fake_action)
    monkeypatch.setattr(sentinel_quickjs, "_ensure_sdk_file", lambda *a, **k: "/tmp/sdk.js")
    monkeypatch.setattr(sentinel_quickjs, "_quickjs_script_path", lambda: script)
    monkeypatch.setattr(sentinel_quickjs, "_fetch_sentinel_challenge",
                        lambda *a, **k: {"token": "c", "so": {"required": False}})
    token = TASK_SETTINGS.set({"sentinel_behavior_ms": "0"})
    try:
        sentinel_quickjs.get_sentinel_token_via_quickjs(
            SimpleNamespace(), device_id="d", flow="authorize_continue", log=lambda *_: None,
        )
    finally:
        TASK_SETTINGS.reset(token)
    assert captured.get("behavior_duration_ms") == 0


# ──────────────────────── 用**真实 session** 验证罐（这次踩的坑）────────────────────────


def test_real_curl_cffi_session_round_trip():
    """真会话：curl_cffi 的 `cookies` 迭代出来是**字符串名**，对象在 `.jar` 里。

    之前的实现直接迭代 `cookies`，`getattr(cookie,"name")` 全空 → 每个都被跳过 →
    罐永远是空串，而且**没有异常**（静默失败）。这条测试用真实 session 钉住它。
    """
    from smsinvit.invitation.safety import GuardedCffiSession

    src = GuardedCffiSession(impersonate="chrome136")
    src.cookies.set("__Secure-next-auth.session-token", "sess-value", domain=".openai.com")
    src.cookies.set("oai-did", "did-value", domain=".chatgpt.com")
    flow = SimpleNamespace(session=src)

    jar = codex_auth.capture_cookie_jar(flow)
    assert jar, "真实 session 必须能抓到 cookie"
    items = {item["name"]: item for item in json.loads(jar)}
    assert items["__Secure-next-auth.session-token"]["value"] == "sess-value"
    assert items["__Secure-next-auth.session-token"]["domain"] == ".openai.com"
    assert codex_auth._has_auth_openai_session(flow) is True

    dst = GuardedCffiSession(impersonate="chrome136")
    assert codex_auth._replay_cookie_jar(SimpleNamespace(session=dst), jar) == 2
    assert dst.cookies.get("oai-did") == "did-value"
    assert codex_auth._has_auth_openai_session(SimpleNamespace(session=dst)) is True
    src.close()
    dst.close()


def test_empty_jar_is_logged_loudly(caplog):
    """空罐必须留痕：上次就是因为静默为空，查了半天。"""
    import logging

    jar = FakeJar()
    with caplog.at_level(logging.WARNING):
        assert codex_auth.capture_cookie_jar(flow_with(jar)) == ""
    assert "会话罐为空" in caplog.text


# ─────────────── 罐年龄闸门：过期罐的认证 cookie 有害，必须丢弃 ───────────────


def test_expired_jar_does_not_replay_auth_openai_cookies():
    """实测：罐过期后回放 auth.openai.com 的旧 cookie 会让重新登录后卡在授权确认页。

    所以超过免登录窗口后，只回放 chatgpt.com 的会话；认证侧 cookie 一律丢弃。
    """
    jar = json.dumps([
        {"name": "__Secure-next-auth.session-token.1", "value": "old", "domain": ".openai.com", "path": "/"},
        {"name": "oai-client-auth-session", "value": "old2", "domain": "auth.openai.com", "path": "/"},
        {"name": "__Secure-next-auth.session-token", "value": "keep", "domain": ".chatgpt.com", "path": "/"},
        {"name": "oai-did", "value": "did", "domain": ".chatgpt.com", "path": "/"},
    ])

    stale = FakeJar()
    restored = codex_auth._replay_cookie_jar(flow_with(stale), jar, include_auth_openai=False)
    assert restored == 2
    assert sorted(c.name for c in stale) == ["__Secure-next-auth.session-token", "oai-did"]

    fresh = FakeJar()
    assert codex_auth._replay_cookie_jar(flow_with(fresh), jar, include_auth_openai=True) == 4


def test_sso_jar_window_defaults_and_is_configurable():
    token = TASK_SETTINGS.set({})
    try:
        assert codex_auth._sso_jar_max_age_minutes() == 25.0
    finally:
        TASK_SETTINGS.reset(token)
    for raw, expected in (("0", 0.0), ("90", 90.0), ("99999", 1440.0), ("abc", 25.0), ("", 25.0)):
        token = TASK_SETTINGS.set({"cookie_jar_sso_max_minutes": raw})
        try:
            assert codex_auth._sso_jar_max_age_minutes() == expected, raw
        finally:
            TASK_SETTINGS.reset(token)


def test_unknown_jar_age_is_treated_as_stale_and_replays_no_auth_openai_cookies():
    """根因回归（2026-10-09 实测）：罐**年龄未知**时必须当过期处理。

    号池工厂写罐的路径不带 `cookie_jar_at`。旧逻辑把"未知"归到"新鲜"一侧，于是回放
    auth.openai.com 的旧 cookie：PoW+TOTP 都过，却卡在 Codex 授权确认页，**三次重试
    全废**（同一个号连续 16 次尝试同一死法）。按过期处理后，同一个号立刻走通。
    """
    import time as _time

    assert codex_auth._sso_jar_is_fresh(_time.time() - 60, 25.0) is True     # 窗口内
    assert codex_auth._sso_jar_is_fresh(_time.time() - 60 * 60, 25.0) is False  # 超窗口
    assert codex_auth._sso_jar_is_fresh(0, 25.0) is False                   # 未知（缺时间戳）
    assert codex_auth._sso_jar_is_fresh(None, 25.0) is False                # 未知（空值）
    assert codex_auth._sso_jar_is_fresh("not-a-number", 25.0) is False      # 脏值

    # 未知年龄 → 走"只回放 chatgpt.com"那条路：auth.openai.com 的 cookie 一条都不进。
    jar = json.dumps([
        {"name": "oai-client-auth-session", "value": "old", "domain": "auth.openai.com", "path": "/"},
        {"name": "__Secure-next-auth.session-token.1", "value": "old2", "domain": ".openai.com", "path": "/"},
        {"name": "__Secure-next-auth.session-token", "value": "keep", "domain": ".chatgpt.com", "path": "/"},
        {"name": "oai-did", "value": "did", "domain": ".chatgpt.com", "path": "/"},
    ])
    stale = FakeJar()
    restored = codex_auth._replay_cookie_jar(
        flow_with(stale), jar,
        include_auth_openai=codex_auth._sso_jar_is_fresh(0, codex_auth._sso_jar_max_age_minutes()),
    )
    assert restored == 2
    assert sorted(c.name for c in stale) == ["__Secure-next-auth.session-token", "oai-did"]


def test_writing_a_new_jar_always_stamps_cookie_jar_at():
    """写罐就带时间；但**继承来的**旧罐绝不能被盖章（否则旧罐会被当成新鲜罐）。

    历史导入/旧调用方只写 `cookie_jar` 不写 `cookie_jar_at` → 年龄未知 → 走"当过期处理"
    （安全但多一次登录）。补时间戳只针对**本轮新抓的罐**。
    """
    from webui import db as invitation_db

    stamped = invitation_db._stamp_new_jar({"cookie_jar": "[]", "device_id": "d"})
    assert stamped["cookie_jar_at"] > 0 and stamped["device_id"] == "d"

    carried = invitation_db._stamp_new_jar({"device_id": "d"})      # 本轮没带罐
    assert "cookie_jar_at" not in carried

    given = invitation_db._stamp_new_jar({"cookie_jar": "[]", "cookie_jar_at": 123.0})
    assert given["cookie_jar_at"] == 123.0


def test_stale_jar_cleanup_removes_only_auth_openai_cookies():
    """清 cookie 只能清 openai.com 域：chatgpt.com 的会话是邀请/会话恢复要用的。"""
    from smsinvit.invitation.safety import GuardedCffiSession

    from auth_flow import AuthFlow

    session = GuardedCffiSession(impersonate="chrome136")
    session.cookies.set("__Secure-next-auth.session-token", "keep", domain=".chatgpt.com")
    session.cookies.set("oai-did", "keep2", domain=".chatgpt.com")
    session.cookies.set("__Secure-next-auth.session-token.1", "drop", domain=".openai.com")
    session.cookies.set("oai-client-auth-session", "drop2", domain="auth.openai.com")

    flow = SimpleNamespace(session=session)
    removed = AuthFlow._drop_stale_auth_openai_cookies(flow)
    assert removed == 2
    left = {(c.name, c.domain) for c in session.cookies.jar}
    assert left == {("__Secure-next-auth.session-token", ".chatgpt.com"), ("oai-did", ".chatgpt.com")}
    session.close()

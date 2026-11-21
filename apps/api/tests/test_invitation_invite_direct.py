"""邀请/授权直连优先 + 被 CF 拦时切代理重试。

实测（容器内，真账号）：直连能走到 choose-an-account/consent（罐有效），但同一流程
随后**连续 403**（Cloudflare 拦），而走代理 + 罐 14s 就能拿到 RT。所以 invite_direct
默认关；开着的时候必须"直连失败 → 自动用代理重试一次"，不能直接失败。
"""
import os
import tempfile

# codex_auth 的导入链需要这些环境变量，且必须在任何 fixture 重置仓库之前完成
os.environ.setdefault("SMSINVIT_INVITATION_LOG_DIR", tempfile.mkdtemp(prefix="invitation-logs-"))
os.environ.setdefault("CODEX_HOMES_ROOT", tempfile.mkdtemp(prefix="codex-homes-"))

from types import SimpleNamespace  # noqa: E402

from webui import codex_auth  # noqa: E402
def test_invite_direct_falls_back_to_a_proxy_when_the_direct_attempt_fails(monkeypatch):
    """实测直连会被 CF 间歇 403：开着 invite_direct 时，拿不到 RT 必须自动改代理重试一次。"""
    from types import SimpleNamespace

    from webui import codex_auth

    calls = {"picked": 0, "plants": [], "rts": []}
    monkeypatch.setattr(codex_auth, "_truthy_setting", lambda key, default="": key == "invite_direct")
    monkeypatch.setattr(codex_auth.proxy_pool, "pick",
                        lambda purpose="", **kwargs: calls.__setitem__("picked", calls["picked"] + 1) or "http://proxy:1")
    monkeypatch.setattr(codex_auth, "_restored_session_alive", lambda flow: True)
    monkeypatch.setattr(codex_auth, "_rebuild_auth_session", lambda *a, **k: True)
    monkeypatch.setattr(codex_auth, "_persist_restored_session", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "emit_stage", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_print_stage_log", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_saved_codex_creds", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth.exporter, "refresh_codex_token",
                        lambda rt, proxy="": {"access_token": "a", "refresh_token": "r",
                                             "id_token": "i"})
    monkeypatch.setattr(codex_auth.mail_alloc, "build_provider_for_account",
                        lambda email: (object(), "cf_temp", {"email": email}))

    def fake_plant(cfg, run_id, email, record, password, totp, *, on_stage=None):
        calls["plants"].append(getattr(cfg, "proxy", None))
        return SimpleNamespace(result=SimpleNamespace(refresh_token=""))

    def fake_rt(flow, provider):
        calls["rts"].append(flow)
        # 第一次（直连）失败，第二次（代理）成功
        return "" if len(calls["rts"]) == 1 else "rt-from-proxy"

    monkeypatch.setattr(codex_auth, "_plant_pool_session", fake_plant)
    monkeypatch.setattr(codex_auth, "_try_codex_rt", fake_rt)
    monkeypatch.setattr(codex_auth.db, "save_codex_auth", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth.db, "update_cdk_fields", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth.invite_mail, "visit_invite_link", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(codex_auth, "_save_rt_immediately", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_refresh_cookie_jar", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_complete_cli", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(codex_auth.db, "get_registered", lambda email: {
        "password": "pw", "totp_secret": "T", "session_token": "s", "access_token": "a",
        "device_id": "d", "extra": {}})

    result = codex_auth._authorize_bound_account(
        code="CDK-1", email="a@example.test", proxy="", run_id="r1", job_id="j1",
        invite_link="https://invite", invite_already_processed=True)
    assert result == {"ok": True}
    assert calls["picked"] == 1, "必须取一次代理"
    assert calls["plants"] == [None, "http://proxy:1"], calls["plants"]   # 先直连，后代理
    assert len(calls["rts"]) == 2


def test_a_failed_first_authorization_retries_after_rebuilding_the_session(monkeypatch):
    """配了代理时，首次拿不到 RT 必须重建会话再试一次（同一代理 IP）。

    生产事故（2026-10-11）：OpenAI 把 consent 页里的 `consent_challenge` 去掉了，
    底层 API 路径永久失效、只剩表单 POST 兜底，而表单 POST 一定被 500 弹回。此时
    **只有会话是刚用协议登录重建起来的那次链路**能继续走完（workspace 选择 →
    callback → 换到 RT）；沿用池里那份旧会话直接静默 SSO 会在 consent 500 后来回
    打转直到失败。

    原来的重建重试被 `invite_direct` + 「必须直连」两个条件门住，而线上恰恰配了代理
    在跑 → 这条唯一的生路被整段跳过、直接失败。本用例钉住的是：代理场景下首次失败
    也要重建重试，且**不换代理、不重新植入会话**（接受邀请与授权必须同一个出口 IP）。
    """
    calls = {"picked": 0, "plants": [], "rts": [], "rebuilt": 0}
    monkeypatch.setattr(codex_auth, "_truthy_setting", lambda key, default="": False)
    monkeypatch.setattr(codex_auth.proxy_pool, "pick",
                        lambda purpose="", **kwargs: calls.__setitem__("picked", calls["picked"] + 1) or "http://other:2")
    monkeypatch.setattr(codex_auth, "_restored_session_alive", lambda flow: True)
    monkeypatch.setattr(codex_auth, "_persist_restored_session", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "emit_stage", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_print_stage_log", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_saved_codex_creds", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth.exporter, "refresh_codex_token",
                        lambda rt, proxy="": {"access_token": "a", "refresh_token": "r",
                                             "id_token": "i"})
    monkeypatch.setattr(codex_auth.mail_alloc, "build_provider_for_account",
                        lambda email: (object(), "cf_temp", {"email": email}))

    def fake_plant(cfg, run_id, email, record, password, totp, *, on_stage=None):
        calls["plants"].append(getattr(cfg, "proxy", None))
        return SimpleNamespace(result=SimpleNamespace(refresh_token=""))

    def fake_rebuild(flow, provider, email, password):
        calls["rebuilt"] += 1
        return True

    def fake_rt(flow, provider):
        calls["rts"].append(flow)
        # 第一次失败，重建会话后的第二次成功
        return "" if len(calls["rts"]) == 1 else "rt-after-rebuild"

    monkeypatch.setattr(codex_auth, "_plant_pool_session", fake_plant)
    monkeypatch.setattr(codex_auth, "_rebuild_auth_session", fake_rebuild)
    monkeypatch.setattr(codex_auth, "_try_codex_rt", fake_rt)
    monkeypatch.setattr(codex_auth.db, "save_codex_auth", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth.db, "update_cdk_fields", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth.invite_mail, "visit_invite_link", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(codex_auth, "_save_rt_immediately", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_refresh_cookie_jar", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_complete_cli", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(codex_auth.db, "get_registered", lambda email: {
        "password": "pw", "totp_secret": "T", "session_token": "s", "access_token": "a",
        "device_id": "d", "extra": {}})

    proxy = "socks5h://u:p@proxy.example:1080"
    result = codex_auth._authorize_bound_account(
        code="CDK-2", email="b@example.test", proxy=proxy, run_id="r2", job_id="j2",
        invite_link="https://invite", invite_already_processed=True)

    assert result == {"ok": True}
    assert calls["rebuilt"] == 1, "代理场景首次失败必须重建一次会话"
    assert len(calls["rts"]) == 2, "必须是两次授权尝试"
    assert calls["rts"][0] is calls["rts"][1], "重试沿用同一条会话（同一代理 IP）"
    assert calls["plants"] == [proxy], "重试不重新植入会话"
    assert calls["picked"] == 0, "代理场景不需要另取代理"

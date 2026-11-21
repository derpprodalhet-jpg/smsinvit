"""Historical phone evidence is bounded, account scoped and display only."""
from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest

from smsinvit.invitation import phone_details
from smsinvit.invitation import mysql
from test_invitation_account_pool_sql import pool, seed  # noqa: F401
from webui import db


EMAIL = "legacy@example.test"
PHONE = "+15550001234"
CODE = "001234"


def line(message, logger="registrar", level="INFO"):
    return f"01:00:00 [{level}] {logger}: {message}\n"


def rental(phone=PHONE):
    return line(f"[sms] 已租到新号码，平台=smsbower 国家=73 测试 号码={phone}，金额=0.04 USD")


def sms(phone=PHONE, code=CODE):
    return line(f"[sms] 已收到验证码：{code}（号码={phone}，金额=0.04 USD）")


def binding(email=EMAIL):
    return line(f"[register] 注册阶段手机绑定成功: {email}（兑换时不再需要 add-phone）")


def success(email=EMAIL):
    return rental() + sms() + line("[sms] 业务验证成功，平台结算已确认") + binding(email)


def run(path, email=EMAIL, kind="register", **fields):
    return {"email": email, "kind": kind, "log_path": str(path), **fields}


def attach(tmp_path, text=None, rows=None, item=None):
    path = tmp_path / "run.log"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    rows = rows if rows is not None else [run(path)]
    calls = []

    def list_runs_for_emails(emails, limit=5):
        calls.append((emails, limit))
        return rows

    repo = SimpleNamespace(list_runs_for_emails=list_runs_for_emails)
    result = item if item is not None else {"email": EMAIL, "phone_bound_ok": 1}
    original = deepcopy(result)
    out = phone_details.attach_phone_details(result, repo, tmp_path)
    assert result == original
    return out, calls


def test_preserves_shape_and_fills_both_admin_list_and_detail(tmp_path):
    item = {"items": [{"email": EMAIL, "phone_bound_ok": 1}], "data": {
        "email": EMAIL.upper(), "phone_bound_ok": 1, "proxy": "http://proxy.example:80"}, "total": 1}
    out, calls = attach(tmp_path, success(), item=item)
    assert out["items"][0]["phone_number"] == out["data"]["phone_number"] == PHONE
    assert out["items"][0]["phone_code"] == out["data"]["phone_code"] == CODE
    assert out["data"]["proxy"] == item["data"]["proxy"] and out["total"] == 1
    assert calls == [([EMAIL], 5)]


@pytest.mark.parametrize("item", [
    {"email": EMAIL, "phone_bound_ok": 0},
    {"email": EMAIL, "phone_bound_ok": -1},
    {"email": EMAIL, "phone_bound_ok": 1, "phone_number": PHONE, "phone_code": CODE},
])
def test_status_and_durable_evidence_do_not_read_historical_runs(tmp_path, item):
    out, calls = attach(tmp_path, success(), item=item)
    assert out == item and calls == []


@pytest.mark.parametrize("text", [
    rental() + line("[email] 已收到验证码：001234") + line("[sms] 业务验证成功，平台结算已确认") + binding(),
    rental() + sms(phone="+15550005678") + line("[sms] 业务验证成功，平台结算已确认") + binding(),
    sms() + line("[sms] 业务验证成功，平台结算已确认") + binding(),
    rental() + sms() + binding(),
    rental() + sms() + line("[sms] 业务验证成功，平台结算已确认"),
    success(email="other@example.test"),
    rental() + sms() + line("[sms] 本次业务失败，退款未成立") + line("[sms] 业务验证成功，平台结算已确认") + binding(),
    success() + line("[register] 注册阶段手机绑定失败（不影响入池，兑换时会到 add-phone）: 测试失败", level="WARNING"),
    rental() + sms() + line("add-phone 号码 +15550001234 失败: 测试失败", logger="auth_flow", level="WARNING") + line("[sms] 业务验证成功，平台结算已确认") + binding(),
    rental() + sms() + rental(phone="+15550005678") + line("[sms] 业务验证成功，平台结算已确认") + binding(),
])
def test_requires_matching_successful_phone_proof(tmp_path, text):
    out, _ = attach(tmp_path, text)
    assert not out.get("phone_number") and not out.get("phone_code")


def test_protocol_login_and_pending_supplier_settlement_are_business_success(tmp_path):
    text = rental() + sms() + line("[sms] 业务验证成功，平台结算待确认") + line(
        f"[register] 注册阶段手机绑定成功（协议登录时已完成）: {EMAIL}")
    out, _ = attach(tmp_path, text)
    assert (out["phone_number"], out["phone_code"]) == (PHONE, CODE)


@pytest.mark.parametrize("phase", ["兑换阶段", "手动授权"])
def test_invite_binding_must_name_the_same_account(tmp_path, phase):
    text = rental() + sms() + line("[sms] 业务验证成功，平台结算已确认") + line(
        f"[invite] {EMAIL} {phase}已绑手机 → 回写接码状态", logger="codex_auth")
    out, _ = attach(tmp_path, text, rows=[run(tmp_path / "run.log", kind="codex_auth")])
    assert (out["phone_number"], out["phone_code"]) == (PHONE, CODE)


def test_account_factory_registration_recovers_legacy_phone_details(tmp_path):
    out, _ = attach(tmp_path, success(), rows=[run(tmp_path / "run.log", kind="register_pool")])
    assert (out["phone_number"], out["phone_code"]) == (PHONE, CODE)


def test_history_repository_failure_preserves_response_without_private_logs(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError(f"history lookup is unavailable for {EMAIL}: {PHONE} {CODE}")

    result = {"items": [{"email": EMAIL, "phone_bound_ok": 1}], "total": 1}
    repo = SimpleNamespace(list_runs_for_emails=unavailable)
    messages = []
    monkeypatch.setattr(phone_details, "_last_history_warning", 0)
    monkeypatch.setattr(phone_details._logger, "warning", messages.append)
    for _ in range(2):
        assert phone_details.attach_phone_details(result, repo, tmp_path) is result
    assert messages == ["Historical phone details lookup is unavailable"]


def test_legacy_pool_evidence_attaches_to_real_repository_lists_without_writes(pool, tmp_path):
    with sqlite3.connect(pool.db.engine.url.database) as connection:
        connection.execute("""CREATE TABLE invitation_v2_runs (
            run_id TEXT PRIMARY KEY, email TEXT, kind TEXT, started_at REAL, log_path TEXT
        )""")
        connection.execute("""CREATE TABLE invitation_v2_codex_auth (
            email TEXT PRIMARY KEY, refresh_token TEXT, access_token TEXT,
            account_id TEXT, cli_completed_at REAL, invite_processed_at REAL
        )""")
    seed(pool, EMAIL)
    db.mark_phone_bound(EMAIL, True)
    path = tmp_path / "register-pool.log"
    path.write_text(success(), encoding="utf-8")
    pool.db.execute("INSERT INTO invitation_v2_runs(run_id,email,kind,started_at,log_path) VALUES (?,?,?,?,?)",
                    mysql.protected("synthetic-run"), EMAIL, "register_pool", 1.0, str(path))
    stored = pool.db.scalar("SELECT extra_json FROM invitation_v2_registered WHERE email=?", EMAIL)
    first_read = len(pool.db.seen)
    mail = phone_details.attach_phone_details({"items": db.list_accounts()}, db, tmp_path)["items"][0]
    registered = phone_details.attach_phone_details({"items": db.list_registered()}, db, tmp_path)["items"][0]
    detail = phone_details.attach_phone_details({"data": db.get_registered(EMAIL)}, db, tmp_path)["data"]
    for item in (mail, registered, detail):
        assert item["phone_bound_ok"] == 1
        assert (item["phone_number"], item["phone_code"]) == (PHONE, CODE)
    assert all(not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
               for statement in pool.db.seen[first_read:])
    assert pool.db.scalar("SELECT extra_json FROM invitation_v2_registered WHERE email=?", EMAIL) == stored
    assert db.get_registered(EMAIL)["phone_number"] == db.get_registered(EMAIL)["phone_code"] == ""


def test_existing_partial_details_are_never_paired_with_a_different_rental(tmp_path):
    out, _ = attach(tmp_path, success(), item={"email": EMAIL, "phone_bound_ok": 1,
                                             "phone_number": "+15550009999", "phone_code": ""})
    assert out["phone_number"] == "+15550009999" and out["phone_code"] == ""
    out, _ = attach(tmp_path, success(), item={"email": EMAIL, "phone_bound_ok": 1, "phone_number": PHONE})
    assert out["phone_code"] == CODE


def test_newer_non_phone_run_can_use_older_proof_but_failed_rental_cannot(tmp_path):
    old = tmp_path / "old.log"
    old.write_text(success(), encoding="utf-8")
    latest = tmp_path / "latest.log"
    latest.write_text(line("[invite] 授权完成", logger="codex_auth"), encoding="utf-8")
    rows = [run(latest, kind="codex_auth"), run(old)]
    out, _ = attach(tmp_path, rows=rows)
    assert out["phone_number"] == PHONE
    latest.write_text(rental() + sms() + line("[sms] 号码取消已确认"), encoding="utf-8")
    out, _ = attach(tmp_path, rows=rows)
    assert not out.get("phone_number")
    latest.write_text(binding(), encoding="utf-8")
    out, _ = attach(tmp_path, rows=rows)
    assert not out.get("phone_number")


@pytest.mark.parametrize("path_kind", ["outside", "file_symlink", "directory_symlink", "traversal", "missing", "directory", "fifo"])
def test_unsafe_or_non_regular_log_paths_are_ignored(tmp_path, path_kind):
    root = tmp_path / "logs"
    root.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text(success(), encoding="utf-8")
    path = outside
    if path_kind == "file_symlink":
        path = root / "link.log"
        path.symlink_to(outside)
    elif path_kind == "directory_symlink":
        link = root / "linked"
        link.symlink_to(tmp_path, target_is_directory=True)
        path = link / "outside.log"
    elif path_kind == "traversal":
        path = root / ".." / "outside.log"
    elif path_kind == "missing":
        path = root / "missing.log"
    elif path_kind == "directory":
        path = root / "directory.log"
        path.mkdir()
    elif path_kind == "fifo":
        path = root / "fifo.log"
        phone_details.os.mkfifo(path)
    out, _ = attach(root, rows=[run(path)])
    assert not out.get("phone_number") and not out.get("phone_code")


def test_tail_read_is_bounded_and_cached_until_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "run.log"
    path.write_text("x" * (phone_details.TAIL_BYTES * 2) + "\n" + success(), encoding="utf-8")
    read = phone_details.os.read
    reads = []

    def counted_read(descriptor, count):
        reads.append(count)
        return read(descriptor, count)

    monkeypatch.setattr(phone_details.os, "read", counted_read)
    for _ in range(2):
        out, _ = attach(tmp_path, rows=[run(path)])
        assert out["phone_number"] == PHONE
    assert reads == [phone_details.TAIL_BYTES]
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line("[sms] 号码取消已确认"))
    out, _ = attach(tmp_path, rows=[run(path)])
    assert not out.get("phone_number") and reads == [phone_details.TAIL_BYTES] * 2


def test_success_outside_bounded_tail_is_not_recovered(tmp_path):
    text = success() + line("其他日志" * phone_details.TAIL_BYTES)
    out, _ = attach(tmp_path, text)
    assert not out.get("phone_number") and not out.get("phone_code")

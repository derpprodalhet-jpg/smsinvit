"""Both invitation admin lists must expose the same durable registration evidence."""
import sqlite3
from types import SimpleNamespace

import pytest

from test_invitation_account_pool_sql import pool, seed  # noqa: F401
from smsinvit.invitation import mysql
from webui import db


FIELDS = ("proxy", "used_proxy", "phone_bound_ok", "phone_bound_at", "phone_number", "phone_code")


@pytest.fixture
def details_pool(pool):
    # list_registered also includes the independent Codex token summary.
    with sqlite3.connect(pool.db.engine.url.database) as connection:
        connection.execute("""CREATE TABLE invitation_v2_codex_auth (
            email TEXT PRIMARY KEY, refresh_token TEXT, access_token TEXT,
            account_id TEXT, cli_completed_at REAL, invite_processed_at REAL
        )""")
    return pool


def details(row):
    return {field: row.get(field) for field in FIELDS}


def test_mail_and_registered_lists_use_current_pool_evidence(details_pool):
    email = "joined@example.test"
    seed(details_pool, email)
    db.save_registered({"email": email, "password": "pw", "proxy": "http://old.example:80",
                        "used_proxy": 0, "phone_bound_ok": 0, "source": "cf_temp",
                        "plus_check": {"status": "free"}})
    db.mark_registered(email, proxy="http://actual.example:80", used_proxy=True)
    db.mark_phone_bound(email, True, phone_number="+15550001234", phone_code="001234")

    mail = db.list_accounts(status="registered", kind="outlook")[0]
    registered = db.list_registered()[0]
    detail = db.get_registered(email)
    assert details(mail) == details(registered) == details(detail) == details(db.get_account(email))
    assert details(detail) == {
        "proxy": "http://actual.example:80", "used_proxy": 1, "phone_bound_ok": 1,
        "phone_bound_at": mail["phone_bound_at"], "phone_number": "+15550001234", "phone_code": "001234",
    }
    assert registered["plus_check"] == {"status": "free"}
    assert details(db.list_registered_full()[0]) == details(detail)
    assert details(db.list_registered_by_emails([email])[0]) == details(detail)
    assert all(not key.startswith("_account_") for key in detail)
    # Metadata is stored by the real repository encryption path, never in a
    # plaintext account column. A new process can decrypt it after cache loss.
    stored = details_pool.db.scalar("SELECT extra_json FROM invitation_v2_registered WHERE email=?", email)
    assert stored.startswith("iv2$") and "+15550001234" not in stored and "001234" not in stored
    mysql._cache.clear()
    assert db.get_registered(email)["phone_code"] == "001234"


def test_session_only_writes_preserve_registered_proxy_source_and_evidence(details_pool):
    email = "partial@example.test"
    seed(details_pool, email)
    db.save_registered({"email": email, "password": "password", "proxy": "http://actual.example:80",
                        "source": "outlook", "access_token": "access-token", "refresh_token": "refresh-token",
                        "phone_number": "+15550005678", "phone_code": "987654"})
    db.save_registered({"email": email, "cookie_jar": [{"name": "test-cookie", "value": "test-value"}]})
    item = db.get_registered(email)
    assert item["proxy"] == "http://actual.example:80" and item["source"] == "outlook"
    assert item["access_token"] == "access-token" and item["refresh_token"] == "refresh-token"
    assert item["phone_number"] == "+15550005678" and item["phone_code"] == "987654"


def test_later_phone_status_updates_keep_successful_evidence_and_credentials(details_pool):
    email = "evidence@example.test"
    seed(details_pool, email)
    db.save_registered({"email": email, "password": "password", "access_token": "access-token",
                        "refresh_token": "refresh-token", "custom_metadata": "preserved"})
    db.mark_phone_bound(email, True, phone_number="+15550001234", phone_code="123456")
    db.mark_phone_bound(email, True)
    db.mark_phone_bound(email, False, "later failure", phone_number="+15550009999", phone_code="999999")
    item = db.get_registered(email)
    assert item["phone_number"] == "+15550001234" and item["phone_code"] == "123456"
    assert item["extra"]["custom_metadata"] == "preserved"
    assert item["access_token"] == "access-token" and item["password"] == "password"
    assert item["phone_bound_ok"] == 0


def test_standalone_legacy_registration_keeps_metadata_without_a_pool_row(details_pool):
    email = "standalone@example.test"
    db.save_registered({"email": email, "proxy": "http://legacy.example:80", "used_proxy": 1,
                        "phone_bound_ok": 1, "phone_bound_at": 123.0,
                        "phone_number": "+15550001234", "phone_code": "123456"})
    item = db.get_registered(email)
    assert details(item) == details(db.list_registered()[0])
    assert item["phone_bound_ok"] == 1 and item["used_proxy"] == 1 and item["phone_bound_at"] == 123.0
    assert item["proxy"] == "http://legacy.example:80"


@pytest.mark.parametrize("value", [None, "bad-json", "[]"])
def test_missing_or_malformed_evidence_stays_empty(details_pool, value):
    email = "old@example.test"
    seed(details_pool, email)
    stored = mysql.protected(value) if value else None
    details_pool.db.execute("UPDATE invitation_v2_registered SET extra_json=? WHERE email=?", stored, email)
    registered = db.get_registered(email)
    mail = db.list_accounts()[0]
    assert registered["phone_number"] == registered["phone_code"] == ""
    assert mail["phone_number"] == mail["phone_code"] == ""
    assert registered["phone_bound_ok"] == registered["used_proxy"] == -1


def test_known_direct_connection_is_not_changed_by_an_old_proxy_snapshot(details_pool):
    email = "direct@example.test"
    seed(details_pool, email)
    db.save_registered({"email": email, "used_proxy": 1, "phone_bound_ok": 1,
                        "proxy": "http://old.example:80"})
    db.mark_registered(email, used_proxy=False)
    db.mark_phone_bound(email, False)
    item = db.get_registered(email)
    assert item["used_proxy"] == item["phone_bound_ok"] == 0
    assert item["proxy"] == db.get_account(email)["proxy"] == db.list_accounts()[0]["proxy"] == ""


def test_phone_state_is_persisted_for_single_registration_without_pool_row(details_pool):
    email = "single@example.test"
    db.save_registered({"email": email, "custom_metadata": "preserved"})
    db.mark_phone_bound(email, True, phone_number="+15550001234", phone_code="001234")
    item = db.get_registered(email)
    assert item["phone_bound_ok"] == 1 and item["phone_bound_at"] is not None
    assert item["phone_number"] == "+15550001234" and item["phone_code"] == "001234"
    db.mark_phone_bound(email, False, "later failure")
    item = db.get_registered(email)
    assert item["phone_bound_ok"] == 0 and item["phone_code"] == "001234"
    assert item["extra"]["custom_metadata"] == "preserved"


@pytest.mark.parametrize("proxy,used_proxy", [("http://actual.example:80", 1), ("", 0)])
def test_single_registrar_persists_actual_proxy_and_phone_with_real_repository(
        details_pool, monkeypatch, tmp_path, proxy, used_proxy):
    monkeypatch.setenv("SMSINVIT_INVITATION_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CODEX_HOMES_ROOT", str(tmp_path / "homes"))
    from webui import codex_auth, registrar

    email = "single-registrar@example.test"
    finished = []
    result = SimpleNamespace(to_dict=lambda: {
        "email": email, "password": "fixture-password", "access_token": "fixture-access",
        "session_token": "fixture-session", "refresh_token": "fixture-refresh",
    })

    class FakeFlow:
        def __init__(self, cfg, **_):
            assert (cfg.proxy or "") == proxy
            self.result = result
            self._phone_bound_ok = True
            self._phone_number = "+15550001234"
            self._phone_code = "001234"

        def run_register(self, _mail):
            return self.result

    monkeypatch.setattr(registrar, "AuthFlow", FakeFlow)
    monkeypatch.setattr(registrar, "Config", lambda: SimpleNamespace())
    monkeypatch.setattr(registrar, "get_provider_class", lambda _: SimpleNamespace(pooled=False))
    monkeypatch.setattr(registrar, "create_mail_provider", lambda *_: SimpleNamespace(
        display_name="fixture", pin_mailbox=lambda _: None))
    monkeypatch.setattr(registrar, "_build_sms_callback", lambda _: None)
    monkeypatch.setattr(registrar, "_install_run_stage", lambda _: None)
    monkeypatch.setattr(registrar, "_run_stage", lambda _: None)
    monkeypatch.setattr(registrar, "_emit_status", lambda *_: None)
    monkeypatch.setattr(registrar, "_try_export_to_panels", lambda *_: None)
    monkeypatch.setattr(registrar, "end_run", lambda *_: None)
    monkeypatch.setattr(codex_auth, "capture_cookie_jar", lambda _: [])
    monkeypatch.setattr(db, "get_mail_settings", lambda: {})
    monkeypatch.setattr(db, "finish_run", lambda run_id, status, *_, **__: finished.append(status))

    registrar._do_register("fixture-run", {"email": email}, {
        "mail_source": "cf_temp", "proxy": proxy, "want_access_token": False,
    }, tmp_path / "fixture.log")

    assert finished == ["done"]
    assert db.get_account(email) is None
    item = db.get_registered(email)
    assert item["used_proxy"] == used_proxy and item["proxy"] == proxy
    assert item["phone_bound_ok"] == 1
    assert item["phone_number"] == "+15550001234" and item["phone_code"] == "001234"
    assert item["access_token"] == "" and item["refresh_token"] == "fixture-refresh"
    assert details(item) == details(db.list_registered()[0])


def test_native_apis_return_consistent_detail_fields(details_pool, monkeypatch, tmp_path):
    monkeypatch.setenv("SMSINVIT_INVITATION_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CODEX_HOMES_ROOT", str(tmp_path / "homes"))
    from webui import app

    email = "api@example.test"
    seed(details_pool, email)
    db.mark_registered(email, proxy="http://actual.example:80", used_proxy=True)
    db.mark_phone_bound(email, True, phone_number="+15550001234", phone_code="123456")
    mail = app.api_accounts()["items"][0]
    registered = app.api_registered()["items"][0]
    detail = app.api_registered_one(email)["data"]
    assert details(mail) == details(registered) == details(detail)


def test_recent_history_query_limits_each_exact_email_and_decrypts_run_ids(details_pool):
    with sqlite3.connect(details_pool.db.engine.url.database) as connection:
        connection.execute("""CREATE TABLE invitation_v2_runs (
            run_id TEXT PRIMARY KEY, email TEXT, kind TEXT, started_at REAL, log_path TEXT
        )""")
    for email in ("first@example.test", "second@example.test", "unrelated@example.test"):
        for index in range(4):
            run_id = mysql.protected(f"{email}-{index}")
            details_pool.db.execute("INSERT INTO invitation_v2_runs(run_id,email,kind,started_at,log_path) VALUES (?,?,?,?,?)",
                                   run_id, email, "register", index + 1.0, f"/tmp/{index}.log")
    rows = db.list_runs_for_emails([" FIRST@example.test ", "second@example.test", "first@example.test"], limit=2)
    assert [row["started_at"] for row in rows] == [4.0, 4.0, 3.0, 3.0]
    assert {row["email"] for row in rows} == {"first@example.test", "second@example.test"}
    assert all(not row["run_id"].startswith("iv2$") and "_run_rank" not in row for row in rows)
    assert db.list_runs_for_emails([]) == []

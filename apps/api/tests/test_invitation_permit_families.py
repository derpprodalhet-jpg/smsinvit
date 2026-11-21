"""两族工位与 TaskGate 分族额度：邀请与补号各占各的，互不挤占。

用内存 SQLite 复现 Permits 的槽位语义（真正的 MySQL 版本仍在
test_invitation_permits.py 里由 SMSINVIT_MYSQL_TEST=1 跑）。
"""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smsinvit.invitation import mysql
from smsinvit.invitation.permits import INVITE_SLOTS, POOL_SLOTS, Permits
from smsinvit.invitation.runtime import TaskGate

SLOTS_COUNT = 33


class SqliteDb:
    """Minimal Database stand-in: one transaction owner, positional args."""

    def __init__(self):
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.depth = 0
        self.con.executescript(
            "CREATE TABLE invitation_v2_execution_permits("
            "slot_id INTEGER PRIMARY KEY,owner_token TEXT,instance_id TEXT,email TEXT,task_id TEXT,acquired_at TEXT);"
            "CREATE TABLE invitation_v2_task_holds(job_id TEXT,original_status TEXT,reason TEXT,created_at TEXT,resolved_at TEXT);"
            "CREATE TABLE invitation_v2_cdk_jobs(job_id TEXT PRIMARY KEY,code TEXT,email TEXT,status TEXT);"
        )
        for slot in range(SLOTS_COUNT):
            self.con.execute("INSERT INTO invitation_v2_execution_permits(slot_id) VALUES (?)", (slot,))

    @contextmanager
    def transaction(self, requires_new=False):
        outer = not self.depth
        if outer:
            self.con.execute("BEGIN")
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

    def _run(self, sql, args):
        sql = sql.replace("UTC_TIMESTAMP(3)", "CURRENT_TIMESTAMP")
        sql = sql.replace(" FOR UPDATE", "").replace(" SKIP LOCKED", "")
        return self.con.execute(sql, args)

    def all(self, sql, *args):
        return [dict(row) for row in self._run(sql, args).fetchall()]

    def one(self, sql, *args):
        rows = self.all(sql, *args)
        return rows[0] if rows else None

    def scalar(self, sql, *args):
        row = self._run(sql, args).fetchone()
        return row[0] if row else None

    def execute(self, sql, *args):
        return SimpleNamespace(rowcount=self._run(sql, args).rowcount)


def permits():
    return Permits(SqliteDb(), SimpleNamespace(fingerprint=lambda value: "f" * 64))


def test_families_have_independent_quotas_and_slot_ranges():
    service = permits()
    invite = [service.claim(f"invite{index}@example.test", "job", 2, INVITE_SLOTS) for index in range(4)]
    pool = [service.claim(f"pool{index}@example.test", "job", 3, POOL_SLOTS) for index in range(5)]
    assert sorted(slot for slot, _ in filter(None, invite)) == [1, 2]
    assert sorted(slot for slot, _ in filter(None, pool)) == [17, 18, 19]
    # 邀请额度用满不影响补号，反之亦然 —— 这正是"各配置各的"
    rows = service.db.all("SELECT slot_id FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL")
    assert {row["slot_id"] for row in rows} == {1, 2, 17, 18, 19}
    assert all(1 <= slot <= 16 for slot, _ in filter(None, invite))
    assert all(17 <= slot <= 32 for slot, _ in filter(None, pool))


def test_one_mailbox_is_still_globally_exclusive_across_families():
    service = permits()
    assert service.claim("shared@example.test", "invite", 3, INVITE_SLOTS)
    assert service.claim("shared@example.test", "pool", 10, POOL_SLOTS) is None
    assert service.claim("other@example.test", "pool", 10, POOL_SLOTS)


def test_family_limit_cannot_exceed_its_slot_range():
    service = permits()
    with pytest.raises(ValueError):
        service.claim("a@example.test", "job", 17, INVITE_SLOTS)
    with pytest.raises(ValueError):
        service.claim("a@example.test", "job", 17, POOL_SLOTS)


def gate(settings):
    repo = SimpleNamespace(
        get_setting=lambda key, default="": settings.get(key, default),
        get_cdk_code=lambda code: {"email": f"{str(code).strip().lower()}@example.test"},
    )
    seen = []

    class Recording:
        def claim(self, email, task_id, limit, slots):
            seen.append({"email": email, "limit": limit, "slots": slots})
            return (1, "token")

        def release(self, permit):
            return True

    return TaskGate(repo, Recording()), seen


@pytest.fixture(autouse=True)
def task_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("SMSINVIT_INVITATION_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(mysql, "services", lambda: SimpleNamespace(db=SimpleNamespace(all=lambda *a: [])))


def test_gate_reads_a_different_limit_and_slot_family_per_task_kind(task_settings):
    settings = {"cdk_max_concurrency": "3", "pool_register_concurrency": "10"}
    task_gate, seen = gate(settings)

    def run_invite_job(code, run_id=""):
        return "invite"

    def _do_register(run_id, account, options, log_file, handler):
        return "pool" if options.get("pool_only") else "register"

    assert task_gate.decorate(run_invite_job)("CDK", run_id="r") == "invite"
    assert task_gate.decorate(_do_register)("r", {"email": "pool@example.test"},
                                            {"pool_only": True}, "log", None) == "pool"
    assert task_gate.decorate(_do_register)("r", {"email": "manual@example.test"},
                                            {}, "log", None) == "register"
    assert seen[0] == {"email": "cdk@example.test", "limit": 3, "slots": INVITE_SLOTS}
    assert seen[1]["limit"] == 10 and seen[1]["slots"] == POOL_SLOTS
    # 管理端"单次注册"（非 pool_only）仍走邀请族的额度
    assert seen[2]["limit"] == 3 and seen[2]["slots"] == INVITE_SLOTS


def test_gate_clamps_pool_concurrency_and_never_borrows_invite_quota(task_settings):
    task_gate, seen = gate({"cdk_max_concurrency": "32", "pool_register_concurrency": "99"})

    def _do_register(run_id, account, options, log_file, handler):
        return True

    task_gate.decorate(_do_register)("r", {"email": "pool@example.test"}, {"pool_only": True}, "log", None)
    assert seen[0]["limit"] == 10  # 上限 10，不会跟着 cdk_max_concurrency 放大


def test_gate_tracks_families_separately(task_settings):
    task_gate, _ = gate({"cdk_max_concurrency": "3", "pool_register_concurrency": "10"})

    def run_invite_job(code, run_id=""):
        return "invite"

    def _do_register(run_id, account, options, log_file, handler):
        return True

    task_gate.decorate(run_invite_job)("CDK", run_id="r")
    task_gate.decorate(_do_register)("r", {"email": "pool@example.test"}, {"pool_only": True}, "log", None)
    assert task_gate.family_active == {"invite": 0, "pool": 0}
    assert task_gate.active == 0 and task_gate.inflight == 0
    assert task_gate.emails == set()


def test_gate_clamps_the_invite_limit_to_the_available_slots(task_settings):
    """配置能填多大，只能到工位区间那么宽。

    旧实现允许 1–32，但邀请族只有 16 个工位：填 17–32 会让每个任务都以
    "Invalid invitation concurrency limit" 失败，而配置本身却存得进去。
    """
    task_gate, seen = gate({"cdk_max_concurrency": "32", "pool_register_concurrency": "10"})

    def run_invite_job(code, run_id=""):
        return "invite"

    task_gate.decorate(run_invite_job)("CDK", run_id="r")
    assert seen[0]["limit"] == INVITE_SLOTS[1] == 16


def test_gate_never_amplifies_an_invalid_invite_limit(task_settings):
    task_gate, seen = gate({"cdk_max_concurrency": "not-a-number", "pool_register_concurrency": "10"})

    def run_invite_job(code, run_id=""):
        return "invite"

    task_gate.decorate(run_invite_job)("CDK", run_id="r")
    assert seen[0]["limit"] == 1   # 非法值回落到下限，绝不放大

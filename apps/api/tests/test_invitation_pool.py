"""账号工厂（补号）行为边界：全部用桩，不碰网络、不碰真实数据库。

覆盖三件事：
  1. 什么时候补号（开关 / 池化来源 / 差额 / 代理 / 库存）
  2. 补号用的参数与邀请侧**完全解耦**（pool_otp_timeout、恒绑 2FA、不换 RT）
  3. 失败与失控保护（无代理退还、连续失败转人工、handler 不泄漏）
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smsinvit.invitation import pool as pool_module

KIND = "outlook"
PROXY = "http://pool-proxy.example.test:8080"


class Repo:
    def __init__(self, settings=None, mailboxes=(), failures=0):
        self.settings = dict(settings or {})
        self.mailboxes = list(mailboxes)
        self.failures = failures
        self.claimed = []
        self.failed = []
        self.released = []
        self.count_calls = []
        self.runs = {}
        self.finished = []
        self.accounts = {}

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def count_accounts(self, status="", kind=""):
        self.count_calls.append((status, kind))
        return len(self.mailboxes) if status == "available" else 0

    def claim_next(self, kind=""):
        if not self.mailboxes:
            return None
        email = self.mailboxes.pop(0)
        self.claimed.append(email)
        return {"email": email, "kind": kind}

    def mark_failed(self, email, reason=""):
        self.failed.append((email, reason))

    def release_unused(self, email):
        self.released.append(email)

    def get_mail_settings(self):
        return {"mail_source": "cf_temp"}

    def get_account(self, email):
        return self.accounts.get(email)

    def ensure_pool_account(self, email, kind, proxy="", status="registered"):
        self.accounts[email] = {"email": email, "kind": kind, "status": status}
        return True

    def mark_failed(self, email, reason):
        self.failed.append((email, reason))
        if email in self.accounts:
            self.accounts[email] = {**self.accounts[email], "status": "failed",
                                    "fail_reason": reason}

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def finish_run(self, run_id, status, error="", category=""):
        self.finished.append((run_id, status, error, category))
        if run_id in self.runs:
            self.runs[run_id] = {**self.runs[run_id], "status": status, "error": error,
                                 "error_category": category}


class Service:
    def __init__(self, repo, alloc, proxy, db=None):
        self.repo = repo
        self.alloc = alloc
        self.proxy = proxy
        self.db = db
        self._started = True
        self._stopping = False


def allocator(available=0, pooled=True, kind=KIND):
    holder = {"available": available, "pooled": pooled, "kind": kind}
    return SimpleNamespace(
        current_kind=lambda settings=None: holder["kind"],
        kind_is_pooled=lambda value: holder["pooled"],
        pool_available=lambda settings=None: holder["available"],
        holder=holder,
    )


def proxy_pool(available=True, url=PROXY):
    calls = []
    return SimpleNamespace(
        has_available=lambda exclude=None, purpose="codex": available,
        # **kwargs：生产签名新增了 exclude_bound_to / allow_shared（代理独占规则）
        pick=lambda exclude=None, purpose="codex", **kwargs: ({"url": url} if url else None),
        # 409 生命周期接口：记账内容由测试断言（含用途维度）
        is_409_error=lambda text: "409" in str(text) or "invalid_state" in str(text),
        is_transport_error=lambda text: "connect tunnel" in str(text).lower() or "curl: (7)" in str(text),
        record_transport_failure=lambda value: (calls.append(("transport", value, "health"))
                                                or {"action": "count"}),
        record_success=lambda value, purpose="codex": calls.append(("success", value, purpose)),
        record_fail=lambda value, error="", purpose="codex": calls.append(("fail", value, purpose)),
        record_409=lambda value, purpose="codex": (calls.append(("409", value, purpose)) or {"action": "count"}),
        calls=calls,
    )


@pytest.fixture
def factory(monkeypatch, tmp_path):
    monkeypatch.setenv("SMSINVIT_INVITATION_LOG_DIR", str(tmp_path / "logs"))
    from smsinvit.invitation.pool import AccountPoolMaintainer
    from webui import registrar

    def build(*, settings=None, available=0, pooled=True, mailboxes=(), failures=0,
              proxy_available=True, records=None):
        repo = Repo(settings=settings, mailboxes=mailboxes, failures=failures)
        alloc = allocator(available=available, pooled=pooled)
        service = Service(repo, alloc, proxy_pool(available=proxy_available), db=SimpleNamespace(one=None))
        service.db = SimpleNamespace(one=lambda *args: {"n": repo.failures})
        maintainer = AccountPoolMaintainer(service)
        calls = records if records is not None else {}
        calls.setdefault("begin", [])
        calls.setdefault("runs", [])
        calls.setdefault("end", [])
        calls.setdefault("removed", [])
        calls.setdefault("kinds", [])

        def begin_run(email, kind="register"):
            calls["kinds"].append(kind)
            return f"run-{email}", "log", None, "handler"

        monkeypatch.setattr(registrar, "begin_run", begin_run)
        monkeypatch.setattr(registrar, "end_run", lambda run_id, handler=None: calls["end"].append(run_id))
        monkeypatch.setattr(registrar, "remove_run_queue", lambda run_id: calls["removed"].append(run_id))
        monkeypatch.setattr(registrar, "_do_register",
                            lambda run_id, account, options, log_file, handler: calls["runs"].append(
                                {"run_id": run_id, "account": account, "options": options}))
        return SimpleNamespace(maintainer=maintainer, repo=repo, alloc=alloc, service=service, calls=calls)

    return build


ENABLED = {"pool_auto_enabled": "true", "pool_min_available": "5",
           "pool_register_concurrency": "2", "pool_check_seconds": "60", "pool_otp_timeout": "181"}


def test_cycle_skips_when_own_switch_is_off(factory):
    built = factory(settings={**ENABLED, "pool_auto_enabled": "false"},
                    available=0, mailboxes=["a@example.test"])
    built.maintainer._cycle()
    assert built.repo.claimed == [] and built.calls["runs"] == []


def fake_mail_provider(monkeypatch, prefix="cf"):
    """把「现造地址」换成可预测的桩，并记录调用次数（用于证明不会二次造地址）。"""
    import mail_providers
    created = []

    class Provider:
        display_name = "CF 临时邮箱"

        def create_mailbox(self):
            address = f"{prefix}{len(created) + 1}@cf.test"
            created.append(address)
            return address

        def pin_mailbox(self, address):
            created.append("pin:" + address)

    monkeypatch.setattr(mail_providers, "create_mail_provider",
                        lambda kind, settings=None, account=None: Provider())
    return created


def test_cycle_creates_real_addresses_before_starting_the_run(factory, monkeypatch):
    """非池化来源：地址必须**先造好**，运行记录里从第一秒就是真实地址。

    以前先用 @placeholder.local 占位，失败时根本看不出是哪个地址。
    """
    created = fake_mail_provider(monkeypatch)
    built = factory(settings=ENABLED, available=0, pooled=False, mailboxes=["imported@example.test"])
    built.maintainer._cycle()
    assert built.repo.claimed == []                      # 不消耗导入库存
    assert len(built.calls["runs"]) == built.maintainer.concurrency() == 2, built.maintainer.snapshot()
    emails = [run["account"]["email"] for run in built.calls["runs"]]
    assert emails == ["cf1@cf.test", "cf2@cf.test"]      # 真实地址，且每次唯一
    assert set(emails) == set(created) and len(emails) == len(set(emails))
    assert all("placeholder.local" not in email for email in emails)
    # 每个地址都先占位成 in_use（注册期间归我们），并携带补号来源
    for email, run in zip(emails, built.calls["runs"]):
        assert built.repo.accounts[email]["status"] == "in_use"
        assert run["options"]["mail_source"] == "outlook"
        assert run["run_id"] == f"run-{email}"


def test_address_creation_failure_never_creates_a_run(factory, monkeypatch):
    """现造地址失败（CF 不通）时不该生成运行记录，只记原因。"""
    import mail_providers

    def boom(kind, settings=None, account=None):
        raise RuntimeError("CF Worker 不可达")

    monkeypatch.setattr(mail_providers, "create_mail_provider", boom)
    built = factory(settings=ENABLED, available=0, pooled=False, mailboxes=[])
    built.maintainer._cycle()
    assert built.calls["runs"] == []
    assert "CF Worker 不可达" in built.maintainer.snapshot()["lastError"]


def test_failed_registration_marks_the_created_address_as_failed(factory, monkeypatch):
    fake_mail_provider(monkeypatch)
    built = factory(settings=ENABLED, available=0, pooled=False, mailboxes=[])
    built.maintainer._cycle()
    address = built.calls["runs"][0]["account"]["email"]
    run_id = built.calls["runs"][0]["run_id"]
    built.repo.runs[run_id] = {"status": "failed", "error": "warmup 失败：409 invalid_state",
                               "error_category": "network"}
    built.maintainer._settle_address(address, run_id)
    assert built.repo.accounts[address]["status"] == "failed"
    assert "409" in built.repo.accounts[address]["fail_reason"]
    # 成功时绝不能被标失败
    built.repo.runs[run_id] = {"status": "done", "error": "", "error_category": None}
    built.repo.accounts[address] = {**built.repo.accounts[address], "status": "registered"}
    built.maintainer._settle_address(address, run_id)
    assert built.repo.accounts[address]["status"] == "registered"


def test_cycle_skips_when_pool_is_at_target(factory):
    built = factory(settings=ENABLED, available=5, mailboxes=["a@example.test"])
    built.maintainer._cycle()
    assert built.repo.claimed == [] and built.calls["runs"] == []


def test_cycle_skips_without_inventory(factory):
    built = factory(settings=ENABLED, available=0, mailboxes=[])
    built.maintainer._cycle()
    assert built.repo.claimed == [] and built.calls["runs"] == []
    assert built.repo.count_calls  # 确实问过库存


def test_cycle_skips_without_proxies_and_says_so(factory):
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test"], proxy_available=False)
    built.maintainer._cycle()
    assert built.repo.claimed == [] and built.calls["runs"] == []
    assert "代理" in built.maintainer.snapshot()["blockedReason"]


def test_cycle_registers_exactly_the_concurrency_budget(factory):
    boxes = [f"box{index}@example.test" for index in range(5)]
    built = factory(settings=ENABLED, available=0, mailboxes=boxes)
    built.maintainer._cycle()
    # min = 5，但并发额度是 2 → 本轮只claim两个（不长驻超发）
    assert built.repo.claimed == boxes[:2]
    assert [run["account"]["email"] for run in built.calls["runs"]] == boxes[:2]
    assert built.maintainer.snapshot()["rounds"] == 1


def test_cycle_never_exceeds_remaining_deficit(factory):
    built = factory(settings={**ENABLED, "pool_min_available": "1"}, available=0,
                    mailboxes=["a@example.test", "b@example.test"])
    built.maintainer._cycle()
    assert built.repo.claimed == ["a@example.test"]


def test_registration_options_are_pool_specific_and_isolated(factory):
    built = factory(settings={**ENABLED, "invite_otp_timeout": "120", "pool_otp_timeout": "181"},
                    available=0, mailboxes=["a@example.test"],
                    records={"begin": [], "runs": [], "end": [], "removed": []})
    built.maintainer._cycle()
    options = built.calls["runs"][0]["options"]
    assert options["pool_only"] is True
    assert options["want_2fa"] is True            # 池的定义：恒绑 2FA
    assert options["want_refresh_token"] is False  # 到 2FA 就停，不换 RT
    assert options["want_access_token"] is True and options["want_session_token"] is True
    assert options["otp_timeout"] == 181           # 用 pool_otp_timeout，不是 invite_otp_timeout
    assert options["proxy"] == PROXY


def test_registration_run_is_closed_out_when_it_raises(factory, monkeypatch):
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test"])
    from webui import registrar

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic registration failure")

    monkeypatch.setattr(registrar, "_do_register", explode)
    built.maintainer._cycle()
    # runner 自己 end_run 了也要再兜一次，且队列必须清掉，否则 handler/队列永久泄漏
    assert built.calls["end"] == ["run-a@example.test"]
    assert built.calls["removed"] == ["run-a@example.test"]


def test_missing_proxy_releases_the_claimed_mailbox(factory):
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test"])
    built.service.proxy = proxy_pool(available=True, url="")
    built.maintainer._cycle()
    assert built.repo.released == ["a@example.test"] and built.calls["runs"] == []


def test_repeated_failures_park_the_mailbox_for_humans(factory):
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test"], failures=3)
    built.maintainer._cycle()
    assert built.repo.failed and built.repo.failed[0][0] == "a@example.test"
    assert "人工" in built.repo.failed[0][1]
    assert built.calls["runs"] == []


def test_execution_readiness_ignores_the_invitation_executor_switch(factory):
    built = factory(settings=ENABLED, available=0)
    # 服务在跑、没在停 → 补号就该动，跟"邀请执行"开关没有关系
    built.service._started = True
    built.service._stopping = False
    assert built.maintainer._execution_ready() is True
    built.service._stopping = True
    assert built.maintainer._execution_ready() is False
    built.service._stopping = False
    built.service._started = False
    assert built.maintainer._execution_ready() is False


def test_settings_are_clamped_to_safe_ranges(factory):
    built = factory(settings={"pool_min_available": "-5", "pool_register_concurrency": "99",
                              "pool_check_seconds": "1", "pool_otp_timeout": "99999"})
    assert built.maintainer.minimum() == 0
    assert built.maintainer.concurrency() == 10
    assert built.maintainer.interval() == 10
    assert built.maintainer.otp_timeout() == 3600
    built.repo.settings.update({"pool_register_concurrency": "abc"})
    assert built.maintainer.concurrency() == 2


def test_start_and_stop_are_idempotent(factory, monkeypatch):
    built = factory(settings=ENABLED)
    monkeypatch.setattr(built.maintainer, "_cycle", lambda: None)
    monkeypatch.setattr(built.maintainer, "interval", lambda: 3600)
    built.maintainer.start()
    built.maintainer.start()
    assert built.maintainer.alive() is True
    built.maintainer.stop()
    built.maintainer.stop()
    assert built.maintainer.alive() is False


def test_snapshot_reports_the_pool_facts(factory):
    built = factory(settings=ENABLED, available=3, mailboxes=["a@example.test", "b@example.test"])
    snapshot = built.maintainer.snapshot()
    assert snapshot == {
        "enabled": True, "running": False, "target": 5, "available": 3,
        "mailSource": "outlook", "pooled": True,
        "mailboxAvailable": 2, "concurrency": 2, "checkSeconds": 60,
        "rounds": 0, "lastError": "", "blockedReason": "",
        "smsBound": 0, "smsUnbound": 0,
    }


def test_pool_registrations_are_recorded_under_their_own_run_kind(factory):
    """补号要能在「运行记录」里和人工单次注册区分开，否则一排「注册」看不出哪条是自动的。"""
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test", "b@example.test"])
    built.maintainer._cycle()
    assert built.calls["kinds"] == ["register_pool", "register_pool"]


def test_recent_failure_guard_counts_both_manual_and_automatic_runs(factory, monkeypatch):
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test"])
    seen = []

    def one(sql, *args):
        seen.append(sql)
        return {"n": 0}

    built.service.db = SimpleNamespace(one=one)
    built.maintainer._cycle()
    # 池化来源：按邮箱统计（人工单次注册也算，同一个号反复失败都得停）
    assert any("kind IN ('register','register_pool')" in sql for sql in seen), seen

    # 非池化来源每次都是新地址，"同一个号失败"无从谈起 → 走全局闸门
    built = factory(settings=ENABLED, available=0, pooled=False, mailboxes=["a@example.test"])
    seen.clear()
    built.service.db = SimpleNamespace(one=one)
    built.maintainer._cycle()
    assert any("kind='register_pool'" in sql and "email=?" not in sql for sql in seen), seen
    # 进程中断（服务重启/被杀）不是业务失败，绝不能算进闸门
    assert all("error_category NOT IN ('interrupted','skipped')" in sql for sql in seen), seen


def test_snapshot_explains_why_nothing_is_topped_up(factory):
    """开了开关却没补号时，管理端必须能直接看到原因，而不是去翻容器日志。"""
    on = {**ENABLED, "pool_auto_enabled": "true"}
    # 非池化来源：地址现造，没有库存问题 → 不是阻塞
    built = factory(settings=on, available=0, pooled=False)
    assert built.maintainer.snapshot()["blockedReason"] == ""
    # 池化来源但没有可注册的新邮箱
    built = factory(settings=on, available=0, pooled=True, mailboxes=[])
    assert "没有可注册的新邮箱" in built.maintainer.snapshot()["blockedReason"]
    # 近 1 小时整体失败太多 → 全局闸门
    built = factory(settings=on, available=0, pooled=False, failures=pool_module.FAILURE_LIMIT_GLOBAL)
    assert "已暂停自动补号" in built.maintainer.snapshot()["blockedReason"]
    # 没有可用代理
    built = factory(settings=on, available=0, pooled=True, mailboxes=["a@example.test"], proxy_available=False)
    assert "代理不可用" in built.maintainer.snapshot()["blockedReason"]
    # 开关关着：原因就是没开
    built = factory(settings={**on, "pool_auto_enabled": "false"}, available=0, mailboxes=["a@example.test"])
    assert "未开启" in built.maintainer.snapshot()["blockedReason"]
    # 条件齐备：没有阻塞原因
    built = factory(settings=on, available=0, pooled=True, mailboxes=["a@example.test"])
    assert built.maintainer.snapshot()["blockedReason"] == ""
    # 已经补够：也不算阻塞
    built = factory(settings={**on, "pool_min_available": "0"}, available=3, mailboxes=["a@example.test"])
    assert built.maintainer.snapshot()["blockedReason"] == ""


def test_blocked_cycles_log_once_and_do_not_raise(factory):
    """被阻塞时只记一条日志（不刷屏），且不能抛异常。"""
    built = factory(settings=ENABLED, available=0, pooled=True, mailboxes=[])
    built.maintainer._cycle()
    first = built.maintainer._last_reason
    assert "没有可注册的新邮箱" in first
    built.maintainer._cycle()
    assert built.maintainer._last_reason == first


def test_recent_failure_guard_counts_both_manual_and_automatic_runs(factory, monkeypatch):
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test"])
    seen = []

    def one(sql, *args):
        seen.append(sql)
        return {"n": 0}

    built.service.db = SimpleNamespace(one=one)
    built.maintainer._cycle()
    # 池化来源：按邮箱统计（人工单次注册也算，同一个号反复失败都得停）
    assert any("kind IN ('register','register_pool')" in sql for sql in seen), seen

    # 非池化来源每次都是新地址，"同一个号失败"无从谈起 → 走全局闸门
    built = factory(settings=ENABLED, available=0, pooled=False, mailboxes=["a@example.test"])
    seen.clear()
    built.service.db = SimpleNamespace(one=one)
    built.maintainer._cycle()
    assert any("kind='register_pool'" in sql and "email=?" not in sql for sql in seen), seen
    # 进程中断（服务重启/被杀）不是业务失败，绝不能算进闸门
    assert all("error_category NOT IN ('interrupted','skipped')" in sql for sql in seen), seen


def test_snapshot_explains_why_nothing_is_topped_up(factory):
    """开了开关却没补号时，管理端必须能直接看到原因，而不是去翻容器日志。"""
    on = {**ENABLED, "pool_auto_enabled": "true"}
    # 非池化来源：地址现造，没有库存问题 → 不是阻塞
    built = factory(settings=on, available=0, pooled=False)
    assert built.maintainer.snapshot()["blockedReason"] == ""
    # 池化来源但没有可注册的新邮箱
    built = factory(settings=on, available=0, pooled=True, mailboxes=[])
    assert "没有可注册的新邮箱" in built.maintainer.snapshot()["blockedReason"]
    # 近 1 小时整体失败太多 → 全局闸门
    built = factory(settings=on, available=0, pooled=False, failures=pool_module.FAILURE_LIMIT_GLOBAL)
    assert "已暂停自动补号" in built.maintainer.snapshot()["blockedReason"]
    # 没有可用代理
    built = factory(settings=on, available=0, pooled=True, mailboxes=["a@example.test"], proxy_available=False)
    assert "代理不可用" in built.maintainer.snapshot()["blockedReason"]
    # 开关关着：原因就是没开
    built = factory(settings={**on, "pool_auto_enabled": "false"}, available=0, mailboxes=["a@example.test"])
    assert "未开启" in built.maintainer.snapshot()["blockedReason"]
    # 条件齐备：没有阻塞原因
    built = factory(settings=on, available=0, pooled=True, mailboxes=["a@example.test"])
    assert built.maintainer.snapshot()["blockedReason"] == ""
    # 已经补够：也不算阻塞
    built = factory(settings={**on, "pool_min_available": "0"}, available=3, mailboxes=["a@example.test"])
    assert built.maintainer.snapshot()["blockedReason"] == ""


def test_blocked_cycles_log_once_and_do_not_raise(factory):
    """被阻塞时只记一条日志（不刷屏），且不能抛异常。"""
    built = factory(settings=ENABLED, available=0, pooled=True, mailboxes=[])
    built.maintainer._cycle()
    first = built.maintainer._last_reason
    assert "没有可注册的新邮箱" in first
    built.maintainer._cycle()
    assert built.maintainer._last_reason == first


def run_once(factory, run, email="a@example.test"):
    built = factory(settings=ENABLED, available=0, pooled=True, mailboxes=[email])
    built.repo.runs["run-" + email] = run
    built.maintainer._cycle()
    return built


def test_warmup_409_is_recorded_against_the_proxy(factory):
    """注册是拆分后唯一还会做 409 预检的链路，必须给代理记账。

    规则沿用既有 proxy_record_409：连续 2 次 → 冻结 20 分钟；
    解冻后再来一次 → 永久不可用；中途 warmup 通过则清零连续计数。
    """
    built = run_once(factory, {"status": "failed",
                              "error": "warmup 失败：继续注册必然 409 invalid_state（多为代理出口 IP 不通）"})
    assert [item[:3] for item in built.service.proxy.calls] == [("409", PROXY, "register")]


def test_successful_registration_clears_the_proxy_streak(factory):
    built = run_once(factory, {"status": "done", "error": ""})
    assert [item[:3] for item in built.service.proxy.calls] == [("success", PROXY, "register")]


def test_ordinary_failure_is_recorded_as_a_plain_proxy_failure(factory):
    built = run_once(factory, {"status": "failed", "error": "synthetic timeout"})
    assert [item[:3] for item in built.service.proxy.calls] == [("fail", PROXY, "register")]


def test_unreadable_run_leaves_proxy_accounting_untouched(factory):
    built = factory(settings=ENABLED, available=0, pooled=True, mailboxes=["a@example.test"])

    def boom(run_id):
        raise RuntimeError("synthetic read failure")

    built.repo.get_run = boom
    built.maintainer._cycle()
    assert built.service.proxy.calls == []


# ──────────────────────── 工位（占位）不泄漏 ────────────────────────


def test_no_spawn_without_a_free_permit_slot(factory):
    """没有空工位就不该生成任务 —— 以前会先建 run 再去门卫无限等，
    run 卡在 running、日志 0 字节，完全看不出原因。"""
    built = factory(settings=ENABLED, available=0, pooled=True, mailboxes=["a@example.test"])
    built.service.gate = SimpleNamespace(permits=SimpleNamespace(free_slots=lambda slots: 0))
    built.maintainer._cycle()
    assert built.calls["runs"] == [] and built.repo.claimed == []


def test_spawn_is_capped_by_free_slots(factory):
    built = factory(settings=ENABLED, available=0, pooled=True,
                    mailboxes=["a@example.test", "b@example.test", "c@example.test"])
    built.service.gate = SimpleNamespace(permits=SimpleNamespace(free_slots=lambda slots: 1))
    built.maintainer._cycle()
    assert len(built.calls["runs"]) == 1


def test_orphan_run_is_closed_when_the_task_never_started(factory):
    """门卫超时/闭锁导致任务没跑起来时，run 不能永远停在 running。"""
    built = factory(settings=ENABLED, available=0, pooled=True, mailboxes=["a@example.test"])
    built.repo.runs["orphan-1"] = {"status": "running", "error": "", "error_category": None}
    built.maintainer._finish_orphan_run("orphan-1")
    assert built.repo.finished == [
        ("orphan-1", "failed", "任务未启动（无空闲工位或执行器正在停止）", "skipped")
    ]
    # 已经结束的 run 绝不能被改写
    built.repo.runs["done-1"] = {"status": "done", "error": "", "error_category": None}
    built.maintainer._finish_orphan_run("done-1")
    assert len(built.repo.finished) == 1


# ──────────────────────── 运行记录自带进度 ────────────────────────


def test_registration_run_records_its_stage(factory, monkeypatch):
    """账号工厂补号没有卡密作业，进度必须写进 runs.stage；
    以前只有卡密作业有阶段，补号那条永远 0% 然后直接跳 100%。"""
    import smsinvit.invitation.runtime as runtime
    from webui import registrar

    written = []
    monkeypatch.setattr(registrar.db, 'set_run_stage', lambda run_id, stage: written.append((run_id, stage)))

    from smsinvit.invitation.runtime import STAGE_EMITTER

    registrar._install_run_stage("run-x")
    try:
        runtime.emit_task_stage("registering")
        runtime.emit_task_stage("register_otp")
        runtime.emit_task_stage("registered")
    finally:
        registrar._clear_run_stage()
    assert written == [("run-x", "registering"), ("run-x", "register_otp"), ("run-x", "registered")]
    # 清掉之后不该再写（免得跟着线程活到下一个任务）
    runtime.emit_task_stage("done")
    assert len(written) == 3


def test_registration_ladder_has_real_intermediate_progress():
    from webui import codex_auth

    ladder = codex_auth.USER_STAGE_PCT
    assert ladder['registering'] == 20
    assert ladder['sentinel_pow'] == 40
    assert ladder['register_otp'] == 50
    assert ladder['registered'] == 70
    assert ladder['done'] == 100


def test_snapshot_reports_sms_binding_counts(factory):
    """账号池卡片要能看到"已接码/未接码"：已接码的号兑换时免 add-phone。"""
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test"])
    built.repo.phone_bound = {1: 0, 0: 0}

    def count_phone_bound(kind="", bound=True):
        return built.repo.phone_bound[1 if bound else 0]

    built.repo.count_phone_bound = count_phone_bound
    built.repo.phone_bound[1] = 2
    built.repo.phone_bound[0] = 3
    snapshot = built.maintainer.snapshot()
    assert snapshot["smsBound"] == 2 and snapshot["smsUnbound"] == 3


def test_snapshot_survives_a_broken_sms_count(factory):
    built = factory(settings=ENABLED, available=0, mailboxes=["a@example.test"])

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic count failure")

    built.repo.count_phone_bound = boom
    snapshot = built.maintainer.snapshot()
    assert snapshot["smsBound"] == 0 and snapshot["smsUnbound"] == 0


# ──────────────────────── 注册阶段顺手验手机 ────────────────────────


def test_phone_binding_is_best_effort_and_never_fails_registration():
    """绑上 2FA 就算 GPT 注册成功：接码成功与否只影响兑换要不要 add-phone。"""
    from types import SimpleNamespace
    from webui import registrar

    class Flow:
        def __init__(self, sms=None, boom=False):
            self._sms_callback = sms
            self._boom = boom

        def _handle_add_phone_verification(self, continue_url=""):
            if self._boom:
                raise RuntimeError("synthetic add-phone failure")

    # 接码未启用 → 记为未接码，不抛
    ok, reason = registrar._bind_phone_best_effort(Flow(sms=None), "a@example.test")
    assert ok is False and "接码未启用" in reason
    # 接码成功
    ok, reason = registrar._bind_phone_best_effort(Flow(sms=object()), "a@example.test")
    assert ok is True and reason == ""
    # 接码失败 → 明确原因，但绝不抛（注册照常成功入池）
    ok, reason = registrar._bind_phone_best_effort(Flow(sms=object(), boom=True), "a@example.test")
    assert ok is False and "add-phone failure" in reason


def test_phone_binding_logs_in_before_verifying(monkeypatch):
    """注册阶段验手机：**先补协议登录，再验手机**（注册完成时 signup 会话已失效）。

    主人的要求：这里只登录，不做 Codex 授权 —— 授权是 codex 邀请阶段的事。
    """
    from types import SimpleNamespace
    from webui import codex_auth, registrar

    order = []
    monkeypatch.setattr(codex_auth, "_rebuild_auth_session",
                        lambda *a, **k: order.append("login") or True)

    class Flow:
        result = SimpleNamespace(password="pw", email="a@example.test")

        def __init__(self, sms=None, completed=False):
            self._sms_callback = sms
            if sms is not None:
                sms.completed = completed

        def _handle_add_phone_verification(self, continue_url=""):
            order.append("phone")

    sms = SimpleNamespace(completed=False)
    ok, reason = registrar._bind_phone_best_effort(Flow(sms), "a@example.test", provider=object())
    assert ok is True and order == ["login", "phone"], order

    # 协议登录过程中已经命中 add-phone 并验成功 → 不再重复验（否则白租一个号）
    order.clear()
    done_sms = SimpleNamespace(completed=True)
    ok, reason = registrar._bind_phone_best_effort(
        Flow(done_sms, completed=True), "a@example.test", provider=object())
    assert ok is True and order == ["login"], order


def test_phone_binding_reports_the_reason_when_login_path_fails(monkeypatch):
    from types import SimpleNamespace
    from webui import codex_auth, registrar

    monkeypatch.setattr(codex_auth, "_rebuild_auth_session", lambda *a, **k: False)

    class Flow:
        result = SimpleNamespace(password="pw", email="a@example.test")
        _sms_callback = SimpleNamespace(completed=False)

        def _handle_add_phone_verification(self, continue_url=""):
            raise RuntimeError("Your sign-in session is no longer valid")

    ok, reason = registrar._bind_phone_best_effort(Flow(), "a@example.test", provider=object())
    assert ok is False and "no longer valid" in reason

"""运行记录的进度：补号没有卡密作业，必须靠 runs.stage 自己带。

事故：自动补号那行的进度一直是 0%，跑完直接跳 100% —— 因为 /api/runs 只从
cdk_jobs 取 stage/progress，而补号根本没有卡密作业。
"""
import os
import tempfile

# webui.registrar 在导入时读这两个环境变量，必须在 import webui 之前设好
os.environ.setdefault("SMSINVIT_INVITATION_LOG_DIR", tempfile.mkdtemp(prefix="invitation-logs-"))
os.environ.setdefault("CODEX_HOMES_ROOT", tempfile.mkdtemp(prefix="codex-homes-"))

from webui import app as webui_app  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def stub_run_count(monkeypatch):
    """这一组测试只关心阶段/进度的合并，不关心分页总数。

    `/api/runs` 现在还会返回 `total`（服务端分页），这里让它跟着同一份替身数据走，
    免得每个用例都要额外打一次桩。
    """
    monkeypatch.setattr(webui_app.db, "count_runs",
                        lambda **_: len(webui_app.db.list_runs()))


def test_pool_run_reports_its_own_stage_and_percentage(monkeypatch):
    monkeypatch.setattr(webui_app.db, "list_runs", lambda limit=50, **_: [
        {"run_id": "r1", "email": "pool@cf.test", "status": "running",
         "started_at": 100.0, "finished_at": None, "stage": "register_otp", "kind": "register_pool"},
    ])
    monkeypatch.setattr(webui_app.db, "list_cdk_jobs_filtered", lambda **kw: [])
    items = webui_app.api_runs(limit=5)["items"]
    assert items[0]["stage_label"] == "正在等待邮箱验证码"
    assert items[0]["progress"] == 50


def test_cdk_job_still_wins_when_present(monkeypatch):
    """邀请作业有卡密作业时以作业为准（它还带 attempt/proxy）。"""
    monkeypatch.setattr(webui_app.db, "list_runs", lambda limit=50, **_: [
        {"run_id": "r2", "email": "invite@cf.test", "status": "running",
         "started_at": 100.0, "finished_at": None, "stage": "registering"},
    ])
    monkeypatch.setattr(webui_app.db, "list_cdk_jobs_filtered", lambda **kw: [
        {"email": "invite@cf.test", "stage": "codex_oauth", "progress": 80, "attempt": 1,
         "proxy": "http://p:1", "job_id": "j1"},
    ])
    item = webui_app.api_runs(limit=5)["items"][0]
    assert item["stage_label"] == "Codex 授权成功" and item["progress"] == 80
    assert item["proxy"] == "http://p:1"


def test_finished_run_reports_one_hundred_percent(monkeypatch):
    monkeypatch.setattr(webui_app.db, "list_runs", lambda limit=50, **_: [
        {"run_id": "r3", "email": "pool@cf.test", "status": "done",
         "started_at": 100.0, "finished_at": 200.0, "stage": "registered"},
    ])
    monkeypatch.setattr(webui_app.db, "list_cdk_jobs_filtered", lambda **kw: [])
    item = webui_app.api_runs(limit=5)["items"][0]
    assert item["stage_label"] == "完成" and item["progress"] == 100


def test_unknown_stage_does_not_break_the_list(monkeypatch):
    monkeypatch.setattr(webui_app.db, "list_runs", lambda limit=50, **_: [
        {"run_id": "r4", "email": "pool@cf.test", "status": "running",
         "started_at": 100.0, "finished_at": None, "stage": "some_future_stage"},
    ])
    monkeypatch.setattr(webui_app.db, "list_cdk_jobs_filtered", lambda **kw: [])
    item = webui_app.api_runs(limit=5)["items"][0]
    assert item["stage_label"] == "some_future_stage" and item["progress"] == 0


def test_failed_run_without_a_stage_still_says_failed(monkeypatch):
    monkeypatch.setattr(webui_app.db, "list_runs", lambda limit=50, **_: [
        {"run_id": "r5", "email": "pool@cf.test", "status": "failed",
         "started_at": 100.0, "finished_at": 120.0, "stage": None},
    ])
    monkeypatch.setattr(webui_app.db, "list_cdk_jobs_filtered", lambda **kw: [])
    item = webui_app.api_runs(limit=5)["items"][0]
    assert item["stage_label"] == "失败"


def test_runs_endpoint_filters_and_pages_on_the_server(monkeypatch):
    """/runs 的筛选与分页必须在服务端：以前是"先截断最近 N 条再在浏览器里筛"。"""
    seen = {}

    def list_runs(limit=50, **kwargs):
        seen.update({"limit": limit, **kwargs})
        return [{"run_id": "r1", "email": "a@example.test", "status": "done",
                 "started_at": 1.0, "finished_at": 2.0, "kind": "codex_auth"}]

    monkeypatch.setattr(webui_app.db, "list_runs", list_runs)
    monkeypatch.setattr(webui_app.db, "count_runs", lambda **_: 42)
    # 阶段补全用到的活动作业查询也要挡掉（否则会去连真正的库）。
    monkeypatch.setattr(webui_app.db, "list_cdk_jobs_filtered", lambda **_: [])
    result = webui_app.api_runs(limit=20, status="done", kind="codex_auth", search="", offset=40)
    assert result["total"] == 42 and len(result["items"]) == 1
    assert seen == {"limit": 20, "status": "done", "kind": "codex_auth", "search": "", "offset": 40}


def test_monitor_summary_counts_globally_not_just_the_returned_page(monkeypatch):
    """实时监控的总数必须是全局值：旧实现只统计本页 items，limit 之外的任务会消失。"""
    monkeypatch.setattr(webui_app.db, "list_cdk_jobs_filtered", lambda **_: [])
    monkeypatch.setattr(webui_app.db, "list_recent_cdk_events", lambda limit=60: [])
    monkeypatch.setattr(webui_app.db, "cdk_stats",
                        lambda: {"unused": 0, "jobs_running": 3, "jobs_queued": 17,
                                 "done": 9, "failed": 2, "total": 31, "mail_pool": 5})
    monkeypatch.setattr(webui_app.db, "stats", lambda: {"mail_pool": 5})
    monkeypatch.setattr(webui_app.cdk.CONTROLLER, "stats",
                        lambda: {"worker_alive": 3, "max_concurrency": 3, "proxies": {"available": 1}})
    summary = webui_app.api_cdk_monitor(limit=40)["summary"]
    assert summary["running"] == 3 and summary["queued"] == 17
    assert summary["done"] == 9 and summary["failed"] == 2
    assert summary["itemsLimit"] == 40 and summary["itemsReturned"] == 0

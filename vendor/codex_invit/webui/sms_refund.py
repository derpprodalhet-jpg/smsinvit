"""三个平台的待取消/待完成订单，每分钟核对，最多三次后转人工核对。"""
from __future__ import annotations

import logging
import threading
import time

from . import db
from smsinvit.invitation import sms_state

logger = logging.getLogger("sms_refund")

TICK_SECONDS = 10.0      # worker 扫描间隔（任务本身的间隔是 60s，由 next_at 控制）
MAX_ATTEMPTS = 3


def _provider_for(platform: str):
    """按平台名构造 provider（配置每次现取，改 Key 后立刻生效）。"""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from sms_provider import create_sms_provider  # noqa: PLC0415

    cfg = db.get_sms_internal_config()
    return create_sms_provider(platform, cfg)


def enqueue(platform: str, activation_id: str, reason: str = "") -> int:
    """登记退号重试任务（供 provider/controller 调用）。"""
    task_id = db.refund_task_add(platform, activation_id, reason, max_attempts=MAX_ATTEMPTS,
                                 delay_seconds=60.0)
    if task_id:
        logger.warning("[sms_refund] %s 的号 %s 退号失败，已挂后台任务 #%s：每 60s 重试，最多 %d 次",
                       platform, activation_id, task_id, MAX_ATTEMPTS)
        try:
            db.add_cdk_event("", "", "refund_retry",
                             f"{platform} {activation_id} 退号失败，已挂重试任务（每 60s，最多 {MAX_ATTEMPTS} 次）")
        except Exception:  # noqa: BLE001
            pass
    return task_id


def run_once() -> dict:
    """扫一轮：把到点的任务拿去重试。返回本轮统计（方便测试与页面展示）。"""
    out = {"tried": 0, "ok": 0, "retry": 0, "gave_up": 0, "errors": []}
    for task in db.refund_task_due(limit=20):
        tid = int(task["id"])
        if not db.refund_task_claim(tid):
            continue                      # 被别的进程抢先了
        out["tried"] += 1
        platform = str(task.get("platform") or "")
        aid = str(task.get("activation_id") or "")
        try:
            provider = _provider_for(platform)
            ok = bool(provider.cancel(aid))
            err = "" if ok else str(getattr(provider, "last_cancel_raw", "") or "平台未确认取消")
            if ok:
                try:
                    sms_state.order_state(platform, aid, "cancelled")
                except RuntimeError as exc:
                    # 部署前已有退款任务可能没有新订单记录，仍应确认原任务。
                    if "订单记录不存在" not in str(exc):
                        raise
        except Exception as e:  # noqa: BLE001
            ok = False
            err = f"{type(e).__name__}: {e}"
        row = db.refund_task_finish(tid, ok, err)
        if ok:
            out["ok"] += 1
            logger.info("[sms_refund] 任务 #%s 退号成功（%s %s）", tid, platform, aid)
        elif row.get("status") == "gave_up":
            out["gave_up"] += 1
            logger.error("[sms_refund] 任务 #%s 重试 %s 次仍失败，不再继续退：%s %s（%s）",
                         tid, row.get("attempts"), platform, aid, err[:160])
        else:
            out["retry"] += 1
            logger.warning("[sms_refund] 任务 #%s 第 %s 次重试仍失败，60s 后再试：%s",
                           tid, row.get("attempts"), err[:160])
        if err:
            out["errors"].append(err[:160])
    out["settlements"] = run_settlements_once()
    return out


def run_settlements_once() -> dict:
    """业务已成功的订单仅补平台完成确认，不重跑发码或 OTP 验证。"""
    out = {"tried": 0, "ok": 0, "review": 0}
    for task in sms_state.settlement_due(limit=20):
        tid = task["id"]
        claim = sms_state.settlement_claim(tid)
        if not claim:
            continue
        out["tried"] += 1
        try:
            provider = _provider_for(task["platform"])
            ok = bool(provider.report_success(task["activation_id"]))
            error = "" if ok else "供应商未确认完成"
        except Exception as exc:
            ok, error = False, type(exc).__name__
        row = sms_state.settlement_result(tid, ok, error, claim=claim)
        out["ok"] += int(ok)
        out["review"] += int(row.get("status") == "finish_review")
    return out


class RefundWorker:
    """每 TICK_SECONDS 秒扫一轮到点任务。两个进程各跑一个也不会重复退（原子认领）。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rounds = 0
        self._last = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sms-refund", daemon=True)
        self._thread.start()
        logger.info("[sms_refund] 退号重试 worker 已启动（每 %.0fs 扫一次，任务间隔 60s，最多 3 次）",
                    TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self._last = run_once()
                self._rounds += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[sms_refund] 扫描异常: %s", e)

    def snapshot(self) -> dict:
        return {
            "alive": self.alive(),
            "rounds": self._rounds,
            "tick_seconds": TICK_SECONDS,
            "max_attempts": MAX_ATTEMPTS,
            "interval_seconds": 60,
            "last": self._last,
            "stats": db.refund_task_stats(),
            "tasks": db.refund_task_list(limit=20),
        }


WORKER = RefundWorker()

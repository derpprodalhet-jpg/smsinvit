"""CDK 控制器：卡密状态机 + 全局并发上限（默认 3）+ 排队。

分工：
  · `enqueue()`  —— 用户点「开始执行」时调用。**开跑前先查有没有可用代理**，
                    没有就直接返回 busy（不建作业、不动卡密状态）；
  · N 个 worker  —— 从 FIFO 队列里取作业，调 `codex_auth.run_invite_job`；
                    每个作业建一条 runs 记录（kind=codex_auth），日志与
                    「运行记录」页、SSE 日志流完全打通；
  · `status_for_codes()` —— 给用户前台的**脱敏**状态（永不含代理/凭证/错误码）。

⚠️ worker 只在**用户前台进程**里启动（`webui/user_app.py`）。管理后台进程不启动，
   否则两个进程各开 3 个 worker，全局并发就变成 6 了。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Optional

from . import cdk_formats, codex_auth, db, proxy_pool, registrar

logger = logging.getLogger("cdk")

# 卡密状态 → 给用户前台的状态词
_USER_STATUS = {
    "unused": "unused",
    "bound": "bound",
    "queued": "queued",
    "running": "running",
    "done": "done",
    "failed": "failed",
    "void": "void",
}


def _int_setting(key: str, default: int) -> int:
    try:
        return int(str(db.get_setting(key, str(default))).strip())
    except Exception:
        return default


class CdkController:
    def __init__(self):
        self._lock = threading.RLock()
        self._q: "queue.Queue" = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._running = 0
        self._spawned = 0
        # 正在被某个工人拿在手上的作业（job_id → 取走时间）。收尾对账要靠它
        # 把"排队中但没人拿"和"刚被取走、还没写库"区分开。
        self._active: dict[str, float] = {}

    # ── 并发 ──
    def max_concurrency(self) -> int:
        return max(1, _int_setting("cdk_max_concurrency", 3))

    def _spawn(self) -> threading.Thread:
        """建一个 worker 线程。名字用进程内单调计数，补建时不会和旧名字撞车。"""
        index = self._spawned
        self._spawned += 1
        thread = threading.Thread(target=self._worker, args=(index,), daemon=True,
                                  name=f"cdk-worker-{index}")
        thread.start()
        self._workers.append(thread)
        return thread

    def start_workers(self) -> dict:
        """幂等启动 worker（只在用户前台进程调用）；已启动时只**补齐**死掉的。

        `_started` 只表示"启动过"，不代表还有几个活着。旧实现据此直接返回，
        于是一个 worker 因写库异常退出后容量就永久少一个；全部退出后页面还在
        收任务，却永远没人做。容量上限恒为 `max_concurrency()`，绝不放大。
        """
        with self._lock:
            already = self._started
            self._started = True
            if already:
                self._workers[:] = [t for t in self._workers if t.is_alive()]
            limit = self.max_concurrency()
            created = 0
            while len(self._workers) < limit:
                self._spawn()
                created += 1
            if created:
                logger.info("[cdk] 已启动 %d 个 worker（全局并发上限 %d，当前存活 %d）",
                            created, limit, self.worker_alive())
            return {"ok": True, "already": already and created == 0,
                    "workers": len(self._workers), "created": created}

    def ensure_workers(self) -> int:
        """补齐死掉的 worker 并返回当前存活数；未启动的进程不做任何事。

        接任务之前调用：先把容量补回来，再决定要不要收活。补不回来时返回 0，
        调用方据此拒绝新任务（绝不让任务进一个没有工人的队列）。
        """
        if not self._started:
            return self.worker_alive()
        try:
            self.start_workers()
        except Exception:  # noqa: BLE001 — 补建失败不能把收活路径也带崩
            logger.exception("[cdk] 补建 worker 失败")
        return self.worker_alive()

    def worker_alive(self) -> int:
        return sum(1 for t in self._workers if t.is_alive())

    def in_flight(self) -> dict:
        """工人已经取走、还没收尾的作业（job_id → 取走时间）。"""
        with self._lock:
            return dict(self._active)

    def queued_job_ids(self) -> set:
        """内存队列里等待的 job_id。"""
        with self._q.mutex:
            return {str(item.get("job_id")) for item in self._q.queue
                    if isinstance(item, dict) and item.get("job_id")}

    def dispatch(self, job_id: str, code: str) -> None:
        """把一个作业递给工人。队列内部结构只在这里露出，调用方不必碰 `_q`。"""
        self._q.put({"job_id": str(job_id), "code": code})

    # ── 入队 ──
    def busy_message(self) -> str:
        return "当前繁忙，请稍后重试"

    def can_start(self) -> bool:
        """开跑前预检查：有可用代理才允许开始。"""
        return proxy_pool.has_available()

    def enqueue(self, code: str) -> dict:
        code = str(code or "").strip()
        # 已经启动、但一个工人都不剩时不能收活：任务进来也永远没人做。
        # 先补建（幂等），补不回来就按"繁忙"拒绝，卡密状态保持不变。
        if self.ensure_workers() <= 0 and self._started:
            return {"ok": False, "reason": "busy", "message": self.busy_message()}
        cdk = db.get_cdk_code(code)
        if not cdk:
            return {"ok": False, "reason": "not_found", "message": "未找到该卡密"}

        # 已有排队/运行中的任务 → 返回现有的，不重复入队
        # （要放在状态校验**之前**：入队后卡密状态就变成 queued/running 了）
        for job in db.list_cdk_jobs(code=code, limit=5):
            if job.get("status") in ("queued", "running"):
                return {"ok": True, "already": True, "job_id": job["job_id"],
                        "position": self.queue_position(job["job_id"])}

        if not cdk.get("invite_sent_at"):
            return {"ok": False, "reason": "invite_not_sent",
                    "message": "请先确认「我已发送邀请信」"}
        if cdk.get("status") not in ("bound", "failed"):
            return {"ok": False, "reason": "bad_status",
                    "message": f"当前状态（{cdk.get('status')}）不能开始执行"}

        if not self.can_start():
            db.add_cdk_event(code, "", "proxy_unavailable", "开跑前预检查：无可用代理")
            return {"ok": False, "reason": "busy", "message": self.busy_message()}

        job_id = uuid.uuid4().hex
        db.create_cdk_job(job_id, code, email=cdk.get("email") or "", attempt=1)
        db.set_cdk_status(code, "queued", error=None, finished_at=None)
        db.add_cdk_event(code, job_id, "enqueue", "已入队")
        return {"ok": True, "already": False, "job_id": job_id,
                "position": self.queue_position(job_id)}

    # ── worker ──
    def _worker(self, idx: int) -> None:
        logger.info("[cdk] worker-%d 启动", idx)
        while True:
            item = self._q.get()
            if item is None:
                return
            job_id = str(item.get("job_id") or "") if isinstance(item, dict) else ""
            with self._lock:
                self._active[job_id] = time.time()
            # 兜底：任何未预料的异常都不该让这个工人永久下班。少一个工人是
            # 静默的容量损失（页面照收任务，却少一份处理能力）。
            try:
                self._run_job(idx, item)
            except BaseException:  # noqa: BLE001
                logger.exception("[cdk] worker-%d 处理作业 %s 时出现未预料的异常", idx, job_id)
            finally:
                with self._lock:
                    self._active.pop(job_id, None)

    def _mark_failed(self, code: str, job_id: str, reason: str) -> None:
        """尽力把作业与卡密落成失败，别让它们卡在 queued/running。

        只在**连"开始执行"都没能做到**时使用；绝不写成 done。
        """
        for description, action in (
            ("作业", lambda: db.set_cdk_job(job_id, status="failed", finished_at=time.time(), error=reason)),
            ("卡密", lambda: db.set_cdk_status(code, "failed", finished_at=time.time(), error=reason)),
        ):
            try:
                action()
            except Exception:  # noqa: BLE001
                logger.exception("[cdk] 作业 %s 的%s失败状态回写失败", job_id, description)

    def _run_job(self, idx: int, item: dict) -> None:
        job_id = str(item.get("job_id") or "")
        code = str(item.get("code") or "")
        run_id = None
        handler = None
        counted = False
        status, err = "failed", "作业未完成"
        try:
            cdk = db.get_cdk_code(code) or {}
            email = str(cdk.get("email") or "")
            # 原子认领：只有仍处于 queued 的作业才允许开始。管理员在这个队列项被取出
            # 之前点了停止时，作业已经是 failed，认领失败，这里必须直接丢弃——不能
            # 回写 running、不能建运行记录、也不能调用 _mark_failed（那会把管理员写的
            # 停止原因覆盖成「本次未启动」，让复核看到错误的原因）。
            if not db.claim_cdk_job(job_id):
                logger.info("[cdk] 作业 %s 已不是排队状态（管理员停止或已被认领），丢弃本次执行", job_id)
                return
            try:
                run_id, _log_file, _q, handler = registrar.begin_run(email, kind="codex_auth")
            except Exception:  # noqa: BLE001
                logger.exception("[cdk] 作业 %s 无法建立运行记录，跳过本次执行", job_id)
                self._mark_failed(code, job_id, "无法建立运行记录，本次未启动。")
                return
            try:
                db.set_cdk_status(code, "running")
            except Exception:  # noqa: BLE001
                logger.exception("[cdk] 作业 %s 无法标记为运行中，跳过本次执行", job_id)
                self._mark_failed(code, job_id, "无法标记为运行中，本次未启动。")
                return
            with self._lock:
                self._running += 1
            counted = True
            logger.info("[cdk] worker-%d 开始作业 %s（%s）", idx, job_id, code)
            try:
                res = codex_auth.run_invite_job(code, run_id=run_id, job_id=job_id)
                if not res.get("ok"):
                    status, err = "failed", str(res.get("reason") or "")
                else:
                    status, err = "done", ""
            except Exception as e:  # noqa: BLE001
                status, err = "failed", f"{type(e).__name__}: {e}"
                logger.exception("[cdk] 作业 %s 异常", job_id)
        finally:
            # 收尾的每一步都各自兜住：任何一步失败都不能跳过运行计数的归还，
            # 也不能把日志处理器永久留在 root logger 上。
            if counted:
                try:
                    category = registrar.classify_error(err) if err else ""
                except Exception:  # noqa: BLE001
                    category = "unknown"
                try:
                    db.finish_run(run_id, status, error=err, category=category)
                except Exception:  # noqa: BLE001
                    logger.exception("[cdk] 作业 %s 的运行记录收尾失败", job_id)
            if run_id is not None:
                try:
                    registrar.end_run(run_id, handler)
                except Exception:  # noqa: BLE001
                    logger.exception("[cdk] 作业 %s 的日志处理器收尾失败", job_id)
            if counted:
                with self._lock:
                    self._running -= 1
            logger.info("[cdk] worker-%d 作业 %s 结束: %s %s", idx, job_id, status, str(err)[:200])

    # ── 查询 ──
    def queue_position(self, job_id: str) -> int:
        """排队名次（1 = 下一个跑）。

        不在排队中、或有未解除复核的返回 0：待复核的旧任务根本不会被调度，把它
        算进名次会让用户看到的数字虚高。同一时刻入队的任务用作业主键兜底排序，
        名次不再并列。
        """
        job = db.get_cdk_job(job_id)
        if not job or job.get("status") != "queued":
            return 0
        # 作业主键在库里是以加密标记存的，拼稳定排序键要用同一套换算。
        from smsinvit.invitation.mysql import marker
        key = marker(str(job_id))
        con = db._conn()
        if con.execute(
            "SELECT job_id FROM invitation_v2_task_holds WHERE job_id=? AND resolved_at IS NULL",
            (key,),
        ).fetchone():
            return 0
        created = job.get("created_at") or 0
        n = con.execute(
            "SELECT COUNT(*) AS n FROM cdk_jobs j "
            "LEFT JOIN invitation_v2_task_holds h ON h.job_id=j.job_id AND h.resolved_at IS NULL "
            "WHERE j.status='queued' AND h.job_id IS NULL "
            "AND (j.created_at < ? OR (j.created_at = ? AND j.job_id < ?))",
            (created, created, key),
        ).fetchone()["n"]
        return int(n) + 1

    def status_for_codes(self, codes: list[str]) -> list[dict]:
        """给用户前台的脱敏状态。

        只包含：code / email / status / stage / 文案 / 进度 / 排队名次 / 是否有结果。
        **绝不含**代理、令牌、密码、2FA、内部错误码。
        """
        cleaned = [str(c or "").strip() for c in (codes or []) if str(c or "").strip()]
        rows = {r["code"]: r for r in db.get_cdk_codes(cleaned)}
        out = []
        for code in cleaned:
            r = rows.get(code)
            if not r:
                out.append({"code": code, "email": "", "status": "unknown",
                            "stage": "unknown", "stage_label": "未知卡密",
                            "progress": 0, "queue_position": 0, "has_result": False,
                            "fail_count": 0})
                continue
            status = _USER_STATUS.get(str(r.get("status") or "unused"), "unknown")
            stage, progress, position = "", 0, 0
            jobs = db.list_cdk_jobs(code=code, limit=1)
            job = jobs[0] if jobs else None
            if status == "queued":
                stage = "queued"
                position = self.queue_position(job["job_id"]) if job else 0
            elif status == "running":
                stage = str((job or {}).get("stage") or "registering")
                progress = int((job or {}).get("progress") or 0)
            elif status == "done":
                stage, progress = "done", 100
            elif status == "failed":
                stage = "failed"
                progress = int((job or {}).get("progress") or 0)
            elif status == "bound":
                stage = "bound"
            elif status == "void":
                stage = "void"
            else:
                stage = "unused"
            out.append({
                "code": code,
                "email": str(r.get("email") or ""),
                "status": status,
                "stage": stage,
                "stage_label": codex_auth.USER_STAGE_LABEL.get(stage, _fallback_label(stage)),
                "progress": progress,
                "queue_position": position,
                "has_result": status == "done",
                "fail_count": int(r.get("fail_count") or 0),
            })
        return out

    def stats(self) -> dict:
        out = dict(db.cdk_stats())
        out.update({
            "proxies": proxy_pool.summary(),
            "worker_alive": self.worker_alive(),
            "max_concurrency": self.max_concurrency(),
            "running_workers": self._running,
            "queue_size": self._q.qsize(),
        })
        return out


def _fallback_label(stage: str) -> str:
    return {
        "unused": "未兑换",
        "bound": "待开始",
        "queued": "正在排队",
        "running": "正在执行",
        "done": "完成",
        "failed": "失败",
        "void": "已作废",
        "unknown": "未知",
    }.get(stage, stage)


CONTROLLER = CdkController()

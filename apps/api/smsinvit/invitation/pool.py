"""账号工厂：把邮箱注册成 GPT 账号（绑完 2FA 就停），维护可兑换账号池。

流程拆分后，注册不再发生在用户的邀请作业里。这个维护任务负责：

    pool_available < pool_min_available 时，用代理把 available 邮箱注册到
    status='registered'（有密码 / 2FA / Access / Session，且**没有** RT），
    等用户的 CDK 兑换时再取号。

与邀请侧**完全解耦**（主人要求"各配置各的，不要互相影响"）：

    · 开关   pool_auto_enabled            —— 补号自己的，不看 invitation_executor_enabled
    · 阈值   pool_min_available
    · 并发   pool_register_concurrency     —— 工位 17-32（permits.POOL_SLOTS）
    · 巡检   pool_check_seconds
    · 超时   pool_otp_timeout              —— 不用 invite_otp_timeout

唯一共享的是基础设施本身：同一个 MySQL、同一个代理池、同一个进程内 TaskGate。
每次补号都自己 claim 邮箱、自己 pick 代理，失败只记自己的账。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

TRUE_VALUES = ("1", "true", "yes", "on")
# 同一个邮箱在这个窗口内失败这么多次就不再自动重试 —— 否则网络类错误会被
# release_unused 放回 available，下一轮立刻再 claim，拿代理和接码余额空转。
FAILURE_WINDOW_SECONDS = 3600
FAILURE_LIMIT = 3
# 整体闸门：非池化来源每轮都是新地址，只能按"近 1 小时失败总数"刹车，
# 否则代理/来源配置有问题时会一直造地址一直失败。
FAILURE_LIMIT_GLOBAL = 6


class AccountPoolMaintainer:
    """单个协调线程 + 每轮最多 N 个并行的补号任务（N 可热改，1..10）。"""

    def __init__(self, service):
        self.service = service
        self.repo = service.repo
        self.alloc = service.alloc
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        # 本轮 spawn 出去的补号线程；手动停止时要 join 它们，否则"停了"只是不再开新一轮。
        self._round_threads = []
        self._rounds = 0
        self._last_error = ""
        # 上一次"为什么没补号"的原因，用于只在原因变化时打日志。
        self._last_reason = ""

    # ──────────────────────── 生命周期 ────────────────────────
    def start(self):
        """幂等；不抛异常 —— service.start() 靠它保证失败也能回滚。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="invitation-pool")
            self._thread.start()

    def stop(self):
        """置位并 join。join 有界：在跑的注册任务由 TaskGate/占位表兜底，不在这里硬等。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        with self._lock:
            self._thread = None

    def alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def stop_round(self) -> int:
        """手动停止本轮补号并 join 正在跑的 `_register_one` 线程。

        返回本轮真正结束掉的线程数。join 有界：单个补号卡在外部请求上时，
        由 TaskGate / 占位表兜底，不在这里无限等。
        """
        self._stop.set()
        with self._lock:
            thread = self._thread
            round_threads = list(self._round_threads)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        stopped = 0
        for worker in round_threads:
            worker.join(timeout=10)
            if not worker.is_alive():
                stopped += 1
        with self._lock:
            self._thread = None
            self._round_threads = []
        return stopped

    # ──────────────────────── 配置（实时读，非法值回落到安全默认） ────────────────────────
    def _int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            number = int(str(self.repo.get_setting(key, str(default))).strip())
        except (TypeError, ValueError):
            number = default
        return max(low, min(high, number))

    def enabled(self) -> bool:
        return str(self.repo.get_setting("pool_auto_enabled", "false")).strip().lower() in TRUE_VALUES

    def minimum(self) -> int:
        return self._int("pool_min_available", 10, 0, 1000)

    def concurrency(self) -> int:
        return self._int("pool_register_concurrency", 2, 1, 10)

    def interval(self) -> int:
        return self._int("pool_check_seconds", 60, 10, 3600)

    def otp_timeout(self) -> int:
        return self._int("pool_otp_timeout", 180, 10, 3600)

    def mail_source(self) -> str:
        """补号用哪个邮箱来源。**与兑换侧独立**：留空才回落到全局 mail_source。

        这样账号池可以是 CF 临时邮箱（现造地址）而兑换侧用 outlook，反之亦然；
        每个号按自己登记时的来源取信（见 mail_alloc.build_provider_for_account）。
        """
        explicit = str(self.repo.get_setting("pool_mail_source", "") or "").strip().lower()
        return explicit or str(self.repo.get_setting("mail_source", "outlook") or "outlook").strip().lower()

    def pooled_source(self) -> bool:
        try:
            return bool(self.alloc.kind_is_pooled(self.mail_source()))
        except Exception:  # noqa: BLE001 — 未知来源按池化保守处理
            return True

    # ──────────────────────── 主循环 ────────────────────────
    def _loop(self):
        logger.info("[pool] 账号池维护任务启动")
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("[pool] 补号巡检异常")
            self._stop.wait(self.interval())
        logger.info("[pool] 账号池维护任务停止")

    def _execution_ready(self) -> bool:
        """只依赖服务本身是否在跑 —— 刻意不看 invitation_executor_enabled。"""
        service = self.service
        return bool(service._started and not service._stopping)

    def _block_reason(self) -> str:
        """当前为什么没在补号。空串 = 条件齐备，可以补。

        全部判断只读。以前这些分支是静默 return 的，管理员打开开关后
        只看到"什么都没发生"，查不出原因 —— 这是必须消除的可观测性缺陷。
        """
        if not self._execution_ready():
            return "邀请执行进程未运行（服务未启动或正在停止）"
        if not self.enabled():
            return "未开启「账号池自动补号」"
        if self._recent_failures("") >= FAILURE_LIMIT_GLOBAL:
            return (
                f"近 1 小时补号连续失败 {self._recent_failures('')} 次，已暂停自动补号，"
                "请先在运行记录里排查失败原因"
            )
        deficit = self.minimum() - self.alloc.pool_available()
        if deficit <= 0:
            return ""
        kind = self.mail_source()
        # 池化来源要靠导入的"新邮箱"才有号可注册；非池化来源的地址是注册时
        # 现造的，不存在库存问题。
        if self.pooled_source() and int(self.repo.count_accounts("available", kind) or 0) <= 0:
            return f"来源 {kind} 没有可注册的新邮箱，请先在邮箱管理里导入"
        if not self.service.proxy.has_available(purpose="register"):
            return "代理不可用：没有可用代理（可能都不可用/在冻结中），请到代理池检查或重置"
        return ""

    def _cycle(self):
        reason = self._block_reason()
        if reason:
            # 只在原因变化时打一条，别每 60 秒刷屏。
            if reason != self._last_reason:
                logger.warning("[pool] 本轮未补号：%s", reason)
                self._last_reason = reason
            return
        kind = self.mail_source()
        deficit = self.minimum() - self.alloc.pool_available()
        budget = min(deficit, self.concurrency())
        if self.pooled_source():
            budget = min(budget, int(self.repo.count_accounts("available", kind) or 0))
        # 没有空工位就**不要生成任务**：以前会先建 run 再去门卫无限等，
        # 结果 run 卡在 running、日志 0 字节，完全看不出原因。
        free = self._free_slots()
        if free is not None:
            budget = min(budget, free)
            if free <= 0:
                if self._last_reason != "no-slot":
                    logger.warning("[pool] 没有空闲的补号工位（占位被占用），本轮跳过")
                    self._last_reason = "no-slot"
                return
        if budget <= 0:
            return
        self._last_reason = ""
        self._rounds += 1
        logger.info("[pool] 可兑换 %s 个，目标 %s，本轮补 %s 个（补号来源=%s，%s）",
                    self.alloc.pool_available(), self.minimum(), budget, kind,
                    "从已导入邮箱取号" if self.pooled_source() else "注册时现造地址")
        workers = [threading.Thread(target=self._register_one, args=(kind,), daemon=True,
                                    name=f"invitation-pool-task-{index}")
                   for index in range(budget)]
        with self._lock:
            self._round_threads = list(workers)
        for worker in workers:
            worker.start()
        for worker in workers:
            # stop() 置位后立刻放手：在跑的任务由 TaskGate/占位表兜底，
            # 不在这里把 service.stop() 拖死。
            while worker.is_alive() and not self._stop.is_set():
                worker.join(timeout=1)
        with self._lock:
            self._round_threads = [worker for worker in workers if worker.is_alive()]

    # ──────────────────────── 单个补号 ────────────────────────
    def _register_one(self, kind: str):
        from webui import registrar

        pooled = self.pooled_source()
        if pooled:
            account = self.repo.claim_next(kind)
            if not account:
                return
            email = str(account.get("email") or "").strip().lower()
        else:
            # 非池化来源：地址在注册那一刻现造。**先造好再建运行记录** ——
            # 这样运行记录/日志/邮箱管理里从第一秒就是真实地址，失败也查得到是哪个号；
            # 以前先用 @placeholder.local 占位，失败时根本看不出是哪个地址。
            address, failure = self._create_address(kind)
            if not address:
                self._last_error = failure
                logger.error("[pool] 现造地址失败，本轮不补号: %s", failure)
                return
            account = {"email": address, "password": "", "client_id": "",
                       "refresh_token": "", "relay_url": "", "kind": kind}
            email = address
        if not email:
            return
        try:
            # 同一个导入邮箱反复失败 → 把它标废，别再自动重试。
            # 非池化来源每次都是新地址，"同一个号失败"无从谈起，由 _block_reason()
            # 的全局失败闸门兜（近 1 小时失败 N 次就整体暂停）。
            if pooled and self._recent_failures(email) >= FAILURE_LIMIT:
                reason = f"连续注册失败（{FAILURE_LIMIT} 次/小时），已暂停自动补号，需人工检查"
                logger.error("[pool] %s %s", email, reason)
                self.repo.mark_failed(email, reason)
                return
            # 注册维度：注册锁冻掉的代理不影响它自己的激活维度，反之亦然。
            #
            # ⚠️ 这里**不能**用 allow_shared=False（严格 1:1 独占）。实测（2026-10-11）：
            #    「被占用」= 只要号还在池里（registered 等待兑换 / in_use 在途）就算，
            #    于是 7 条代理被 8 个池内号持有 → 全部判定占用 → 补号永久停摆
            #    （日志每分钟刷「本轮不补号：代理池里没有空闲的出口 IP」）。
            #    而且严格 1:1 与「池目标 20 个号」数学上冲突：N 条代理最多养 N 个号。
            # 用默认的 least-used 轮换：账号自然分散到不同代理，同时不会把池子锁死。
            picked = self.service.proxy.pick(purpose="register")
            if not picked:
                logger.info("[pool] 本轮不补号：代理池里没有可用代理"
                            "（可能都已不可用/冻结，或被用途锁挡住）")
                if pooled:
                    # 池化来源：号本来就存在，放回 available 等下一轮再 claim。
                    self.repo.release_unused(email)
                else:
                    # 非池化来源（cf_temp 这类）的地址是**现造**的：放回 available 没有
                    # 意义（没有真实邮箱可复用），但**绝不能就这样留在 in_use**。
                    # 实测（2026-10-11）：一次「拿不到代理」期间号池每分钟开一轮、每轮先
                    # 造地址再失败，10 分钟就攒出 24 行 in_use 僵尸 —— 邮箱管理页看着
                    # 一团乱，而且这些行不再对应任何真实账号。这里直接标失败回收。
                    self.repo.mark_failed(email, "代理不可用：没有可用代理，本次未开始注册")
                return
            proxy = str(picked.get("url") or "")
            options = {
                "want_access_token": True,
                "want_session_token": True,
                # 账号工厂到 2FA 就停：不换 Codex RT（授权发生在用户点开始之后）。
                "want_refresh_token": False,
                # 池账号恒绑 2FA —— 池的定义，不受 invite_want_2fa 影响。
                "want_2fa": True,
                "pool_only": True,
                # 补号来源与兑换侧独立：显式传给 _do_register，别让它去读全局
                # mail_source（否则会拿错 provider）。
                "mail_source": kind,
                "proxy": proxy,
                "otp_timeout": self.otp_timeout(),
                "allow_existing_login": True,
            }
            logger.info("[pool] 开始补号 %s（proxy=%s）", email, proxy)
            # kind 用 register_pool：运行记录里要和人工「单次注册」分开，
            # 否则开了自动补号之后一排「注册」分不清哪条是自动跑的。
            run_id, log_file, _queue, handler = registrar.begin_run(email, kind="register_pool")
            try:
                # _do_register 已被 TaskGate 包裹（service.py 里 wrap 的模块函数），
                # 因此并发/邮箱互斥/超时不确定性的回收全部沿用既有边界。
                registrar._do_register(run_id, account, options, log_file, handler)
            finally:
                # 正常路径 _do_register 已经 end_run 过；这里兜住"被门卫挡回、
                # 原函数根本没跑"的情况，避免 handler 永远挂在 root logger 上。
                registrar.end_run(run_id, handler)
                registrar.remove_run_queue(run_id)
                self._finish_orphan_run(run_id)
            # 代理 409 生命周期必须在这里记账：注册是拆分后**唯一**还会做
            # warmup/「必定 409」预检的链路，不记就会反复挑中同一个坏代理。
            # 规则沿用既有实现：连续 2 次 409 → 冻结 20 分钟；解冻后再来一次
            # → 永久不可用；中途成功（warmup 通过）清零连续计数。
            self._settle_proxy(proxy, run_id)
            self._settle_address(email, run_id)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[pool] 补号 %s 异常", email)

    def _create_address(self, kind: str):
        """非池化来源：现造一个地址并占位成 in_use（失败返回 (None, 原因)）。

        占位的意义：注册期间这个地址属于我们，不能被别的流程重复使用；
        注册成功后由 _do_register 转成 registered 进池，失败则由 _settle_address 标失败。
        """
        try:
            from mail_providers import create_mail_provider
            provider = create_mail_provider(kind, self.repo.get_mail_settings())
            address = str(provider.create_mailbox() or "").strip().lower()
            if not address:
                return None, f"来源 {kind} 没有返回地址"
            if not self.repo.ensure_pool_account(address, kind, status="in_use"):
                return None, f"地址 {address} 已被占用，无法登记"
            return address, ""
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"

    def _settle_address(self, email: str, run_id: str) -> None:
        """非池化来源：注册没成功就把这个现造地址标成失败，别留在 in_use 里。"""
        if self.pooled_source():
            return
        try:
            row = self.repo.get_run(run_id) or {}
            # 只在**明确失败**时回收：读到空行或其它状态一律不动账号
            # （读不到不是"失败"，绝不能因此把刚造好的地址判死）。
            if str(row.get("status") or "") != "failed":
                return
            account = self.repo.get_account(email) or {}
            if str(account.get("status") or "") in ("registered", "done"):
                return
            self.repo.mark_failed(email, (str(row.get("error") or "") or "注册未完成")[:200])
        except Exception as e:  # noqa: BLE001
            logger.warning("[pool] 失败地址回写失败: %s", e)

    def _finish_orphan_run(self, run_id: str) -> None:
        """任务没被放行（门卫超时/闭锁）时把 run 收尾，别留下永远 running 的记录。"""
        try:
            row = self.repo.get_run(run_id) or {}
            if str(row.get("status") or "") == "running":
                self.repo.finish_run(run_id, "failed", "任务未启动（无空闲工位或执行器正在停止）",
                                     category="skipped")
        except Exception as e:  # noqa: BLE001
            logger.warning("[pool] 收尾未启动的 run 失败: %s", e)

    def _count_phone_bound(self, bound: bool) -> int:
        """已接码 / 未接码（含失败）的池内账号数；拿不到就返回 0。"""
        try:
            return int(self.repo.count_phone_bound("", bound) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _free_slots(self):
        """补号族还剩几个空工位；拿不到（桩/未接线）时返回 None 表示不限制。"""
        try:
            permits = getattr(getattr(self.service, "gate", None), "permits", None)
            if permits is None:
                return None
            from .permits import POOL_SLOTS
            return int(permits.free_slots(POOL_SLOTS))
        except Exception:  # noqa: BLE001
            return None

    def _settle_proxy(self, proxy: str, run_id: str) -> None:
        """按本次注册的结果给代理记账（成功清零 / 409 生命周期 / 普通失败）。"""
        try:
            run = self.repo.get_run(run_id) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("[pool] 读运行记录失败，代理未记账: %s", e)
            return
        status = str(run.get("status") or "")
        error = str(run.get("error") or "")
        try:
            if status == "done":
                self.service.proxy.record_success(proxy, "register")
                return
            if status == "failed" and error and self.service.proxy.is_transport_error(error):
                # 代理自身不可用（连不上/隧道被拒）→ 记健康维度，pick 会跳过它
                action = self.service.proxy.record_transport_failure(proxy)
                logger.warning("[pool] 代理不可用记账 proxy=%s action=%s",
                               proxy, (action or {}).get("action"))
                return
            if status == "failed" and error and self.service.proxy.is_409_error(error):
                action = self.service.proxy.record_409(proxy, "register")
                logger.warning("[pool] 注册 409 记账 proxy=%s action=%s",
                               proxy, (action or {}).get("action"))
                return
            self.service.proxy.record_fail(proxy, error or status, "register")
        except Exception as e:  # noqa: BLE001
            logger.warning("[pool] 代理记账失败（不影响本次注册结果）: %s", e)

    def _recent_failures(self, email: str) -> int:
        """近 1 小时失败次数。email 为空时统计**全部**补号失败（全局闸门）。

        人工单次注册与自动补号都算：同一个号反复失败都得停下来让人看；
        非池化来源每次都是新地址，只能按整体失败率兜。
        """
        since = time.time() - FAILURE_WINDOW_SECONDS
        # ⚠️ 排除 interrupted：服务重启/被杀留下的"进程中断"不是业务失败，
        #    算进闸门会让重启后的补号被自己的清理结果误挡（实测踩到）。
        not_interrupted = ("AND (error_category IS NULL OR "
                           "error_category NOT IN ('interrupted','skipped'))")
        if email:
            row = self.service.db.one(
                "SELECT COUNT(*) AS n FROM invitation_v2_runs "
                "WHERE email=? AND kind IN ('register','register_pool') AND status='failed' "
                + not_interrupted + " AND finished_at>=?",
                email, since,
            )
        else:
            row = self.service.db.one(
                "SELECT COUNT(*) AS n FROM invitation_v2_runs "
                "WHERE kind='register_pool' AND status='failed' "
                + not_interrupted + " AND finished_at>=?",
                since,
            )
        return int((row or {}).get("n") or 0)

    # ──────────────────────── 状态 ────────────────────────
    def snapshot(self) -> dict:
        kind = self.mail_source()
        pooled = self.pooled_source()
        # 可兑换账号永远是实数（池化/非池化都走 number）。
        try:
            available = max(0, int(self.alloc.pool_available() or 0))
        except (TypeError, ValueError):
            available = 0
        return {
            "enabled": self.enabled(),
            "running": self.alive(),
            "target": self.minimum(),
            "available": available,
            # 补号来源与兑换来源各配各的：管理端要能看出补号用的是什么。
            "mailSource": kind,
            "pooled": pooled,
            "mailboxAvailable": int(self.repo.count_accounts("available", kind) or 0) if pooled else 0,
            # 「是否接码」：注册阶段顺手验手机的成果（池子里有多少号兑换时免 add-phone）
            "smsBound": self._count_phone_bound(True),
            "smsUnbound": self._count_phone_bound(False),
            "concurrency": self.concurrency(),
            "checkSeconds": self.interval(),
            "rounds": self._rounds,
            "lastError": self._last_error,
            # 当前为什么没在补号；空串表示条件齐备。管理端必须能直接看到，
            # 否则"打开了开关却没反应"只能靠翻容器日志才查得出来。
            "blockedReason": self._block_reason(),
        }

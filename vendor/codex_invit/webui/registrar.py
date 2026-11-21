"""注册 worker：调 auth_flow.run_register，并把日志/状态实时推到队列。

每个注册任务跑在独立线程；通过 `RunLogger` 把 `logging` 记录 + tail 状态推
到队列，前端用 SSE 实时收日志。
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]  # ???/
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from auth_flow import AuthFlow  # noqa: E402
from mail_providers import (  # noqa: E402
    MailProviderError,
    create_mail_provider,
    get_provider_class,
)
from sms_provider import PhoneCallbackController  # noqa: E402

from . import db  # noqa: E402

# run_id -> queue of log strings; sentinel = None 表示流结束
_run_queues: dict[str, queue.Queue] = {}
_lock = threading.Lock()

# 当前线程正在跑哪个 run。
# ⚠️ 为什么需要这个：QueueLogHandler 是挂在 **root logger** 上的，而 root logger
#    是进程全局的。auto_loop 并发时 N 个 run 各挂一个 handler，每条日志会被
#    广播进**所有** run 的文件和 SSE 流 —— 实测 2026-08-04 三 worker 并发，
#    一个号的记录同时出现在 3 个 .log 里，WebUI 上三个号的日志搅在一起，
#    而 "[4/10] 获取 Sentinel Token..." 这类行不带邮箱，根本分不清是谁的。
#
#    注册链路（auth_flow / mail_providers / sentinel）内部不开任何线程，
#    一个 run 的日志全在自己那条线程上产生，所以线程绑定就能干净切开。
_current_run = threading.local()

LOG_DIR = Path(os.environ["SMSINVIT_INVITATION_LOG_DIR"])
LOG_DIR.mkdir(parents=True, exist_ok=True)


class QueueLogHandler(logging.Handler):
    """把 logging 记录扔进 run queue + 写 log 文件。

    只收**本 run 线程**产生的日志，见 emit 里的过滤。
    """

    def __init__(self, run_id: str, log_file: Path):
        super().__init__()
        self.run_id = run_id
        self._fh = open(log_file, "a", encoding="utf-8")
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord):
        try:
            # emit 是在**打日志的那条线程**里同步跑的，所以这里读到的就是
            # 日志产生者的 run_id。别人 run 的日志直接丢掉。
            rid = getattr(_current_run, "run_id", None)
            if rid != self.run_id:
                return
            # rid is None = 不属于任何 run（webui 请求线程、启动期日志等）。
            # 这类照旧广播给所有 handler —— 宁可多收也不能丢，日志文件
            # 开头那句 "webui: [run] xxx -> email@..." 就是这么来的。
            from smsinvit.invitation.safety import redact
            msg = redact(self.format(record))
            self._fh.write(msg + "\n")
            self._fh.flush()
            q = _run_queues.get(self.run_id)
            if q is not None:
                q.put(msg)
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
        super().close()


def _bind_phone_best_effort(flow, email: str, provider=None) -> tuple[bool, str]:
    """注册阶段顺手把手机验掉（best-effort），返回 (是否成功, 失败原因)。

    为什么在注册时做：池子里的号只注册到 2FA，手机没验；等 CDK 兑换时才命中
    add-phone，就要现租号 + 等码（实测 ~28s）。趁会话还热先验掉，兑换就不用再走。
    **失败绝不影响注册结论** —— 绑上 2FA 就算 GPT 注册成功，只是兑换会到 add-phone。
    """
    log = logging.getLogger("registrar")
    try:
        if flow is None or getattr(flow, "_sms_callback", None) is None:
            return False, "接码未启用，兑换时会走 add-phone"
        # ① 先补一次**协议登录**：注册完成时那次 signup 会话已被服务端作废，
        #    直接调 add-phone/send 会拿到 409 "Your sign-in session is no longer
        #    valid"（实测 3 个号各烧 3 次租号全失败）。这里重新建立登录会话，
        #    **只登录、不做 Codex 授权**（授权是 codex 邀请阶段的事）。
        try:
            from .codex_auth import _rebuild_auth_session

            password = str(getattr(flow.result, "password", "") or "")
            _rebuild_auth_session(flow, provider, email, password)
        except Exception as e:  # noqa: BLE001
            log.warning(f"[register] 注册后协议登录未完成（继续尝试验手机）: "
                        f"{type(e).__name__}: {str(e)[:160]}")
        # 协议登录过程中若已命中 add-phone，接码 controller 会标记 completed
        ctrl = getattr(flow, "_sms_callback", None)
        if ctrl is not None and getattr(ctrl, "completed", False):
            log.info(f"[register] 注册阶段手机绑定成功（协议登录时已完成）: {email}")
            return True, ""
        # ② 显式验一次手机
        flow._handle_add_phone_verification(continue_url="")
        log.info(f"[register] 注册阶段手机绑定成功: {email}（兑换时不再需要 add-phone）")
        return True, ""
    except Exception as e:  # noqa: BLE001
        try:
            from smsinvit.invitation.safety import redact as _redact

            detail = _redact(str(e))[:200]
        except Exception:  # noqa: BLE001
            detail = str(e)[:200]
        reason = f"{type(e).__name__}: {detail}"
        log.warning(f"[register] 注册阶段手机绑定失败（不影响入池，兑换时会到 add-phone）: {reason}")
        return False, reason


def _install_run_stage(run_id: str) -> None:
    """把阶段发射器指向**运行记录**（runs.stage）。

    进度百分比由读侧（api_runs）用同一份阶梯换算，避免两处维护阶段表。
    清理在 end_run() 里统一做。
    """
    try:
        from smsinvit.invitation.runtime import STAGE_EMITTER

        # 补号运行记录只存阶段名（runs.stage）；阶段附带的金额字段（接码成本）
        # 由 sms_provider 的日志与 invitation_v2_sms_orders 持久化，不写进本表。
        STAGE_EMITTER.set(lambda name, **fields: db.set_run_stage(run_id, name))
    except Exception:  # noqa: BLE001
        logging.getLogger("registrar").debug("[register] 运行阶段发射器安装失败", exc_info=True)


def _run_stage(name: str) -> None:
    """推一个注册阶段（写 runs.stage）。没有作业上下文时静默跳过。"""
    try:
        from smsinvit.invitation.runtime import emit_task_stage

        emit_task_stage(name)
    except Exception:  # noqa: BLE001
        pass


def _clear_run_stage() -> None:
    """运行结束后清掉阶段发射器，别让它跟着线程活到下一个任务。"""
    try:
        from smsinvit.invitation.runtime import STAGE_EMITTER

        STAGE_EMITTER.set(None)
    except Exception:  # noqa: BLE001
        pass


def _emit_status(run_id: str, kind: str, payload: dict | str = ""):
    """前端约定：以 `__EVENT__:` 开头的行被解析成 JSON 状态事件。"""
    import json as _json
    q = _run_queues.get(run_id)
    if q is None:
        return
    body = payload if isinstance(payload, dict) else {"message": str(payload)}
    body["kind"] = kind
    q.put("__EVENT__:" + _json.dumps(body, ensure_ascii=False))


# 网络/环境层错误特征：命中任一就把号放回 available（号本身没问题，是环境炸了）
_NETWORK_ERROR_PATTERNS = [
    "tls", "ssl", "sslerror", "connection", "connect error", "timeout", "timed out",
    "proxy", "socks", "dns", "name resolution", "name or service",
    "cloudflare", "just a moment", "403 forbidden",
    "csrf token 获取失败", "csrf token 失败",
    "/sentinel/req", "sentinel /req", "sentinel quickjs",
    "check_proxy 失败", "网络预检查",
    "curl: (35)", "curl: (28)", "curl: (6)", "curl: (7)",
    "remote disconnected", "connection reset", "connection aborted",
    "max retries exceeded",
    "invalid_state",
]


def classify_error(err: str, mail_source: str = "") -> str:
    """分类错误：'network'（环境/代理问题，号无辜）/ 'account'（号本身有问题）/ 'unknown'。

    mail_source 用来问 provider 要不要豁免某些模式 —— 比如 iCloud 中转号
    本来就是买的老号，"已有账号"是正常流程不是失败（见
    MailProvider.accepts_existing_account）。留空则按最严格的规则判。
    """
    s = (err or "").lower()

    account_patterns = [
        "wrong_email_otp_code", "invalid_grant", "imap xoauth2",
        "outlook imap account unusable", "user is authenticated but not connected",
        "outlook refresh failed", "authentication failed", "authenticate failed",
        "outlook otp timeout", "registration_disallowed",
        "已有账号", "账号被", "refresh_token 失效",
    ]
    if mail_source:
        try:
            exempt = get_provider_class(mail_source).accepts_existing_account
        except MailProviderError:
            exempt = False  # 未知来源 —— 按默认最严格规则走
        # ⚠️ 用 if-in 而不是裸 remove()：上面的模式表将来被人改动/重排后，
        #    remove 抛的 ValueError 会跟 get_provider_class 的错混在同一个
        #    except 里被一起吞掉，豁免静默失效且没人看得出来。
        if exempt and "已有账号" in account_patterns:
            account_patterns.remove("已有账号")

    # 先匹配 account 特征（更具体），避免子串误命中（如 "outlook OTP timeout" 含 "timeout"）
    if any(p in s for p in account_patterns):
        return "account"
    if any(p in s for p in _NETWORK_ERROR_PATTERNS):
        return "network"
    return "unknown"


def _do_register(
    run_id: str,
    account: dict,
    options: dict,
    log_file: Path,
    handler: Optional[logging.Handler] = None,
):
    """实际注册任务。

    options:
        want_access_token: bool
        want_session_token: bool
        want_refresh_token: bool
        proxy: Optional[str]
        otp_timeout: int
        allow_existing_login: bool
        pool_only: bool  # 账号工厂模式：注册到 2FA 就停，入池等 CDK 兑换
    """
    # 先认领本线程，再挂 handler —— 顺序不能反：中间要是有日志产生，
    # 没打标记的话会被广播到其他并发 run 的日志里去。
    # codex_invit：这两件事统一在 begin_run() 里做，注册与 codex_auth 作业共用。
    email = account["email"]
    # 让深层的 PoW / 接码里程碑也能写进**运行记录**（运行记录页据此显示进度）。
    # 以前只有卡密作业有阶段，账号工厂补号的进度永远是 0%，跑完直接跳 100%。
    _install_run_stage(run_id)
    _run_stage("registering")
    # 提前读取，避免在 try 块前异常时 except 引用未定义。
    # ⚠️ 优先用调用方显式指定的来源：账号工厂可以配一个和兑换侧**不同**的邮箱
    #    来源（pool_mail_source），硬读全局 mail_source 会拿错 provider ——
    #    既可能造到别人的域，也可能读不到这个号后续的验证码/邀请信。
    mail_source = str((options or {}).get("mail_source") or "").strip().lower() or db.get_setting("mail_source", "outlook")
    # 要不要操作号池（mark_done / mark_failed / release）由 provider 声明的
    # pooled 决定。未知 kind 时保守当池化处理 —— 号池里真有这行的话
    # 至少不会漏掉状态回写，把号永远卡在 in_use。
    try:
        is_pooled = get_provider_class(mail_source).pooled
    except MailProviderError:
        is_pooled = True

    try:
        # 本次注册专属的配置覆盖。
        # ⚠️ 以前是写 os.environ + finally 还原，但 auto_loop 并发跑多个 worker，
        #    os.environ 是**进程全局**的：A 设的 OTP_TIMEOUT/WEBUI_ALLOW_LOGIN 会被
        #    B 读到，B 跑完还原成 A 之前的值，A 后半程就用上别人的配置了。
        #    现在整个 dict 直接传给 AuthFlow，只挂在实例上，谁都污染不到谁。
        env_overrides = {}
        # outlook 接码邮箱常被 OpenAI 走 passwordless_signup 流程（新号收码而非设密码），
        # auth_flow 会误判为"已有账号"分支 → 不设 WEBUI_ALLOW_LOGIN 会 fast-fail。
        # 单号 WebUI 场景下 fast-fail 没意义（批量跑才需要"跳过被识别的号"），故强制 ON。
        env_overrides["WEBUI_ALLOW_LOGIN"] = "1"
        env_overrides["OTP_TIMEOUT"] = str(int(options.get("otp_timeout") or 180))
        # 用户不要 refresh_token → 直接跳过 Codex OAuth（每次都失败浪费 ~10s + 一堆告警）
        if not options.get("want_refresh_token", True):
            env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
            env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
        # PROXY 走 cfg.proxy，无需 env

        cfg = Config()
        cfg.proxy = (options.get("proxy") or "").strip() or None

        # ─ 邮箱来源路由 ─
        # 原来是 if cf_temp / else outlook 的写死分支，加一种邮箱就得回来改。
        # 现在交给注册表工厂：provider 自己从 settings + account 里取需要的字段。
        mail = create_mail_provider(mail_source, db.get_mail_settings(), account)
        # 账号工厂会**先**把地址造好再建运行记录（否则运行记录里只能写占位地址）。
        # 这里必须钉住它，否则 run_register 里的 create_mailbox() 会再新造一个，
        # 注册用的号和我们记账的号就不是同一个了。
        if email and not email.endswith("@placeholder.local"):
            try:
                mail.pin_mailbox(email)
            except Exception as e:  # noqa: BLE001
                logging.getLogger("registrar").warning(f"[register] 钉住已有地址失败: {e}")
        logging.getLogger("registrar").info(
            f"[register] 邮箱来源: {mail_source} ({mail.display_name})"
        )

        # ─ 2FA 绑定钩子：插在「拿到 session」和「Codex 授权」之间 ─
        #   主人指定的顺序：注册完 → 绑 2FA → Codex 授权 → 接码。
        #   2FA 必须有 access_token 才能打 mfa/enroll，而 at 只能从 get_auth_session 拿，
        #   所以这是唯一「已有 at 且 Codex 还没跑」的位置（见 auth_flow.py 那处注释）。
        #   钩子里绑成了就把结果存进 _tfa_box，run_register 返回后直接取，不再重绑。
        _tfa_box: dict = {}

        def _bind_2fa_hook(_flow, at: str) -> None:
            if not (getattr(_flow.result, "password", "") or "").strip():
                logging.getLogger("registrar").warning(
                    "[register] 勾了 2FA 但该号无密码，跳过绑定"
                )
                return
            from .two_factor import bind_totp_2fa_inline
            info = bind_totp_2fa_inline(_flow, at)
            if info and info.get("secret"):
                _tfa_box.update(info)

        def _account_callback_for_flow(email: str) -> dict:
            """从数据库加载账号凭证（密码和 totp_secret）供 AuthFlow 登录时使用。

            用于既有账号登录场景：当服务端返回 mfa-challenge 时，AuthFlow 需要
            totp_secret 来计算 6 位动态码完成 2FA 验证。
            """
            try:
                data = db.get_registered(email)
                if data:
                    return {
                        "password": data.get("password", ""),
                        "totp_secret": data.get("totp_secret", ""),
                    }
            except Exception as e:
                logging.getLogger("registrar").warning(f"[register] account_callback 异常: {e}")
            return {}

        flow = AuthFlow(
            cfg,
            sms_callback=_build_sms_callback(run_id),
            env_overrides=env_overrides,
            on_password=_save_password_early,
            on_session_ready=_bind_2fa_hook if options.get("want_2fa") else None,
            account_callback=_account_callback_for_flow,
        )
        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        logging.getLogger("registrar").info(f"[register] 开始: {email}")

        partial = False
        d: dict
        try:
            result = flow.run_register(mail)
            d = result.to_dict()
        except RuntimeError as e:
            # 部分凭证也算成功（OTP 验证通过 + create_account 成功 → flow.result 有 token）
            d = flow.result.to_dict()
            need_access = options.get("want_access_token", True)
            need_session = options.get("want_session_token", True)
            need_refresh = options.get("want_refresh_token", True)
            # 用户勾选的凭证全拿到 → 算正常完成（不视为 partial）
            wanted_ok = (
                (not need_access or d.get("access_token"))
                and (not need_session or d.get("session_token"))
                and (not need_refresh or d.get("refresh_token"))
            )
            has_any = bool(
                d.get("access_token") or d.get("refresh_token") or d.get("session_token")
            )
            if wanted_ok and has_any:
                logging.getLogger("registrar").warning(
                    f"[register] 流程末段异常但用户勾选的凭证已齐: {e}"
                )
            elif has_any:
                partial = True
                logging.getLogger("registrar").warning(
                    f"[register] 部分凭证 (缺用户勾选的某项): {e}"
                )
            else:
                raise

        # ─ 用户选项过滤：未勾选的字段从结果里抹掉，DB 只存用户想要的
        full = d
        d = {
            "email": full.get("email", ""),
            "password": full.get("password", ""),
            # 实际注册出口属于操作记录，不受用户选择哪些令牌的过滤影响。
            "proxy": str(getattr(cfg, "proxy", "") or "").strip(),
            "used_proxy": 1 if getattr(cfg, "proxy", "") else 0,
        }
        if options.get("want_access_token", True):
            d["access_token"] = full.get("access_token", "")
        if options.get("want_session_token", True):
            d["session_token"] = full.get("session_token", "")
            d["cookie_header"] = full.get("cookie_header", "")  # 同样是浏览器注入用
        if options.get("want_refresh_token", True):
            d["refresh_token"] = full.get("refresh_token", "")
            d["id_token"] = full.get("id_token", "")

        # 账号工厂（pool_only）：多留两样东西给后面的执行链路用。
        #   device_id —— codex_auth._authorize_bound_account 要拿它"零登录"恢复
        #                chatgpt.com 会话；常规注册的选项过滤会把它丢掉。
        #   source    —— 「注册结果」页区分哪些号是给 CDK 池养的。
        pool_only = bool(options.get("pool_only"))
        if pool_only:
            d["device_id"] = full.get("device_id", "")
            d["source"] = "pool"

        # ─ 可选：绑定 TOTP 2FA（仅用户勾选 want_2fa 时才跑） ─
        #   正常情况上面的 on_session_ready 钩子已经在【Codex 授权之前】绑完了，
        #   这里只是兜底：钩子没跑到（run_register 中途抛异常走 partial 分支、
        #   或那时 access_token 还是空）时再补一次。
        #   兜底本身也是先快后慢两条路（见 two_factor.py 模块头）：
        #     快 bind_totp_2fa_inline —— 直接复用刚跑完注册的 flow + access_token，
        #        6.2s 搞定，零 PoW 零邮件（实测 2026-08-08 <测试号>@<自建域>
        #        四个请求全 200，mfa_enabled=true）。
        #     慢 bind_totp_2fa —— 新起 AuthFlow 重走 login 正式链，约 40s + 一次 PoW
        #        + 一封验证码邮件。只在快路径没成时兜底。
        #   失败仅告警、绝不废掉已注册成功的号；secret 一次性下发，成功即随 d 落库+推前端。
        if options.get("want_2fa") and (d.get("password") or "").strip():
            _emit_status(run_id, "phase", {"phase": "binding_2fa", "email": d.get("email")})
            try:
                from .two_factor import bind_totp_2fa, bind_totp_2fa_inline
                # 钩子（Codex 授权之前那次）已经绑好就直接用，别再打一遍 enroll
                tinfo = dict(_tfa_box) if _tfa_box.get("secret") else None
                if not tinfo:
                    tinfo = bind_totp_2fa_inline(flow, full.get("access_token", ""))
                if not (tinfo and tinfo.get("secret")):
                    logging.getLogger("registrar").info(
                        "[register] 2FA 快路径未成，回落重走登录链..."
                    )
                    tinfo = bind_totp_2fa(
                        cfg, d.get("email", ""), d.get("password", ""),
                        mail_provider=mail, env_overrides=env_overrides,
                    )
                if tinfo and tinfo.get("secret"):
                    d["totp_secret"] = tinfo["secret"]
                    d["totp_factor_id"] = tinfo.get("factor_id", "")
                    logging.getLogger("registrar").info(
                        f"[register] 2FA 绑定成功 email={d.get('email')}"
                    )
                    _emit_status(run_id, "phase", {"phase": "2fa_bound", "email": d.get("email")})
                else:
                    logging.getLogger("registrar").warning(
                        "[register] 2FA 绑定未成功（账号仍有效，仅未绑 2FA）"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(
                    f"[register] 2FA 绑定异常（账号仍有效）: {e}"
                )
        elif options.get("want_2fa"):
            logging.getLogger("registrar").warning(
                "[register] 勾选了 2FA 但该号无密码，跳过绑定"
            )

        # 会话罐：把整个 cookie 罐（含 auth.openai.com 会话）随凭证一起存下来。
        # 兑换阶段重放它，Codex authorize 才有可能直接 SSO（省掉整轮登录）。
        try:
            from .codex_auth import capture_cookie_jar

            _jar = capture_cookie_jar(flow)
            if _jar:
                d["cookie_jar"] = _jar
                d["cookie_jar_at"] = time.time()   # 罐年龄诊断（不做限制，能多大就多大）
        except Exception as e:  # noqa: BLE001
            logging.getLogger("registrar").debug(f"[register] 会话罐抓取失败: {e}")

        # 落库
        db.save_registered(d)
        # ⚠️ d 是**本轮内存里**的结果，它不一定知道这个号有密码：
        #    重跑一个之前设过密码的邮箱时，OpenAI 会认成已有账号 → passwordless_login
        #    → register_password 根本不执行 → d["password"] 是空的。
        #    但上一轮 save_password_early 存的密码还在库里，save_registered 刚刚
        #    已经把它保留下来了（空值不覆盖非空旧值）——「注册结果」页读 DB 显示正确，
        #    唯独跑完这一刻的 done 事件拿的是 d，前端 `v-if="lastRunResult.password"`
        #    判空 → 密码行连同「复制密码」「复制 email----password」两个按钮一起消失，
        #    主人会以为密码又丢了。所以这里从库里回读补回来。
        #    只在 d 里密码为空时查一次，正常路径零额外开销。
        if not (d.get("password") or "").strip():
            try:
                _saved = db.get_registered(d.get("email") or "")
                _pw = ((_saved or {}).get("password") or "").strip()
                if _pw:
                    d["password"] = _pw
                    logging.getLogger("registrar").info(
                        "[register] 本轮未设密码，沿用库中已存密码（上一轮 register_password 留下的）"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(f"[register] 回读已存密码失败: {e}")
        # 账号工厂的硬门槛：没有 2FA secret 的号不进池。
        # 常规注册允许"2FA 绑定失败但账号仍有效"；池不行 —— 池的消费方只认
        # status='registered'，放一个没 2FA 的号进去，后面授权失败/找不回账号
        # 都无从判断。这里按失败处理，并把原号退回可重试状态由人工决定。
        if pool_only:
            _secret = (d.get("totp_secret") or "").strip()
            if not _secret:
                try:
                    _secret = str((db.get_registered(d.get("email") or "") or {}).get("totp_secret") or "").strip()
                except Exception as e:  # noqa: BLE001
                    logging.getLogger("registrar").warning(f"[register] 池模式回读 2FA 失败: {e}")
            if not _secret:
                reason = "池注册未获得 2FA secret，未入池"
                logging.getLogger("registrar").error(f"[register] {email} {reason}")
                if is_pooled:
                    db.mark_failed(email, reason)
                db.finish_run(run_id, "failed", reason, category="account")
                _emit_status(run_id, "error", {"message": reason, "category": "account"})
                return

        # 非池化 provider（CF 临时邮箱这类）的 email 是虚拟占位
        # （xxx_placeholder_N@placeholder.local），号池里根本没这行，不能去 mark。
        # 但账号工厂模式下必须把**注册时现造的真实地址**补登记进号池，否则
        # 这个号谁都兑换不到。判据用 provider 的 pooled，不写死 kind。
        real_email = (d.get("email") or "").strip().lower()
        # 本次**实际使用**的代理：运行记录与账号行都要能看到，
        # 否则注册结果列表里会显示成"直连"，看起来像没走代理。
        used_proxy = str(d.get("proxy") or (options or {}).get("proxy") or "").strip()
        _run_stage("registered")
        # 入池前顺手验手机：成功 → 兑换免 add-phone；失败也照常入池，只记状态。
        if pool_only:
            phone_ok, phone_error = _bind_phone_best_effort(flow, email, mail)
            try:
                db.mark_phone_bound(
                    real_email or email, phone_ok, phone_error,
                    phone_number=getattr(flow, "_phone_number", "") if phone_ok else "",
                    phone_code=getattr(flow, "_phone_code", "") if phone_ok else "",
                )
            except Exception as e:  # noqa: BLE001
                logging.getLogger("registrar").warning(f"[register] 接码状态回写失败: {e}")
        elif getattr(flow, "_phone_bound_ok", False):
            try:
                db.mark_phone_bound(
                    real_email or email, True,
                    phone_number=getattr(flow, "_phone_number", ""),
                    phone_code=getattr(flow, "_phone_code", ""),
                )
            except Exception as e:  # noqa: BLE001
                logging.getLogger("registrar").warning(f"[register] 接码状态回写失败: {e}")
        if is_pooled:
            if pool_only:
                db.mark_registered(email, proxy=used_proxy, used_proxy=bool(used_proxy))
            else:
                db.mark_done(email, proxy=used_proxy, used_proxy=bool(used_proxy))
        elif pool_only:
            # ⚠️ 以前判据是 `real_email != email.lower()`（"地址是注册时现造的"）。
            #    但账号工厂现在**预先造好真实地址**，两者相等 → 这个分支从不执行 →
            #    号永远停在 in_use、兑换不到。只要有真实地址就必须登记入池。
            if real_email:
                if db.ensure_pool_account(real_email, mail_source, proxy=used_proxy,
                                          used_proxy=bool(used_proxy)):
                    logging.getLogger("registrar").info(
                        f"[register] 现造地址已入池: {real_email}（来源={mail_source}）"
                    )
                    # 运行记录挂在占位地址上没法核对，改写成真实地址
                    # （工厂预造地址时两者相同，这里是 no-op，保留兜底）。
                    try:
                        db.retarget_run(run_id, real_email)
                    except Exception as e:  # noqa: BLE001
                        logging.getLogger("registrar").warning(f"[register] 运行记录改址失败: {e}")
                else:
                    logging.getLogger("registrar").error(
                        f"[register] 现造地址入池被拒绝（已绑定或已完成）: {real_email}"
                    )

        # ─ 可选：导出到 CPA / SUB2API 面板（仅勾选启用时才执行） ─
        # 池里的号还没做 Codex 授权，绝不能提前推到面板 —— 导出发生在邀请完成之后。
        if not pool_only:
            _try_export_to_panels(run_id, d)

        result_summary = {
            "email": d.get("email"),
            # 密码走明文推给前端：token 只给长度是因为太长且必须点按钮复制，
            # 但密码是随机 16 位、用户注册完第一件事就是拿去登录，
            # 藏在「查看凭证」弹窗里等于每次都要多点两下。
            # 这是本机自用工具，SSE 只发给本地浏览器，不外传。
            "password": d.get("password") or "",
            "access_token_len": len(d.get("access_token") or ""),
            "session_token_len": len(d.get("session_token") or ""),
            "refresh_token_len": len(d.get("refresh_token") or ""),
            # 2FA secret 一次性下发、服务端取不回，明文推前端让用户当场导入验证器
            # （理由同密码；本机自用工具，SSE 只发本地浏览器）。未绑则为空串。
            "totp_secret": d.get("totp_secret") or "",
            "partial": partial,
        }
        _emit_status(run_id, "done", result_summary)
        logging.getLogger("registrar").info(
            f"[register] 完成 email={d.get('email')} "
            f"pw={d.get('password') or '(无)'} "
            f"at={result_summary['access_token_len']} "
            f"st={result_summary['session_token_len']} "
            f"rt={result_summary['refresh_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err, mail_source)
        logging.getLogger("registrar").error(f"[register] 失败 (category={category}): {err}")
        # ⚠️ 密码是在 register_password 里现生成的，只活在内存里。
        #    走到这里说明 save_registered 没执行过 —— 但 POST user/register 可能**已经成功**，
        #    OpenAI 那边账号连同这个密码已经建好了，只是后续步骤（发码/验证/建账户）挂了。
        #    不打出来的话这个号就成了谁也登不进去的孤儿。这里只写日志不落库，
        #    避免把没有任何 token 的半成品塞进「注册结果」表里。
        try:
            _pw = (flow.result.password or "").strip()
            if _pw:
                logging.getLogger("registrar").error(
                    f"[register] 该号已生成密码，请自行留存: {flow.result.email or email} / {_pw}"
                )
        except Exception:
            pass  # flow 还没建出来（异常发生在 AuthFlow 之前），没密码可救
        if category != "account":
            logging.getLogger("registrar").error(traceback.format_exc())
        # 非池化 provider 没有号池记录，不操作
        if is_pooled:
            if category == "network":
                db.release_unused(email)
                logging.getLogger("registrar").warning(
                    f"[register] {email} 判定为网络/环境错误，号已 release 回 available"
                )
            else:
                db.mark_failed(email, f"[{category}] {err}")
        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(run_id, "error", {"message": err, "category": category})

    finally:
        # env 覆盖现在只挂在 AuthFlow 实例上，随实例一起回收，无需还原。
        end_run(run_id, handler)


def _try_export_to_panels(run_id: str, cred: dict) -> None:
    """注册完成后可选地把凭证导出到 CPA / SUB2API 面板。

    - 任一目标的"启用"开关关闭时,该目标跳过(不发请求);两者都未启用时整段 no-op。
    - 任何异常都不抛,只 emit 日志/状态(不影响注册主流程)。
    """
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:
        logging.getLogger("registrar").warning(f"[export] 读取配置失败: {e}")
        return

    cpa_enabled = bool(cfg.get("cpa", {}).get("enabled"))
    sub2api_enabled = bool(cfg.get("sub2api", {}).get("enabled"))
    if not (cpa_enabled or sub2api_enabled):
        return  # 用户没勾选任何目标 → 完全不执行

    from . import exporter  # 懒 import,避免未启用时强依赖

    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
        except Exception:
            pass

    try:
        results = exporter.run_exports(
            cred,
            cpa_cfg=cfg.get("cpa") if cpa_enabled else None,
            sub2api_cfg=cfg.get("sub2api") if sub2api_enabled else None,
            log_fn=_log,
        )
    except Exception as e:
        _log(f"导出整体异常: {e}", "error")
        return

    # 汇总成一个事件给前端
    summary = {}
    if results.get("cpa") is not None:
        summary["cpa"] = {"ok": bool(results["cpa"].get("ok")),
                          "message": results["cpa"].get("message") or results["cpa"].get("error") or ""}
    if results.get("sub2api") is not None:
        summary["sub2api"] = {"ok": bool(results["sub2api"].get("ok")),
                              "message": results["sub2api"].get("message") or results["sub2api"].get("error") or ""}
    try:
        _emit_status(run_id, "phase", {"phase": "export_done", "summary": summary})
    except Exception:
        pass


def _save_password_early(email: str, password: str) -> None:
    """AuthFlow 的 on_password 回调：密码在 OpenAI 侧一生效就落盘。

    以前密码只在流程**全部**跑通后才随 save_registered 一起入库，
    中间任何一步失败（实测最常见的是 OTP 超时）密码就只剩一行 ERROR 日志兜底 ——
    换台机器、日志轮转、或者干脆没人去翻，号就废了。

    这里存的是"有密码、无凭证"的半成品行，跑通后 save_registered 会用
    同一个 email 主键覆盖补全，不会多出一行对不上的记录。
    """
    log = logging.getLogger("registrar")
    try:
        db.save_password_early(email, password)
        log.info(f"[register] 密码已落盘: {email}（凭证待补）")
    except Exception as e:
        # 落盘失败不能影响注册；下面 except 里那行 ERROR 日志仍然是兜底
        log.warning(f"[register] 密码落盘失败，仅剩日志兜底: {e}")


def _build_sms_callback(run_id: str, cfg: Optional[dict] = None) -> Optional[PhoneCallbackController]:
    """根据 webui 配置创建 SMS 接码 controller。

    未启用接码时返回 None；启用后缺少配置必须明确失败。
    log_fn 把租号/等码的状态推到 SSE 流，前端可见。

    codex_invit：把「5s 价格任务算出的最优」和成功率统计一起注入 ——
    命中 SMS 时用的平台/国家/价格/前缀规则，就是接码价格页上显示的那一套。
    """
    cfg = cfg or db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        return None

    from .sms_prices import MONITOR, selected_platforms, platform_key_configured

    if not any(platform_key_configured(cfg, pk) for pk in selected_platforms(cfg)):
        raise RuntimeError("已启用接码，但所选平台均未配置 API Key")

    smslog = logging.getLogger("registrar")

    def _log(msg: str) -> None:
        # 既写日志、又通过 _emit_status 推 phase 事件给前端
        smslog.info(f"[sms] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "sms", "message": msg})
        except Exception:
            pass

    try:
        eff = MONITOR.effective_params(cfg)
        platforms = [str(p) for p in (eff.get("platform_candidates") or []) if str(p).strip()]
        return PhoneCallbackController(
            provider_key=platforms[0] if platforms else (cfg.get("sms_provider") or "smsbower"),
            config=cfg,
            service=cfg.get("sms_service") or "dr",
            country=cfg.get("sms_country") or "",
            log_fn=_log,
            auto_select_country=bool(cfg.get("sms_auto_country")),
            platform_candidates=platforms,
            country_candidates=eff.get("country_candidates") or [],
            effective_params_fn=MONITOR.effective_params,
            stats_fn=db.sms_stats_bump,
            blocked_prefixes=eff.get("blocked_prefixes") or [],
        )
    except Exception as e:
        # ⚠️ 这里以前是 warning：结果接码 controller 创建失败后**静默继续**跑注册，
        #    需要手机号时才发现没有接码 —— 排查起来像见鬼。
        #    接码是"启用了就必须有"的东西，失败要响、要带堆栈。
        smslog.error("[sms] 创建接码 controller 失败，本次注册将没有接码能力: %s", e,
                     exc_info=True)
        try:
            _emit_status(run_id, "phase",
                         {"phase": "sms", "message": f"接码初始化失败: {e}"})
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("SMS 接码初始化失败，停止使用环境号码兜底") from e


def pre_register(run_id: str) -> queue.Queue:
    """先建好队列，让前端能立刻订阅 SSE。

    日志 handler 要等工作线程里 `begin_run` 才挂 —— 但队列提前存在，
    `/api/runs/{id}/stream` 就不会因为「线程还没起来」而 404。
    """
    q: queue.Queue = queue.Queue()
    with _lock:
        _run_queues[run_id] = q
    return q


def begin_run(email: str, kind: str = "register", run_id: str = "",
              existing_queue: Optional[queue.Queue] = None):
    """建一条 runs 记录 + 挂好本线程的日志 handler/队列。

    「单次注册」与「CDK 邀请作业」「手动 Codex 授权」共用这一套：运行记录页能
    统一看到各类运行（runs.kind = register / codex_auth），SSE 日志流也走同一条管道。

    返回 (run_id, log_file, queue, handler)。
    """
    rid = run_id or uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{rid}.log"
    db.create_run(rid, email, str(log_file), kind=kind)

    q = existing_queue if existing_queue is not None else queue.Queue()
    with _lock:
        _run_queues[rid] = q

    # 先认领本线程，再挂 handler —— 顺序不能反，否则中间产生的日志会被
    # 广播进其他并发 run 的日志/SSE 流里（见 _current_run 的注释）。
    _current_run.run_id = rid
    handler = QueueLogHandler(rid, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)
    return rid, log_file, q, handler


def end_run(run_id: str, handler: Optional[logging.Handler] = None) -> None:
    """收尾：摘 handler、给 SSE 发 sentinel、清线程标记。"""
    try:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
    except Exception:
        pass
    q = _run_queues.get(run_id)
    if q is not None:
        q.put(None)  # sentinel: 流结束
    # 线程标记清掉：threading.local 绑在线程对象上，换成线程池复用线程时
    # 残留的 run_id 会把下一个任务的日志投进上一个 run 的文件里。
    try:
        _current_run.run_id = None
    except Exception:
        pass
    _clear_run_stage()


def start_registration(account: dict, options: dict) -> str:
    """启动一次注册任务，返回 run_id。"""
    run_id, log_file, _q, handler = begin_run(account["email"], kind="register")
    th = threading.Thread(
        target=_do_register,
        args=(run_id, account, options, log_file, handler),
        daemon=True,
        name=f"register-{run_id}",
    )
    th.start()
    return run_id


def get_run_queue(run_id: str) -> Optional[queue.Queue]:
    return _run_queues.get(run_id)


def remove_run_queue(run_id: str) -> None:
    with _lock:
        _run_queues.pop(run_id, None)

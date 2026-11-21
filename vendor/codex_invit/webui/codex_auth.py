"""CDK 邀请作业：账号池取号后的「登录 + 授权」，注册已经拆到账号工厂。

顺序（主人定的，别改）：

    等邀请信(150s) → 选中一个代理 → 恢复池中会话（零登录）
    → 会话失效才补一次纯协议登录 → 接受邀请
    → Codex 授权直连换 RT → Codex CLI 跑一个真实回合(sayhi) → 导出 SUB2API

与拆分前的区别（这次改动的核心）：

  · **注册不再发生在这里**。号由 smsinvit.invitation.pool 的账号工厂先注册到
    2FA 并停在 `outlook_accounts.status='registered'`，用户拿 CDK 兑换时才把
    邮箱绑到卡密上。所以这里只认"已注册的号"，没有密码就直接失败。
  · **授权阶段不做 warmup / 「必定 409」预检查**，也不按 409 冻结代理：
    失败只记 fail，换下一个代理重试。注册那边的 409 预检保持不变。
  · **不经过 chatgpt.com 的 CSRF / signin / OAuth 初始化链**：直接用池里存的
    session_token / access_token / device_id 恢复会话（见
    `_authorize_bound_account`）。会话真的失效时，才补一次同样不含这些步骤的
    纯协议登录（sentinel → authorize_continue → 密码 → TOTP → 邮件 OTP → 接码）。

三个关键约束：

  1. **同一个代理 IP**：一次尝试只构造一个 `AuthFlow`（一个 cfg.proxy），
     恢复会话、登录、接受邀请、Codex 授权、CLI 全程复用 —— 这正是原先
     「把两步合并」保留下来的性质。
  2. **按阶段重试**：授权前失败可换一个本次没用过的代理重跑；
     授权凭据已保存后，刷新令牌或 CLI 失败立即停止，人工重试只续 CLI。
     不因消息结果未知而重新登录、OAuth 或租号。CDK **永不作废**。
  3. **成功判据只看 CLI 回合**：SUB2API 导出失败不算失败（只记事件）。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth_flow import AuthFlow  # noqa: E402
from config import Config  # noqa: E402
from mail_providers import create_mail_provider, get_provider_class  # noqa: E402

from . import db, exporter, invite_mail, mail_alloc, proxy_pool, registrar  # noqa: E402
from . import codex_cli  # noqa: E402

logger = logging.getLogger("codex_auth")


# 内部阶段 → (对外文案, 进度%)。对外只暴露这 7 个词，绝不泄漏后端细节。
STAGES: dict[str, tuple[str, Optional[int]]] = {
    "queued":            ("正在排队", 0),
    "waiting_invite":    ("正在等待邀请信", 3),
    "invite_found":      ("收到邀请信", 5),
    "picking_proxy":     ("正在接受邀请", 8),
    "accepting_invite":  ("正在接受邀请", 10),
    # ── 主人指定的里程碑（管理员看得到细节；用户侧映射成脱敏文案）──
    "invite_accepted":   ("接受邀请成功", 30),
    "sentinel_pow":      ("获取 Sentinel Token (PoW)", 40),
    "sms_phone":         ("命中手机验证，正在短信接码", 45),
    "sms_code":          ("已收到验证码，正在验证", 55),
    "sms_settled":       ("业务验证成功，平台结算已确认", 60),
    "codex_authorizing": ("正在获取 Codex 授权", 70),
    "codex_oauth":       ("Codex 授权成功", 80),
    "cli_say_hi":        ("正在发送消息", 90),
    "export":            ("正在生成凭证", 95),
    "done":              ("完成", 100),
    "failed":            ("失败", None),
    # 注册链路（账号工厂补号 / 管理端单次注册）
    "registering":       ("正在注册账号", 20),
    "register_otp":      ("正在等待邮箱验证码", 50),
    "registered":        ("注册成功", 70),
}

USER_STAGE_LABEL = {k: v[0] for k, v in STAGES.items()}
USER_STAGE_PCT = {k: v[1] for k, v in STAGES.items()}


class JobFailure(RuntimeError):
    """一次尝试失败。is_409 决定要不要按 409 规则处理代理。

    ⚠️ CDK 作业（run_invite_job）的授权阶段已经**不再**使用 is_409 —— 那边的
    重试循环一律只记普通 fail。这个标记现在只对管理端手动授权
    （authorize_registered_email）生效。
    """

    def __init__(self, message: str, *, is_409: bool = False, retryable: bool = True):
        super().__init__(message)
        self.is_409 = bool(is_409)
        self.retryable = bool(retryable)


def _sms_retry_stopped(error: BaseException) -> bool:
    """A lost rental response or exhausted run budget must not rent via another IP."""
    from smsinvit.invitation.sms_state import RentalUncertainError, SmsBudgetExhausted
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (RentalUncertainError, SmsBudgetExhausted)):
            return True
        error = error.__cause__ or error.__context__
    return False


def _int_setting(key: str, default: int) -> int:
    try:
        return int(str(db.get_setting(key, str(default))).strip())
    except Exception:
        return default


# 当前线程正在跑的 cdk job_id。阶段进度要写进 cdk_jobs 给用户前台轮询，
# 但阶段是在很深的调用里发出的 —— 用线程局部变量传递，省掉一层层传参。
_CURRENT_JOB = threading.local()


def emit_stage(cb, name: str) -> None:
    """发一个（脱敏的）阶段：写库 + 通知调用方。"""
    pct = USER_STAGE_PCT.get(name)
    jid = getattr(_CURRENT_JOB, "job_id", "") or ""
    if jid:
        try:
            db.set_cdk_job(jid, stage=name, progress=(0 if pct is None else int(pct)))
        except Exception:  # noqa: BLE001
            pass
    if not cb:
        return
    try:
        cb(name, USER_STAGE_LABEL.get(name, name), pct)
    except Exception:  # noqa: BLE001
        pass


def _account_callback(email: str) -> dict:
    """给 AuthFlow 用：库里的密码 / TOTP secret（MFA 挑战要算码）。"""
    try:
        data = db.get_registered(email) or {}
        return {
            "password": data.get("password", "") or "",
            "totp_secret": data.get("totp_secret", "") or "",
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("account_callback 异常: %s", e)
        return {}


def _account_id_of(token: str) -> str:
    try:
        auth = exporter._get_auth(exporter._decode_jwt_payload(token))
        return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _save_rt_immediately(email: str, rt: str, proxy: str = "") -> None:
    """RT 一拿到就落库。

    ⚠️ 实测教训：OAuth 换到 RT 之后，下一步（刷新令牌/CLI）可能因为代理抖动失败，
    而 RT 是**一次性拿到就必须留住**的东西 —— 不立刻存，这一轮的授权就白做，
    下一轮还得重新走 add-phone（又要花钱租号）。所以拿到就写。
    """
    if not rt:
        return
    try:
        db.save_codex_auth(email, refresh_token=rt,
                           codex_authorized_at=time.time())
        # 同时回写 registered.refresh_token —— 与本项目一致，
        # 「注册结果」页的 refresh 列才看得到（它读的就是这一列）。
        db.set_registered_refresh_token(email, rt)
        logger.info("[invite] %s 已即时保存 refresh_token（len=%d，并回写 registered）", email, len(rt))
    except Exception as e:  # noqa: BLE001
        raise JobFailure("授权凭据保存失败，停止重新授权，请人工核查。", retryable=False) from e


def _saved_codex_creds(email: str, *, invite_link: str = "") -> Optional[dict]:
    """Resume only credentials belonging to this accepted invitation and account."""
    row = db.get_codex_auth(email) or {}
    if (str(row.get("email") or "").strip().lower() != email.strip().lower()
            or not row.get("invite_processed_at")
            or (invite_link and str(row.get("invite_link") or "").strip() != invite_link.strip())):
        return None
    creds = {key: str(row.get(key) or "").strip()
             for key in ("access_token", "refresh_token", "id_token", "account_id")}
    if (not all(creds[key] for key in ("access_token", "refresh_token", "account_id"))
            or _account_id_of(creds["access_token"]) != creds["account_id"]):
        return None
    profile = exporter._get_profile(exporter._decode_jwt_payload(creds["access_token"]))
    identity = exporter._decode_jwt_payload(creds["id_token"])
    if any(str(source.get("email") or "").strip().lower() not in ("", email.strip().lower())
           for source in (profile, identity)):
        return None
    return creds


_OPENAI_SESSION_COOKIES = (
    "__Secure-next-auth.session-token", "oai-client-auth-session", "login_session",
    "__Host-next-auth.csrf-token", "oai-did", "oai-sc",
)


def _iter_cookie_objects(flow):
    """产出 (name, value, domain, path)；**必须**优先用 `cookies.jar`。

    ⚠️ 坑：curl_cffi 的 `session.cookies` **迭代出来是字符串（cookie 名）**，
    真正的 cookie 对象在 `session.cookies.jar` 里。以前直接迭代 cookies，于是
    `getattr(cookie, "name")` 全是空 → 每个 cookie 都被跳过 → 会话罐永远是空的，
    而且**没有异常**，静默失败（这次就是这么被坑的）。
    """
    jar = getattr(getattr(flow, "session", None), "cookies", None)
    if jar is None:
        return []
    source = getattr(jar, "jar", None)
    if source is None:
        source = jar
    out = []
    for item in list(source):
        if isinstance(item, str):          # 退回到"只有名字"的情况
            value = ""
            try:
                value = str(jar.get(item) or "")
            except Exception:  # noqa: BLE001
                value = ""
            if item and value:
                out.append((item, value, "", "/"))
            continue
        name = str(getattr(item, "name", "") or "").strip()
        value = getattr(item, "value", "") or ""
        if not name or not value:
            continue
        out.append((name, value,
                    str(getattr(item, "domain", "") or ""),
                    str(getattr(item, "path", "") or "/")))
    return out


def capture_cookie_jar(flow) -> str:
    """把整个 cookie 罐序列化（含 auth.openai.com 域的会话 cookie）。

    为什么要存：拆成"注册到 2FA"+"兑换时再授权"之后，兑换阶段的 Codex authorize
    在 auth.openai.com 那一侧没有会话（`from_existing_credentials` 只放两个
    chatgpt.com 域的 cookie），所以每单都要重新登录（实测 SSO 0/3）。把登录成功时的
    整个罐存下来、下次重放，authorize 才有可能直接过（省掉整轮登录 ~27s）。
    存进 extra_json（该列已在 SECRET_FIELDS 里加密），不需要迁移。
    """
    try:
        cookies = [{"name": name, "value": value, "domain": domain, "path": path}
                   for name, value, domain, path in _iter_cookie_objects(flow)]
    except Exception as e:  # noqa: BLE001
        logger.warning("会话罐抓取异常（不写罐）: %s: %s", type(e).__name__, e)
        return ""
    if not cookies:
        # 静默为空就是上次的教训：这里必须留痕，否则"罐没存下来"查不出来。
        logger.warning("会话罐为空（session 里没有可保存的 cookie），本次不写罐")
        return ""
    logger.info("会话罐已抓取 %d 条 cookie（含 auth.openai.com 会话则兑换时可跳过登录）", len(cookies))
    return _json_dumps(cookies)


def _sso_jar_max_age_minutes() -> float:
    """会话罐还能"免登录"的最长年龄（分钟）。

    实测：注册后 2 分钟兑换 → SSO 直过；29 分钟兑换 → 被弹回 /log-in。
    超过这个窗口的罐**不是没用，而是有害** —— 旧的 auth.openai.com cookie 会让重新
    登录后的服务端状态错乱（workspace 选择拿不到 id），卡在 Codex 授权确认页。
    所以过窗口就只回放 chatgpt.com 的会话，auth.openai.com 的一律丢掉，让登录重新下发。
    """
    raw = None
    try:
        from smsinvit.invitation.runtime import TASK_SETTINGS

        raw = (TASK_SETTINGS.get() or {}).get("cookie_jar_sso_max_minutes")
    except Exception:  # noqa: BLE001
        raw = None
    try:
        value = float(str(raw).strip()) if raw not in (None, "") else 25.0
    except (TypeError, ValueError):
        value = 25.0
    return max(0.0, min(24 * 60.0, value))


def _sso_jar_is_fresh(captured_at, limit_minutes: float) -> bool:
    """罐"新不新鲜"只看"有记录时间 + 没超过免登录窗口"。

    ⚠️ **年龄未知必须当"不新鲜"**（2026-10-09 真账号实测）：
    号池工厂写罐的路径没有 `cookie_jar_at` → 年龄未知。以前把"未知"归到"新鲜"一侧，
    于是回放了 auth.openai.com 的 cookie，服务端状态被带偏 —— PoW+TOTP 都过，却卡在
    Codex 授权确认页，**三次重试全废**（连续 16 次尝试同一死法）。同一个号只要按
    "过期"处理（丢弃 auth.openai.com cookie、让登录重新下发），就能走通。
    """
    try:
        captured = float(captured_at or 0)
    except (TypeError, ValueError):
        captured = 0.0
    if captured <= 0:
        return False
    age_min = (time.time() - captured) / 60.0
    return 0.0 <= age_min <= float(limit_minutes)


def _replay_cookie_jar(flow, jar: str, *, include_auth_openai: bool = True) -> int:
    """把存下来的 cookie 罐放回 flow 的 session；返回放回条数。

    include_auth_openai=False 时**跳过 auth.openai.com 的 cookie**（罐已过期：
    旧的认证 cookie 会让重新登录后的状态错乱，反而导致卡在授权确认页）。
    """
    if not jar:
        return 0
    try:
        import json as _json

        cookies = _json.loads(jar)
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(cookies, list):
        return 0
    restored = 0
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name, value = str(item.get("name") or ""), str(item.get("value") or "")
        if not name or not value:
            continue
        if (not include_auth_openai) and "openai.com" in str(item.get("domain") or "").lower():
            continue                      # 过期罐：认证侧 cookie 一律不重放
        try:
            flow.session.cookies.set(name, value,
                                     domain=str(item.get("domain") or "") or None,
                                     path=str(item.get("path") or "/") or "/")
            restored += 1
        except Exception:  # noqa: BLE001
            continue
    return restored


def _has_auth_openai_session(flow) -> bool:
    """flow 手里有没有 **auth.openai.com 侧**的会话 cookie（0 网络请求的廉价判据）。

      · 有 → 先试直连 authorize（可能直接过，省掉整轮登录）；
      · 没有 → 这次 authorize 的作用就是**建立登录会话**，落到 /log-in 属预期路径
        （不是"注定失败的探测"）。
    """
    try:
        for name, _value, domain, _path in _iter_cookie_objects(flow):
            low = domain.lower()
            if "openai.com" in low and "chatgpt.com" not in low:
                return True
            if name in _OPENAI_SESSION_COOKIES and "chatgpt.com" not in low:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _truthy_setting(key: str, default: str = "") -> bool:
    """读任务冻结设置里的布尔项（1/true/on/yes 为真）。读不到用默认值。"""
    try:
        from smsinvit.invitation.runtime import TASK_SETTINGS

        value = (TASK_SETTINGS.get() or {}).get(key, default)
    except Exception:  # noqa: BLE001
        value = default
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def _try_codex_rt(flow, provider) -> str:
    """跑一次静默 SSO 换 RT，返回 refresh_token（空串=没换到）。

    刻意**不带 prompt=login**：带它会被强制重登并打回 /log-in，白绕一圈协议登录。
    """
    try:
        flow._codex_rt_attempted = False  # 内置尝试要关掉，这里要能真跑
    except Exception:  # noqa: BLE001
        pass
    rt = str(getattr(flow.result, "refresh_token", "") or "").strip()
    if rt:
        return rt
    try:
        flow.oauth_codex_rt_exchange(mail_provider=provider, prompt_override="")
    except Exception as e:  # noqa: BLE001
        logger.warning("Codex 授权异常（按未拿到 RT 处理）: %s: %s",
                       type(e).__name__, str(e)[:200])
    return str(getattr(flow.result, "refresh_token", "") or "").strip()


def _refresh_cookie_jar(email: str, flow) -> None:
    """授权成功后回写最新罐：罐跟服务端会话同步，SSO 命中率才不随库存下降。"""
    try:
        jar = capture_cookie_jar(flow)
        if not jar:
            return
        db.save_registered({"email": email, "cookie_jar": jar, "cookie_jar_at": time.time()})
        logger.info("[invite] %s 已回写会话罐（%d 条 cookie）", email, len(_json_loads_safe(jar)))
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] %s 会话罐回写失败（不影响结果）: %s", email, e)


def _json_loads_safe(raw: str) -> list:
    try:
        import json as _json

        value = _json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:  # noqa: BLE001
        return []


def _plant_pool_session(cfg, run_id: str, email: str, record: dict, password: str,
                        totp_secret: str, *, on_stage=None):
    """建 flow 并把池里的会话/罐种进去（两侧路径复用：直连与走代理）。

    只做"恢复"，不做登录 —— 登录由 `_rebuild_auth_session` 按需触发。
    """
    flow = _new_flow(cfg, run_id, email, want_2fa=False)
    flow.result.email = email
    flow.result.password = password
    flow.result.totp_secret = totp_secret

    emit_stage(on_stage, "accepting_invite")   # 恢复池中会话（不注册）
    _print_stage_log("accepting_invite", email)
    try:
        flow.from_existing_credentials(
            str(record.get("session_token") or ""),
            str(record.get("access_token") or ""),
            str(record.get("device_id") or ""),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] %s 恢复池中会话异常: %s", email, e)
    # from_existing_credentials 会用 token 里的 email 覆盖，这里以绑定邮箱为准。
    flow.result.email = email
    flow.result.password = password
    flow.result.totp_secret = flow.result.totp_secret or totp_secret
    _persist_restored_session(email, flow)

    # 重放存下来的 cookie 罐（含 auth.openai.com 会话）—— 有罐才可能免登录 SSO
    try:
        extra = (db.get_registered(email) or {}).get("extra") or {}
        jar = str(extra.get("cookie_jar") or "")
        captured_at = float(extra.get("cookie_jar_at") or 0)
        age_min = (time.time() - captured_at) / 60.0 if captured_at else -1.0
        limit = _sso_jar_max_age_minutes()
        # 未知年龄（没有 cookie_jar_at）也当过期：旧 auth.openai.com cookie 会把
        # 服务端状态带偏 → 卡在 Codex 授权确认页（见 _sso_jar_is_fresh 的实测说明）。
        fresh_enough = _sso_jar_is_fresh(captured_at, limit)
        if not fresh_enough:
            if captured_at:
                logger.info(
                    "[invite] %s 罐已 %.0f 分钟（超过免登录窗口 %.0f 分钟）→ 只回放 chatgpt.com "
                    "会话，auth.openai.com 的旧 cookie 全部丢弃（它们会导致登录后卡在授权确认页）",
                    email, age_min, limit,
                )
            else:
                logger.info(
                    "[invite] %s 会话罐没有记录时间（cookie_jar_at 缺失）→ 一律按过期处理：只回放 "
                    "chatgpt.com 会话，auth.openai.com 的旧 cookie 全部丢弃", email,
                )
        restored = _replay_cookie_jar(flow, jar, include_auth_openai=fresh_enough)
        if not fresh_enough:
            # 罐过期时还要清掉 `from_existing_credentials` 刚种下的 auth.openai.com
            # 会话（它来自旧的 session_token，同样会让重新登录后的状态错乱）。
            # 位置很关键：必须在"种完会话"之后、SSO 之前清，不能等到回落登录后再清
            # （那时会把登录页状态一起清掉，反而连登录都进不去）。
            try:
                seed = flow._drop_stale_auth_openai_cookies()
                if seed:
                    logger.info("[invite] %s 已清掉 %d 条旧的认证 cookie（来自旧 session_token）",
                                email, seed)
            except Exception as e:  # noqa: BLE001
                logger.warning("[invite] %s 清理旧认证 cookie 失败（不影响继续）: %s", email, e)
        if restored:
            captured_at = float(extra.get("cookie_jar_at") or 0)
            # 用**分钟**显示：实测免登录窗口只有 ~30 分钟，写成"0.0 天"会误导
            # （2 分钟和 29 分钟看起来一模一样）。
            age_min = (time.time() - captured_at) / 60.0 if captured_at else -1
            if age_min < 0:
                age_text = "未知"
            elif age_min < 90:
                age_text = f"{age_min:.0f} 分钟"
            else:
                age_text = f"{age_min / 60.0:.1f} 小时"
            logger.info("[invite] %s 已重放会话罐 %d 条 cookie（罐年龄 %s）", email, restored, age_text)
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] %s 会话罐重放失败（不影响后续）: %s", email, e)
    return flow


def _ensure_fresh_codex_creds(email: str, creds: dict, *, proxy: str) -> dict:
    """CLI 兑现前保证 access_token 新鲜；换不到就立刻失败（不空转）。

    为什么：凭据陈旧时 codex 会**先打 7 次 WebSocket 重试**（实测 1.8→17.7s）再失败，
    每次白烧 ~18s —— 失败样本 320-457s 里就有这个成分。这里在启动 CLI **之前**
    用 refresh_token 换一组新的；换不到就以明确原因失败。
    """
    try:
        from codex_turn import access_token_stale
    except Exception:  # noqa: BLE001
        return creds
    if not access_token_stale(creds.get("access_token"), skew=60):
        return creds
    refresh = str(creds.get("refresh_token") or "").strip()
    if not refresh:
        raise JobFailure(
            "Codex access_token 已过期且没有 refresh_token，无法继续；请重新授权该账号。",
            retryable=False,
        )
    try:
        fresh = exporter.refresh_codex_token(refresh, proxy=proxy)
    except Exception as e:  # noqa: BLE001
        raise JobFailure(
            f"Codex access_token 已过期且刷新失败（{str(e)[:120]}）；请重新授权该账号。",
            retryable=False,
        ) from e
    access = str((fresh or {}).get("access_token") or "").strip()
    if not access:
        raise JobFailure("Codex 凭据刷新未返回 access_token；请重新授权该账号。", retryable=False)
    updated = dict(creds)
    updated["access_token"] = access
    if (fresh or {}).get("id_token"):
        updated["id_token"] = str(fresh["id_token"])
    if (fresh or {}).get("refresh_token"):
        updated["refresh_token"] = str(fresh["refresh_token"])
    try:
        db.save_codex_auth(email, **{k: updated.get(k, "") or "" for k in
                                     ("access_token", "refresh_token", "id_token", "account_id")},
                           last_refresh=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if updated.get("refresh_token"):
            db.set_registered_refresh_token(email, updated["refresh_token"])
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] 刷新后的 Codex 凭据回写失败（不影响本次）: %s", e)
    logger.info("[invite] %s access_token 已过期 → 先刷新再跑 CLI 兑现", email)
    return updated


def _run_cli_with_fallback(*, email: str, creds: dict, proxy: str, job_id: str,
                           direct: bool) -> dict:
    """CLI 兑现这一轮：直连优先，**传输类**失败时用本次 run 的代理重跑一次。

    为什么默认直连：生产日志三段计时是 `启动 4.7-5.1s / 模型 10.1-29.8s`（三次输入
    都是同一个 ~10039 token），走的却全是代理；同一账号实测直连整轮 10.6s，走池内
    那个慢代理 18.3-33.3s，甚至整轮跑不完（71.9s 超时）。

    边界：
      · 后台显式 `sayhi_direct=0` → 只走代理且**不回退**（操作员可以锁死出口）；
      · 没有代理可用时，直连失败就是失败（与原语义一致）；
      · 只回退"传输/超时"这一种失败 —— 模型拒绝、账号问题不走第二次，别用重试
        掩盖真问题；
      · 回退那次超时 = `max(30, cli_timeout - 第一次耗时)`，整段 CLI 不超预算。
    """
    cli_proxy = "" if direct else proxy
    if direct:
        logger.info("[invite] %s CLI 兑现走直连（sayhi_direct 默认开；要锁代理请显式设 0）", email)
    started = time.time()
    cli = codex_cli.run_say_hi(job_id, {**creds, "email": email}, proxy=cli_proxy)
    if cli.get("ok") or not direct or not str(proxy or "").strip():
        return cli
    reason = str(cli.get("reason") or "")
    if not proxy_pool.is_transport_error(reason):
        return cli
    timeout = _int_setting("cli_timeout", 180)
    # 下限不能超过总预算本身：写死 max(30, ...) 会让一次 cli_timeout=60 的回退
    # 花到 60+30 秒，"整段 CLI 不超预算"就成了假的。
    remaining = max(min(30, timeout), timeout - int(time.time() - started))
    # 第一次可能已经轮换过凭证：回退必须带上，否则拿旧 token 再跑一轮只会白烧。
    direct_tokens = {k: v for k, v in (cli.get("tokens") or {}).items() if v}
    retry_creds = {key: direct_tokens.get(key) or creds.get(key) or ""
                   for key in ("access_token", "refresh_token", "id_token", "account_id")}
    # 直连那一轮若换掉了 refresh_token，旧 RT 此刻已经作废。先把账本落一次库，
    # 免得回退这轮跑完之前进程挂掉，账号被留在一把废钥匙上。
    if direct_tokens:
        try:
            db.save_codex_auth(email, **{key: direct_tokens.get(key) or creds.get(key) or ""
                                         for key in ("access_token", "refresh_token", "id_token", "account_id")},
                               last_refresh=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            if direct_tokens.get("refresh_token"):
                db.set_registered_refresh_token(email, direct_tokens["refresh_token"])
        except Exception as e:  # noqa: BLE001
            logger.warning("[invite] 直连轮换凭据先行落库失败（继续回退，不影响本轮结果）: %s", e)
    logger.warning("[invite] %s CLI 直连失败（%s）→ 回退代理重试一次（剩余预算 %ss）",
                   email, reason[:160], remaining)
    retry = codex_cli.run_say_hi(job_id, {**retry_creds, "email": email}, proxy=proxy,
                                 settings={"cli_timeout": str(remaining)})
    merged = dict(retry)
    # 变量名叫 merged 就得真的合并：回退轮未必再轮换，只取最后一轮会把直连那轮
    # 已经刷新的凭据丢掉（旧 RT 已作废，写回旧值等于把号弄废）。
    merged["tokens"] = {**direct_tokens,
                        **{k: v for k, v in (retry.get("tokens") or {}).items() if v}}
    merged["tokens_unreadable"] = bool(cli.get("tokens_unreadable")) or bool(retry.get("tokens_unreadable"))
    merged["fallback_from_direct"] = True
    merged["elapsed"] = round(float(cli.get("elapsed") or 0) + float(retry.get("elapsed") or 0), 1)
    logger.info("[invite] %s CLI 回退代理结果：ok=%s（直连+代理合计 %.1fs）",
                email, bool(retry.get("ok")), merged["elapsed"])
    return merged


def _complete_cli(email: str, creds: dict, *, proxy: str, job_id: str, on_stage=None) -> dict:
    """One explicitly requested CLI attempt; never restart OAuth after this boundary."""
    emit_stage(on_stage, "cli_say_hi")
    _print_stage_log("cli_say_hi", email)
    try:
        if not all(creds.get(key) for key in ("access_token", "refresh_token", "account_id")):
            raise JobFailure("Codex 授权凭据不完整，停止重新授权，请人工核查。", retryable=False)
        # 先保证凭据新鲜：过期就刷新，刷不到就立刻失败（避免 CLI 白烧 ~18s 重试）
        creds = _ensure_fresh_codex_creds(email, creds, proxy=proxy)
        db.save_codex_auth(email, **creds,
                           last_refresh=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        db.set_registered_refresh_token(email, creds["refresh_token"])
        # CLI 这一步默认直连（实测整轮 10.6s vs 走慢代理 33.3s）。默认值必须与后台
        # `_INVITE_DEFAULTS["sayhi_direct"]="1"` 一致：这一行以前写 "0"，而库里根本
        # 没有这一行 → 后台显示"直连已开"，生产却一直走代理（三段计时 16/33/36s）。
        cli = _run_cli_with_fallback(
            email=email, creds=creds, proxy=proxy, job_id=job_id,
            direct=_truthy_setting("sayhi_direct", "1"),
        )
        # `cli["tokens"]` 是**累计账本**（模型回退与代理回退都在里面累加过），
        # 不是"最后一轮的变化字段"。某一轮都没轮换过的字段才回落到 CLI 之前的
        # creds（它已被 _ensure_fresh_codex_creds 刷新并存过库），所以这里是安全的。
        tokens = cli.get("tokens") or {}
        if cli.get("tokens_unreadable"):
            # 读不出 auth.json：CLI 可能已经轮换，但我们**不知道**新值。保留原凭据
            # 并留下告警，绝不假装"没有变化"，也不去猜一个值写回。
            logger.warning("[invite] %s 的 auth.json 无法解析，本轮按未观察到轮换处理（保留原凭据）", email)
        final_creds = {key: tokens.get(key) or creds[key]
                       for key in ("access_token", "refresh_token", "id_token", "account_id")}
        # 失败路径同样要写：这一轮确实轮换过 RT，旧 RT 已经作废，不写就会把号弄废。
        db.save_codex_auth(email, **final_creds,
                           last_refresh=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        db.set_registered_refresh_token(email, final_creds["refresh_token"])
        if not cli.get("ok"):
            raise JobFailure(f"CLI 回合未完成: {cli.get('reason')}", retryable=False)
        db.save_codex_auth(email, cli_completed_at=time.time(),
                           cli_completed_account_id=final_creds["account_id"])
    except JobFailure as failure:
        failure.retryable = False
        raise
    except Exception as exc:
        raise JobFailure(f"CLI 阶段未完成，停止重新授权，请人工核查: {exc}", retryable=False) from exc

    emit_stage(on_stage, "export")
    _print_stage_log("export", email)
    export_res = _export_sub2api(email, {
        "email": email, "access_token": final_creds["access_token"],
        "refresh_token": final_creds["refresh_token"], "id_token": final_creds["id_token"],
        "client_id": exporter.CODEX_CLIENT_ID,
    })
    return {"email": email, "proxy": proxy, "account_id": final_creds["account_id"],
            "refresh_token_len": len(final_creds["refresh_token"]),
            "cli_elapsed": cli.get("elapsed"), "export": export_res, "inviter_email": ""}



def _new_flow(cfg: Config, run_id: str, email: str, *, want_2fa: bool = False) -> AuthFlow:
    env = {
        # 新邮箱也允许走「已有账号」分支：撞上老号时能拿凭证而不是白跑一轮
        "WEBUI_ALLOW_LOGIN": "1",
        "OTP_TIMEOUT": str(_int_setting("invite_otp_timeout", 120)),
        # 注册阶段先不换 Codex RT：等接受邀请之后再单独打 Codex 授权
        "OAUTH_CODEX_RT_EXCHANGE": "0",
        "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
    }
    on_session_ready = None
    if want_2fa:
        def on_session_ready(_flow, at):  # noqa: ANN001
            try:
                from .two_factor import bind_totp_2fa_inline
                info = bind_totp_2fa_inline(_flow, at)
                if info and info.get("secret"):
                    _flow.result.totp_secret = info["secret"]
            except Exception as e:  # noqa: BLE001
                logger.warning("2FA 绑定失败（账号仍有效）: %s", e)

    return AuthFlow(
        cfg,
        sms_callback=registrar._build_sms_callback(run_id),
        env_overrides=env,
        on_password=lambda e, p: db.save_password_early(e, p),
        on_session_ready=on_session_ready,
        account_callback=_account_callback,
    )


def _print_stage_log(name: str, email: str, **fields) -> None:
    label, pct = STAGES.get(name, (name, None))
    # 接码阶段会带金额（runtime.emit_task_stage 的字段）；沿用同一条日志，不加新风格。
    amount = fields.get("amount")
    detail = f"，金额={amount} {fields.get('currency') or ''}".rstrip() if amount is not None else ""
    logger.info("[invite] %s → %s%s%s", email, label, f"（{pct}%）" if pct is not None else "", detail)


# ──────────────────────── 导出（失败不算失败） ────────────────────────


def _export_sub2api(email: str, cred: dict) -> dict:
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:  # noqa: BLE001
        return {"attempted": False, "ok": False, "message": f"读导出配置失败: {e}"}
    sub = cfg.get("sub2api") or {}
    if not sub.get("enabled"):
        return {"attempted": False, "ok": False, "message": "SUB2API 导出未启用"}
    try:
        res = exporter.export_to_sub2api(cred, sub)
        ok = bool(res.get("ok"))
        msg = str(res.get("message") or res.get("error") or "")
        logger.info("[invite] SUB2API 导出%s: %s", "成功" if ok else "失败（不计入失败）", msg)
        return {"attempted": True, "ok": ok, "message": msg}
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] SUB2API 导出异常（不计入失败）: %s", e)
        return {"attempted": True, "ok": False, "message": str(e)}


# ──────────────────────── 单次尝试 ────────────────────────


def _restored_session_alive(flow) -> bool:
    """池里恢复出来的 chatgpt.com 会话是否**真的**还能用。

    ⚠️ from_existing_credentials 在 session_token 失效时只 warning 然后继续，
    result.access_token 可能还是那串旧的。此时 visit_invite_link 会把登录页
    当 200 正常页收下 → 变成"假接受邀请"，后面 CLI 必失败还查不出原因。
    所以必须自己确认一次：只有 /api/auth/session 真的回了 accessToken
    （auth_flow 里的 _session_refreshed），才算会话活着。
    """
    if not getattr(flow, "_session_refreshed", False):
        return False
    return bool(str(getattr(flow.result, "access_token", "") or "").strip())


def _persist_restored_session(email: str, flow) -> None:
    """把恢复/登录后刷新到的会话写回 registered，下一次尝试少一次刷新。

    只在值真的变了、且新值非空时才写 —— save_registered 是整行 REPLACE，
    拿空字符串覆盖会把本来还能用的凭证冲掉。
    """
    try:
        existing = db.get_registered(email) or {}
        current = {
            "access_token": str(existing.get("access_token") or "").strip(),
            "session_token": str(existing.get("session_token") or "").strip(),
            "cookie_header": str(existing.get("cookie_header") or "").strip(),
        }
        fresh = {
            key: str(getattr(flow.result, key, "") or "").strip() or current[key]
            for key in current
        }
        if fresh == current or not any(fresh.values()):
            return
        db.save_registered({
            "email": email,
            "password": str(existing.get("password") or "") or str(getattr(flow.result, "password", "") or ""),
            "access_token": fresh["access_token"],
            "session_token": fresh["session_token"],
            "cookie_header": fresh["cookie_header"],
            "refresh_token": str(existing.get("refresh_token") or ""),
            "id_token": str(existing.get("id_token") or ""),
            "totp_secret": str(existing.get("totp_secret") or "") or str(getattr(flow.result, "totp_secret", "") or ""),
            "totp_factor_id": str(existing.get("totp_factor_id") or ""),
            "device_id": str(existing.get("device_id") or ""),
            "proxy": str(existing.get("proxy") or ""),
            "source": str(existing.get("source") or "pool"),
        })
        logger.info("[invite] %s 已回写刷新后的会话凭证", email)
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] 会话回写失败（不影响本次执行）: %s", e)


def _is_rate_limited(text: str) -> bool:
    """是不是 OpenAI 的限流（429）。

    实测（2026-10-11）：协议登录连打几轮 PoW+TOTP 之后，
    `authorize/continue` 会直接回 429「Too many requests」，紧接着同一 IP 上的
    后续请求开始被 Cloudflare 连续 403 拦。**换代理立刻重试只会更糟** ——
    换 IP 会重新触发风控，密集请求又把 429 拖长。
    """
    t = str(text or "")
    return "status=429" in t or "HTTP 429" in t or "Too many requests" in t


def _rebuild_auth_session(flow, provider, email: str, password: str) -> bool:
    """会话失效时的纯协议登录：**不经** CSRF / signin / OAuth 初始化。

    先 GET 一次 Codex authorize —— 只为把 auth.openai.com 的登录会话立起来
    （这是 /authorize/continue 的前置状态），命中 /log-in 后交给
    `_codex_drive_login_from_log_in`：sentinel(PoW) → authorize_continue
    → 密码 → TOTP → 邮件 OTP → 命中就接码。

    返回 True 表示流程推进到了可继续的状态（拿到 callback code 或走完了登录）。
    """
    try:
        auth_url, state, verifier, redirect_uri, client_id = flow._build_codex_authorize()
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] %s 构造 Codex authorize 失败: %s", email, e)
        # 构造本身不该发网络请求；真出传输错误说明代理已经不可用 → 可重试。
        if proxy_pool.is_transport_error(e):
            raise JobFailure(f"代理不可用（{type(e).__name__}: {str(e)[:120]}），换代理重试", retryable=True) from e
        return False
    flow._oauth_auth_url = auth_url
    flow._oauth_client_id = client_id
    flow._oauth_redirect_uri = redirect_uri
    flow._oauth_state = state
    flow._manual_login_verifier = verifier
    flow._captured_login_verifier = verifier
    try:
        callback_url, final_url = flow._follow_authorize_for_callback(
            auth_url, redirect_uri, "codex_rebuild_session"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] %s authorize 链异常: %s", email, e)
        if proxy_pool.is_transport_error(e):
            # 网络/代理层失败**不等于**账号不可用：必须换代理重试，
            # 绝不能报"请重新注册该账号"（实测踩到：一个好号被 403 的坏代理判死）。
            raise JobFailure(f"代理不可用（{type(e).__name__}: {str(e)[:120]}），换代理重试", retryable=True) from e
        return False
    if callback_url:
        return True  # 会话其实还够用，直接拿到了授权码
    if "/log-in" not in (final_url or ""):
        return False
    try:
        continue_url = flow._codex_drive_login_from_log_in(mail_provider=provider)
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] %s 纯协议登录失败: %s", email, e)
        if proxy_pool.is_transport_error(e):
            raise JobFailure(f"代理不可用（{type(e).__name__}: {str(e)[:120]}），换代理重试", retryable=True) from e
        if _is_rate_limited(e):
            # 限流时**必须停手**：原来的行为是 return False → 外层换代理立刻再登一轮，
            # 结果是 429 越滚越重、紧接着整片 403（现场：13:38 一次作业里 429 → 403×4）。
            # 判定为不可重试，让作业就此停下等冷却，而不是继续加压。
            raise JobFailure(
                "OpenAI 限流（429）：本账号暂时无法登录，请冷却几分钟后再试"
                "（本次未消耗卡密；连续重试只会让限流更久）",
                retryable=False,
            ) from e
        return False
    return bool(continue_url)


def _install_stage_emitter(email: str, on_stage) -> None:
    """把（进度回调 + 运行日志）装进 contextvar，供 auth_flow / sms_provider 使用。"""
    try:
        from smsinvit.invitation.runtime import STAGE_EMITTER

        def emitter(name: str, **fields) -> None:  # noqa: ANN001
            emit_stage(on_stage, name)
            _print_stage_log(name, email, **fields)

        STAGE_EMITTER.set(emitter)
    except Exception:  # noqa: BLE001
        logger.debug("[invite] 阶段发射器安装失败（进度仍可来自主体流程）", exc_info=True)


def _record_codex_proxy(proxy: str, reason: str) -> None:
    """把邀请/Codex 授权阶段的失败记到**激活锁**这个维度上。

    409 类失败（错误文案含 409 / invalid_state）走 409 生命周期：
    连续 2 次 → 冻结 20 分钟；解冻后再 1 次 → 该维度永久不可用。
    其它失败只记普通 fail。注册锁与激活锁互不影响。
    """
    try:
        if proxy_pool.is_transport_error(reason):
            # 代理自身不可用：记健康维度（与用途无关），pick 会跳过它 → 换代理重试
            action = proxy_pool.record_transport_failure(proxy)
            logger.warning("[invite] 代理不可用记账 proxy=%s action=%s",
                           proxy, (action or {}).get("action"))
            return
        if proxy_pool.is_409_error(reason):
            action = proxy_pool.record_409(proxy, "codex")
            logger.warning("[invite] 激活 409 记账 proxy=%s action=%s",
                           proxy, (action or {}).get("action"))
            return
        proxy_pool.record_fail(proxy, reason, "codex")
    except Exception as e:  # noqa: BLE001
        logger.warning("[invite] 代理记账失败（不影响作业结果）: %s", e)


def _authorize_bound_account(
    *, code: str, email: str, proxy: str, run_id: str, job_id: str,
    invite_link: str, invite_already_processed: bool, on_stage=None,
) -> dict:
    """账号池取号后的完整链路（拆分后的授权阶段）。失败抛 JobFailure。

    与旧的 `_attempt_register_then_authorize` 的区别就是这次拆分的核心：
      · 不再注册 —— 号在池子里早已注册到 2FA；
      · 不再走 chatgpt.com 的 CSRF / signin / OAuth 初始化 —— 直接用池里存的
        session_token / access_token / device_id 恢复会话；
      · 不做 409 预检查 —— 授权阶段失败只记 fail，不冻结代理。
    """
    email = str(email or "").strip().lower()
    if invite_already_processed:
        saved = _saved_codex_creds(email, invite_link=invite_link)
        if saved:
            return _complete_cli(email, saved, proxy=proxy, job_id=job_id or code, on_stage=on_stage)

    record = db.get_registered(email) or {}
    password = str(record.get("password") or "").strip()
    if not password:
        raise JobFailure(
            "该邮箱在账号池里没有密码，无法登录；请先补注册该账号后再执行。",
            retryable=False,
        )
    totp_secret = str(record.get("totp_secret") or "").strip()

    cfg = Config()
    cfg.proxy = (proxy or "").strip() or None
    provider, kind, _account = mail_alloc.build_provider_for_account(email)
    flow = _plant_pool_session(cfg, run_id, email, record, password, totp_secret,
                               on_stage=on_stage)
    # 本阶段是否现场绑了手机（add-phone 走完 send+验证码校验）。见文件末尾的回写处。
    phone_bound_ok = bool(getattr(flow, "_phone_bound_ok", False))
    phone_number = getattr(flow, "_phone_number", "") if phone_bound_ok else ""
    phone_code = getattr(flow, "_phone_code", "") if phone_bound_ok else ""

    # ── 1b) 会话失效 → 重新登录，并把会话罐刷新成新的 ──
    if not _restored_session_alive(flow):
        logger.info("[invite] %s 池中会话已失效，尝试纯协议登录重建状态", email)
        rebuilt = _rebuild_auth_session(flow, provider, email, password)
        flow.result.email = email
        flow.result.password = password
        _persist_restored_session(email, flow)
        if not rebuilt or not _restored_session_alive(flow):
            # 能走到这里说明 authorize 链本身没抛传输错误 → 是凭证/会话真的不可用。
            raise JobFailure(
                "池中会话已失效，且当前无法在不走登录初始化链的前提下重建；"
                "请重新注册该账号后再执行。",
                retryable=False,
            )
        # 会话是刚重新登录起来的，罐里的旧 cookie 已经跟它对不上了 →
        # 立刻换成新罐，否则下一次兑换又会拿着过期罐去回放，等于没罐。
        _refresh_cookie_jar(email, flow)

    # ── 2) 接受邀请（沿用当前会话 = 同一个代理 IP）──
    if invite_already_processed:
        logger.info("[invite] %s 之前已处理过邀请，跳过接受邀请（防重放）", email)
        emit_stage(on_stage, "invite_accepted")
        _print_stage_log("invite_accepted", email)
    else:
        emit_stage(on_stage, "accepting_invite")
        _print_stage_log("accepting_invite", email)
        res = invite_mail.visit_invite_link(invite_link, flow)
        if not res.get("ok"):
            raise JobFailure(f"接受邀请失败: {res.get('reason')}")
        emit_stage(on_stage, "invite_accepted")
        _print_stage_log("invite_accepted", email)
        db.save_codex_auth(
            email,
            invite_processed_at=time.time(),
            invite_link=invite_link,
            web_access_token=flow.result.access_token or "",
            session_token=flow.result.session_token or "",
        )
        db.update_cdk_fields(code, invite_processed_at=time.time())

    # ── 3) Codex 授权直连（取 refresh_token）──
    emit_stage(on_stage, "codex_authorizing")
    _print_stage_log("codex_authorizing", email)
    # 廉价探针决定日志口径与预期：有 auth.openai.com 会话才可能直接 SSO。
    if _has_auth_openai_session(flow):
        logger.info("[invite] %s 检测到 auth.openai.com 会话 → 先试静默 SSO", email)
    else:
        logger.info("[invite] %s 无 auth.openai.com 会话 → 本次 authorize 用于建立登录会话"
                    "（预期落到 /log-in 后走协议登录）", email)
    rt = _try_codex_rt(flow, provider)

    # 第一次没换到 RT → **回落到最前面重新登录，再一步步重新走授权**。
    #
    # 顺序（用户明确的流程）：
    #   ① 注册用哪个出口 IP，授权就用哪个出口 IP（在上层 run_invite_job 里落实）；
    #   ② 会话罐失效 → 重新登录，并把罐刷新成新的；
    #   ③ 还不行 → 回落到最前面重新登录，然后一步步重新走授权；
    #   ④ 走完授权链后换取 RT（_try_codex_rt 内部就是静默 SSO → 必要时协议登录 →
    #      workspace/select → callback → 换 RT）。
    #
    # 为什么必须有这一步（2026-10-11 生产实测）：沿用池里那份旧会话直接静默 SSO 时，
    # 会在授权确认页反复打转直到失败；**会话是刚用协议登录建立起来的那次链路**才能走完
    # 并换到 RT。同一账号连续两次实测：重建会话 → 拿到 RT；不重建 → 失败。
    #
    # 只给**代理场景**补这一次重试：直连场景下面那段「直连失败改用代理重试」已经覆盖了，
    # 在这里再插一手会把它的换代理逻辑抢掉（那次尝试本来就不该走同一个被墙的出口）。
    if not rt and (proxy or "").strip():
        rebuilt = False
        try:
            rebuilt = bool(_rebuild_auth_session(flow, provider, email, password))
            flow.result.email = email
            flow.result.password = password
            if rebuilt:
                _persist_restored_session(email, flow)
        except Exception as e:  # noqa: BLE001
            logger.warning("[invite] %s 会话重建失败: %s: %s",
                           email, type(e).__name__, str(e)[:160])
        if rebuilt:
            # 重新登录后旧罐已经对不上新会话 → 换成新罐，再一步步走一遍授权。
            _refresh_cookie_jar(email, flow)
            logger.warning("[invite] %s 首次授权未换到 RT → 重新登录并刷新会话罐后重试一次", email)
            rt = _try_codex_rt(flow, provider)

    # 直连被 Cloudflare 间歇 403 是实测事实：开着 invite_direct 且直连没换到 RT 时，
    # 立刻用池内代理重建会话再试一次（不再直接失败）。
    if not rt and _truthy_setting("invite_direct", "0") and not (proxy or "").strip():
        retry_proxy = ""
        try:
            retry_proxy = str(proxy_pool.pick(purpose="codex",
                                             exclude_bound_to=email) or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("[invite] %s 直连失败后取代理异常: %s", email, e)
        if retry_proxy:
            logger.warning("[invite] %s 直连授权未成功 → 改用代理重试一次", email)
            retry_cfg = Config()
            retry_cfg.proxy = retry_proxy
            # 换 flow 之前先把旧 flow 的绑号标记收下，否则它随对象一起丢掉。
            phone_bound_ok = phone_bound_ok or bool(getattr(flow, "_phone_bound_ok", False))
            if getattr(flow, "_phone_bound_ok", False):
                phone_number = getattr(flow, "_phone_number", "") or phone_number
                phone_code = getattr(flow, "_phone_code", "") or phone_code
            flow = _plant_pool_session(retry_cfg, run_id, email, record, password,
                                       totp_secret, on_stage=on_stage)
            if not _restored_session_alive(flow):
                _rebuild_auth_session(flow, provider, email, password)
                flow.result.email = email
                flow.result.password = password
                _persist_restored_session(email, flow)
                # 换了出口 IP 就必须重新登录，登录完把罐也刷成这一版的。
                _refresh_cookie_jar(email, flow)
            rt = _try_codex_rt(flow, provider)
        else:
            logger.warning("[invite] %s 直连授权未成功，且池内没有可用代理", email)

    # 兑换阶段现场把手机绑上了，就必须回写账号的接码状态。
    #
    # 为什么必须在这里写：接码 controller 只拿到 run_id、不知道是哪个邮箱
    # （`_build_sms_callback(run_id)`），它自己写不了；而 `mark_phone_bound` 原来
    # 只在**注册**路径调过。于是「注册时绑号失败、兑换时才绑上」的号，页面会永远
    # 显示「未接」—— 哪怕授权已经成功、短信也确实收到并验证过了（现场实测）。
    # 直连→代理那条重试会换一个 flow，所以两个 flow 的标记都要看。
    phone_bound_ok = phone_bound_ok or bool(getattr(flow, "_phone_bound_ok", False))
    if getattr(flow, "_phone_bound_ok", False):
        phone_number = getattr(flow, "_phone_number", "") or phone_number
        phone_code = getattr(flow, "_phone_code", "") or phone_code
    if phone_bound_ok:
        try:
            db.mark_phone_bound(email, True, phone_number=phone_number, phone_code=phone_code)
            logger.info("[invite] %s 兑换阶段已绑手机 → 回写接码状态", email)
        except Exception as e:  # noqa: BLE001
            logger.warning("[invite] %s 接码状态回写失败（不影响本次结果）: %s", email, e)

    if rt:
        _save_rt_immediately(email, rt, proxy)
        _refresh_cookie_jar(email, flow)
        # 拿到 RT 就是这个号被消耗掉了 → 立刻把池内状态置为 done。
        #
        # 为什么必须在这里做：`mark_done` 原来只在作业流程（run_invite_job）和注册
        # 路径里调，**手动「授权」不会调** → 授权完成的号 status 还停在 in_use，
        # 于是「邮箱管理」那一列的三段身份（邮箱 / GPT / Codex，按 status 推导）
        # 会把它显示成「邮箱」，看着像没授权（现场实测：三个已授权+已绑卡的号全这样）。
        # 作业流程随后再调一次是幂等的。
        try:
            db.mark_done(email)
        except Exception as e:  # noqa: BLE001
            logger.warning("[invite] %s 池状态置 done 失败（不影响本次结果）: %s", email, e)
    if not rt:
        raise JobFailure("Codex 授权未拿到 refresh_token")
    emit_stage(on_stage, "codex_oauth")
    _print_stage_log("codex_oauth", email)
    logger.info("[invite] %s Codex 授权成功（RT len=%d）", email, len(rt))

    # ── 4) Codex CLI sayhi（成功判据）──
    try:
        fresh = exporter.refresh_codex_token(rt, proxy=proxy)
    except Exception as e:  # noqa: BLE001
        raise JobFailure(f"刷新 Codex 令牌失败: {e}", retryable=False) from e
    codex_creds = {
        "access_token": fresh.get("access_token") or "",
        "refresh_token": fresh.get("refresh_token") or rt,
        "id_token": fresh.get("id_token") or "",
        "account_id": _account_id_of(fresh.get("access_token") or ""),
    }
    return _complete_cli(email, codex_creds, proxy=proxy, job_id=job_id or code, on_stage=on_stage)


# ──────────────────────── 作业入口 ────────────────────────


def run_invite_job(
    code: str,
    *,
    on_stage: Optional[Callable[[str, str, Optional[int]], None]] = None,
    run_id: str = "",
    proxy_override: str = "",
    job_id: str = "",
) -> dict:
    """跑完一张 CDK 的邀请作业（含静默换 IP 重试）。

    返回 {"ok", "reason", "attempts", "proxies", "result"}；失败时 reason 是
    给后台看的真实原因，前端只拿通用文案。
    """
    code = str(code or "").strip()
    cdk = db.get_cdk_code(code)
    if not cdk:
        return {"ok": False, "reason": "卡密不存在", "attempts": 0, "proxies": [], "result": {}}
    email = str(cdk.get("email") or "").strip()
    if not email:
        return {"ok": False, "reason": "该卡密还没有绑定邮箱", "attempts": 0, "proxies": [], "result": {}}

    job_id = job_id or f"{code}-{int(time.time())}"
    max_attempts = proxy_pool.attempts_limit()
    tried: list[str] = []
    last_reason = ""

    _CURRENT_JOB.job_id = job_id
    # 作业行通常由 CdkController.enqueue 建好；直接调用（管理员/测试）时兜底建一条，
    # 否则阶段进度没处写。
    if not db.get_cdk_job(job_id):
        db.create_cdk_job(job_id, code, email=email, attempt=1)
    db.add_cdk_event(code, job_id, "start", f"attempts_limit={max_attempts}")
    emit_stage(on_stage, "queued")
    # 让深层的 PoW / 接码里程碑也能推进度（见 runtime.STAGE_EMITTER）
    _install_stage_emitter(email, on_stage)

    try:
        provider_for_invite, _kind, _acc = mail_alloc.build_provider_for_account(email)

        # ── 第一步：等邀请信（没有取到就等 invite_wait_timeout，默认 150s）──
        invite_link = str(cdk.get("invite_link") or "").strip()
        invite_summary = {
            "link": invite_link,
            "inviter_email": str(cdk.get("inviter_email") or ""),
            "subject": str(cdk.get("inviter_subject") or ""),
        }
        invite_processed = bool(cdk.get("invite_processed_at"))
        if not invite_link:
            emit_stage(on_stage, "waiting_invite")
            _print_stage_log("waiting_invite", email)
            wait_s = _int_setting("invite_wait_timeout", 150)

            # 等信期间让后台「实时看得见」：每轮收件箱有几封、认出几封邀请信；
            # 读信报错也立刻记一条事件（不再静默干等 150 秒）。
            _seen: dict = {"poll": "", "err": ""}

            def _on_poll(mail_count: int, invite_count: int) -> None:
                key = f"{mail_count}/{invite_count}"
                if key == _seen["poll"]:
                    return
                _seen["poll"] = key
                db.add_cdk_event(code, job_id, "invite_poll",
                                 f"收件箱 {mail_count} 封，识别为邀请信 {invite_count} 封")

            def _on_error(msg: str) -> None:
                if msg == _seen["err"]:
                    return
                _seen["err"] = msg
                db.add_cdk_event(code, job_id, "invite_read_error", msg[:300])

            got = invite_mail.read_invitation(provider_for_invite, timeout=wait_s,
                                              on_poll=_on_poll, on_error=_on_error)
            if not got.get("ok"):
                last_reason = str(got.get("reason") or "未收到邀请信")
                db.add_cdk_event(code, job_id, "invite_mail", last_reason)
                db.set_cdk_status(code, "failed", error=last_reason, finished_at=time.time())
                db.set_cdk_job(job_id, status="failed", error=last_reason, finished_at=time.time())
                emit_stage(on_stage, "failed")
                _CURRENT_JOB.job_id = ""
                return {"ok": False, "reason": last_reason, "attempts": 0,
                        "proxies": [], "result": {}}
            invite_summary = got["summary"]
            invite_link = str(invite_summary.get("link") or "")
            db.update_cdk_fields(
                code,
                inviter_email=invite_summary.get("inviter_email") or "",
                inviter_subject=invite_summary.get("subject") or "",
                invite_link=invite_link,
                invite_received_at=time.time(),
                invite_sent_at=cdk.get("invite_sent_at") or time.time(),
            )
            db.add_cdk_event(
                code, job_id, "invite_received",
                f"from={invite_summary.get('inviter_email')} subject={invite_summary.get('subject')}",
            )
            logger.info("[invite] %s 收到邀请信（发件人=%s）",
                        email, invite_summary.get("inviter_email"))
        emit_stage(on_stage, "invite_found")
        _print_stage_log("invite_found", email)

        # 注册时用的出口 IP 必须沿用到授权。
        #
        # 会话罐里的 cookie 和 session_token 都是在**注册那一刻的出口 IP** 上建立的，
        # 换个 IP 去回放，OpenAI 直接不认、要求重新输密码 —— 罐就白存了。
        # 账号行里的 proxy 由注册阶段 `mark_registered(proxy=used_proxy)` 写入。
        registration_proxy = ""
        try:
            account_row = db.get_account(email) or {}
            candidate = account_row.get("proxy") if isinstance(account_row, dict) else ""
            candidate = str(candidate or "").strip()
            # 只接受看起来像代理 URL 的值：读不到 / 脏数据 / 测试替身一律当"没记录"，
            # 退回"从池里挑"的原有行为，绝不能因为读不到账号行就把作业打断。
            if "://" in candidate:
                registration_proxy = candidate
        except Exception as e:  # noqa: BLE001
            logger.warning("[invite] %s 读取账号注册代理失败（按未记录处理）: %s", email, e)
        if registration_proxy:
            logger.info("[invite] %s 沿用注册时的出口 IP（注册代理已记录）", email)
        else:
            logger.warning("[invite] %s 账号行没有记录注册代理 → 只能另挑，"
                           "会话罐很可能因出口 IP 不同而失效", email)

        # ── 第二步：恢复会话 + 登录 + 授权，最多 max_attempts 次 ──
        for attempt in range(1, max_attempts + 1):
            emit_stage(on_stage, "picking_proxy")
            if proxy_override and attempt == 1:
                picked = {"url": proxy_override}
            elif registration_proxy and attempt == 1:
                # 第一次必须走注册时的那个出口，否则罐一定不认。
                picked = {"url": registration_proxy}
            else:
                # 不抢别的号占着的出口 IP（自己那条还能拿回来）。
                picked = proxy_pool.pick(exclude=tried, purpose="codex",
                                         exclude_bound_to=email)
            if not picked:
                # 所有代理都不可用（连不上/隧道被拒被冻结，或已停用）→ 立刻停下，
                # 不消耗卡密，并把「代理不可用」显示出来。
                last_reason = ("代理不可用：池内没有可用代理（都已不可用或冻结中），"
                               "请到后台代理池检查或重置后再试")
                db.add_cdk_event(code, job_id, "proxy_unavailable", last_reason)
                break

            proxy = str(picked.get("url") or "")
            tried.append(proxy)
            db.set_cdk_job(job_id, attempt=attempt, proxy=proxy, status="running",
                           started_at=time.time())
            db.update_cdk_fields(code, proxy_used=proxy, attempts=attempt)
            logger.info("[invite] %s 第 %d/%d 次尝试，代理=%s", email, attempt, max_attempts, proxy)

            authorization_finished = False
            try:
                # 授权阶段不做 warmup / 409 预检查，也不按 409 处理代理：
                # 这个号是否可用由池子注册时的预检决定，这里失败就是失败。
                result = _authorize_bound_account(
                    code=code, email=email, proxy=proxy, run_id=run_id, job_id=job_id,
                    invite_link=invite_link,
                    invite_already_processed=bool((db.get_cdk_code(code) or {}).get('invite_processed_at')),
                    on_stage=on_stage,
                )
                authorization_finished = True
                result["inviter_email"] = invite_summary.get("inviter_email") or ""
                proxy_pool.record_success(proxy, "codex")
                # 邀请流程完成 → 这个邮箱从"被占用"变成"已用掉"，不能再流回池子。
                try:
                    db.set_account_proxy(email, proxy)
                    db.mark_done(email)
                except Exception:  # noqa: BLE001
                    logger.warning("[invite] %s 号池状态回写失败（不影响结果）", email)
                emit_stage(on_stage, "done")
                _print_stage_log("done", email)
                db.set_cdk_status(
                    code, "done",
                    finished_at=time.time(),
                    error=None,
                    result_json=_json_dumps({
                        "email": email,
                        "inviter_email": result.get("inviter_email"),
                        "account_id": result.get("account_id"),
                        "export": result.get("export"),
                        "proxy_attempts": len(tried),
                    }),
                )
                db.set_cdk_job(job_id, status="done", finished_at=time.time())
                db.add_cdk_event(code, job_id, "done", f"attempts={attempt} proxies={len(tried)}")
                _CURRENT_JOB.job_id = ""
                return {"ok": True, "reason": "", "attempts": attempt,
                        "proxies": tried, "result": result}
            except JobFailure as e:
                last_reason = str(e)
                # ⚠️ 记账必须放在 break 之前：非重试的**传输错误**（代理拒绝隧道）
                #    以前因为先 break 而一次都记不到，坏代理会被反复挑中。
                #    账号自身的错误（缺密码等）不赖代理，只记传输/409/可重试这三类。
                if (e.retryable or proxy_pool.is_409_error(last_reason)
                        or proxy_pool.is_transport_error(last_reason)):
                    _record_codex_proxy(proxy, last_reason)
                if authorization_finished or not e.retryable:
                    db.add_cdk_event(code, job_id, "authorization_retry_stopped", last_reason)
                    break
                if _sms_retry_stopped(e):
                    db.add_cdk_event(code, job_id, "sms_retry_stopped", last_reason)
                    break
                db.add_cdk_event(code, job_id, "fail", f"proxy={proxy} {last_reason}")
                logger.warning("[invite] %s 第 %d 次尝试失败: %s", email, attempt, last_reason)
            except Exception as e:  # noqa: BLE001
                last_reason = f"{type(e).__name__}: {e}"
                if proxy_pool.is_transport_error(last_reason) or proxy_pool.is_409_error(last_reason):
                    _record_codex_proxy(proxy, last_reason)
                if authorization_finished:
                    db.add_cdk_event(code, job_id, "authorization_retry_stopped", last_reason)
                    break
                if _sms_retry_stopped(e):
                    db.add_cdk_event(code, job_id, "sms_retry_stopped", last_reason)
                    break
                _record_codex_proxy(proxy, last_reason)
                db.add_cdk_event(code, job_id, "error", last_reason)
                logger.exception("[invite] %s 第 %d 次尝试异常", email, attempt)
    except Exception as e:  # noqa: BLE001
        last_reason = last_reason or f"{type(e).__name__}: {e}"
        logger.exception("[invite] 作业异常")
        db.add_cdk_event(code, job_id, "error", last_reason)

    # 没有可用代理：不算这个号的失败，退回 bound 让用户稍后再点，
    # 但要把「代理不可用」留在卡密上，用户才看得到原因。
    if last_reason.startswith("代理不可用") or last_reason.startswith("PROXY_UNAVAILABLE"):
        db.set_cdk_status(code, "bound", error=last_reason)
        db.set_cdk_job(job_id, status="failed", finished_at=time.time(), error=last_reason)
        db.set_cdk_job(job_id, status="failed", error=last_reason, finished_at=time.time())
        db.add_cdk_event(code, job_id, "busy", "无可用代理，本次未执行")
        emit_stage(on_stage, "failed")
        _CURRENT_JOB.job_id = ""
        return {"ok": False, "reason": "busy", "message": "当前繁忙，请稍后重试",
                "attempts": len(tried), "proxies": tried, "result": {}}

    emit_stage(on_stage, "failed")
    _print_stage_log("failed", email)
    fresh = db.get_cdk_code(code) or {}
    db.set_cdk_status(code, "failed", error=last_reason, finished_at=time.time(),
                      fail_count=int(fresh.get("fail_count") or 0) + 1)
    db.set_cdk_job(job_id, status="failed", error=last_reason, finished_at=time.time())
    db.add_cdk_event(code, job_id, "failed", last_reason)
    _CURRENT_JOB.job_id = ""
    return {"ok": False, "reason": last_reason or "未知失败",
            "attempts": len(tried), "proxies": tried, "result": {}}


def _json_dumps(obj) -> str:
    import json as _json
    try:
        return _json.dumps(obj, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return "{}"


def authorize_registered_email(
    email: str,
    *,
    on_stage: Optional[Callable[[str, str, Optional[int]], None]] = None,
    run_id: str = "",
    proxy_override: str = "",
) -> dict:
    """管理员手动路径：对**已经注册过**的邮箱补跑「邀请 + Codex 授权 + sayhi」。

    与 CDK 作业的区别：不分配邮箱、不注册（直接登录已有账号）。
    """
    email = str(email or "").strip().lower()
    cred = db.get_registered(email)
    if not cred:
        return {"ok": False, "reason": f"未找到已注册账号: {email}", "attempts": 0, "proxies": []}

    max_attempts = proxy_pool.attempts_limit()
    tried: list[str] = []
    last_reason = ""
    job_id = f"manual-{email.split('@')[0]}-{int(time.time())}"

    emit_stage(on_stage, "queued")
    _install_stage_emitter(email, on_stage)
    for attempt in range(1, max_attempts + 1):
        # 代理：优先账号自己注册时那个（仍可用的话），否则 least-used
        picked = None
        if attempt == 1 and proxy_override:
            picked = {"url": proxy_override}
        elif attempt == 1 and cred.get("proxy"):
            row = db.proxy_get(cred["proxy"])
            now = time.time()
            if row and row.get("enabled") and not row.get("permanent_dead") and (
                not row.get("frozen_until") or row["frozen_until"] <= now
            ):
                picked = {"url": row["url"]}
        if picked is None:
            picked = proxy_pool.pick(exclude=tried, purpose="codex",
                                     exclude_bound_to=email)
        if not picked:
            last_reason = "PROXY_UNAVAILABLE: 没有可用代理"
            break
        proxy = str(picked["url"])
        tried.append(proxy)
        logger.info("[invite] 手动授权 %s 第 %d/%d 次，代理=%s", email, attempt, max_attempts, proxy)
        authorization_finished = False
        try:
            result = _manual_attempt(email, proxy=proxy, run_id=run_id, job_id=job_id,
                                     on_stage=on_stage)
            authorization_finished = True
            proxy_pool.record_success(proxy, "codex")
            emit_stage(on_stage, "done")
            return {"ok": True, "reason": "", "attempts": attempt, "proxies": tried,
                    "result": result}
        except JobFailure as e:
            last_reason = str(e)
            if authorization_finished or not e.retryable:
                logger.warning("[invite] 手动授权停止重试: %s", last_reason)
                break
            if _sms_retry_stopped(e):
                logger.warning("[invite] 手动授权停止重试: %s", last_reason)
                break
            if e.is_409:
                proxy_pool.record_409(proxy)
            else:
                proxy_pool.record_fail(proxy, last_reason)
            logger.warning("[invite] 手动授权 %s 第 %d 次失败（409=%s）: %s",
                           email, attempt, e.is_409, last_reason)
        except Exception as e:  # noqa: BLE001
            last_reason = f"{type(e).__name__}: {e}"
            if authorization_finished:
                logger.warning("[invite] 已完成授权阶段，停止自动重试: %s", last_reason)
                break
            if _sms_retry_stopped(e):
                logger.warning("[invite] 手动授权停止重试: %s", last_reason)
                break
            proxy_pool.record_fail(proxy, last_reason)
            logger.exception("[invite] 手动授权 %s 第 %d 次异常", email, attempt)
    emit_stage(on_stage, "failed")
    return {"ok": False, "reason": last_reason or "未知失败",
            "attempts": len(tried), "proxies": tried}


def _manual_attempt(email: str, *, proxy: str, run_id: str, job_id: str, on_stage=None) -> dict:
    """已注册账号：读邀请信 → 登录 → 接受邀请 → Codex 授权 → sayhi → 导出。"""
    saved = _saved_codex_creds(email)
    if saved:
        logger.info("[invite] %s 已保存 Codex 授权，人工重试只继续 CLI 阶段", email)
        return _complete_cli(email, saved, proxy=proxy, job_id=job_id, on_stage=on_stage)
    cfg = Config()
    cfg.proxy = (proxy or "").strip() or None
    provider, kind, _account = mail_alloc.build_provider_for_account(email)

    emit_stage(on_stage, "waiting_invite")
    wait_s = _int_setting("invite_wait_timeout", 150)
    # ★ 重跑别再干等：邀请信通常早已处理过；而且 CF 邮箱接口只给最近 20 封，
    #   老邀请信会被后来的登录提醒挤出窗口 —— 表现就是"每次都等 150s 说没收到"。
    #   库里已存 invite_link / invite_processed_at 时直接复用（跳过等待）。
    _auth_row = db.get_codex_auth(email) or {}
    _stored_link = str(_auth_row.get("invite_link") or "").strip()
    if _stored_link or _auth_row.get("invite_processed_at"):
        summary = {"link": _stored_link, "inviter_email": "", "inviter_name": "",
                   "inviter_source": "db", "subject": "", "id": "", "received": None}
        logger.info("[invite] %s 邀请信此前已处理（链接=%s）→ 跳过等待收信，省 %ss",
                    email, "库里有" if _stored_link else "库里没有", int(wait_s))
    else:
        got = invite_mail.read_invitation(
            provider, timeout=wait_s,
            on_poll=lambda n, iv: logger.info("[invite] 收件箱 %s 封，认出邀请信 %s 封…", n, iv),
            on_error=lambda m: logger.warning("[invite] 读信异常: %s", m),
        )
        if not got.get("ok"):
            raise JobFailure(str(got.get("reason") or "未收到邀请信"))
        summary = got["summary"]
    emit_stage(on_stage, "invite_found")

    record = db.get_registered(email) or {}
    password = str(record.get("password") or "").strip()
    if not password:
        raise JobFailure("该账号库里没有密码，无法登录（passwordless 号请手动补录密码）")

    # ⚠️ 手动路径以前没传 want_2fa → 开关打开也永远不绑 2FA。
    #    而且 _on_session_ready 钩子只在 run_register() 里被调用，
    #    run_protocol_login() 完全不调 —— 所以这条路必须**显式**补绑（见下面）。
    want_2fa = str(db.get_setting("invite_want_2fa", "1")).strip().lower() in ("1", "true", "yes", "on")
    flow = _new_flow(cfg, run_id, email, want_2fa=want_2fa)
    emit_stage(on_stage, "registering")   # 对外文案一致：正在接受邀请
    try:
        flow.run_protocol_login(provider, email, password)
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        raise JobFailure(msg, is_409=proxy_pool.is_409_error(msg)) from e
    emit_stage(on_stage, "registered")

    # ── 2FA：run_protocol_login 不会触发 on_session_ready 钩子，这里显式补绑 ──
    #    顺序仍是「登录 → 绑 2FA → 接受邀请 → Codex 授权」，
    #    所以即使后面在 add-phone 卡住，2FA 也已经绑好并落库了（secret 一次性下发，丢了就永久锁死）。
    if want_2fa and str(getattr(flow.result, "totp_secret", "") or "").strip() == "":
        prev = str((db.get_registered(email) or {}).get("totp_secret") or "").strip()
        if prev:
            flow.result.totp_secret = prev
            logger.info("[invite] %s 已有 2FA secret，跳过重复绑定", email)
        else:
            try:
                from .two_factor import bind_totp_2fa_inline
                at = str(getattr(flow.result, "access_token", "") or "")
                info = bind_totp_2fa_inline(flow, at) if at else None
                if info and info.get("secret"):
                    flow.result.totp_secret = info["secret"]
                    db.save_registered({
                        "email": email,
                        "password": (db.get_registered(email) or {}).get("password", "") or password,
                        "access_token": at,
                        "session_token": getattr(flow.result, "session_token", "") or "",
                        "totp_secret": info["secret"],
                        "totp_factor_id": info.get("factor_id", ""),
                        "proxy": proxy or "",
                        "source": "invite",
                    })
                    logger.info("[invite] %s 已绑定 2FA 并落库（secret 已保存）", email)
                else:
                    logger.warning("[invite] %s 2FA 绑定未拿到 secret（账号仍有效）", email)
            except Exception as e:  # noqa: BLE001
                logger.warning("[invite] 2FA 绑定失败（不影响后续授权）: %s", e)

    already = False
    try:
        existing = db.get_codex_auth(email) or {}
        already = bool(existing.get("invite_processed_at"))
    except Exception:  # noqa: BLE001
        already = False

    if not already:
        emit_stage(on_stage, "accepting_invite")
        res = invite_mail.visit_invite_link(summary["link"], flow)
        if not res.get("ok"):
            raise JobFailure(f"接受邀请失败: {res.get('reason')}")
        db.save_codex_auth(email, invite_processed_at=time.time(),
                           invite_link=summary["link"])

    emit_stage(on_stage, "codex_oauth")
    try:
        flow._codex_rt_attempted = False
    except Exception:  # noqa: BLE001
        pass
    rt = str(getattr(flow.result, "refresh_token", "") or "").strip()
    if not rt:
        # 同 _authorize_bound_account：会话已恢复，优先静默 SSO（不带 prompt=login），
        # 避免被强制重登打回 /log-in 再绕一遍协议登录。
        flow.oauth_codex_rt_exchange(mail_provider=provider, prompt_override="")
        rt = str(getattr(flow.result, "refresh_token", "") or "").strip()
    if getattr(flow, "_phone_bound_ok", False):
        try:
            db.mark_phone_bound(
                email, True,
                phone_number=getattr(flow, "_phone_number", ""),
                phone_code=getattr(flow, "_phone_code", ""),
            )
            logger.info("[invite] %s 手动授权已绑手机 → 回写接码状态", email)
        except Exception as e:  # noqa: BLE001
            logger.warning("[invite] %s 接码状态回写失败（不影响本次结果）: %s", email, e)
    if rt:
        _save_rt_immediately(email, rt, proxy)
        _refresh_cookie_jar(email, flow)
        # 拿到 RT 就是这个号被消耗掉了 → 立刻把池内状态置为 done。
        #
        # 为什么必须在这里做：`mark_done` 原来只在作业流程（run_invite_job）和注册
        # 路径里调，**手动「授权」不会调** → 授权完成的号 status 还停在 in_use，
        # 于是「邮箱管理」那一列的三段身份（邮箱 / GPT / Codex，按 status 推导）
        # 会把它显示成「邮箱」，看着像没授权（现场实测：三个已授权+已绑卡的号全这样）。
        # 作业流程随后再调一次是幂等的。
        try:
            db.mark_done(email)
        except Exception as e:  # noqa: BLE001
            logger.warning("[invite] %s 池状态置 done 失败（不影响本次结果）: %s", email, e)
    if not rt:
        raise JobFailure("Codex 授权未拿到 refresh_token")

    try:
        fresh = exporter.refresh_codex_token(rt, proxy=proxy)
    except Exception as e:  # noqa: BLE001
        raise JobFailure(f"刷新 Codex 令牌失败: {e}", retryable=False) from e
    codex_creds = {
        "access_token": fresh.get("access_token") or "",
        "refresh_token": fresh.get("refresh_token") or rt,
        "id_token": fresh.get("id_token") or "",
        "account_id": _account_id_of(fresh.get("access_token") or ""),
    }
    result = _complete_cli(email, codex_creds, proxy=proxy, job_id=job_id, on_stage=on_stage)
    result["inviter_email"] = summary.get("inviter_email") or ""
    return result

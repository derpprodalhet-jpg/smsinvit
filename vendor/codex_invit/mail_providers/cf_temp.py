"""Cloudflare Worker 自建临时邮箱 provider（dreamhunter2333/cloudflare_temp_email）。

借鉴 lxf746/any-auto-register 的 CFWorkerMailbox 实战逻辑：
  - POST /admin/new_address 创建邮箱（必须带 enablePrefix=True）
  - GET  /admin/mails?address=<email> 拉特定邮箱的邮件列表
  - 从 raw 字段抽 OTP，严格过滤 hex 颜色 / 邮箱地址 / 时间戳

能力：pooled=False（自己造地址，不走号池）
      ephemeral=True（每次新地址 → OpenAI 始终当新号，这是 CF 能跑通的根因）

主人需要：
    api_url          Worker HTTPS 地址（如 https://mail.example.com）
    admin_token      Worker 配置的 ADMIN_PASSWORDS
    domain           主人配的 catch-all 域名（如 example.com）

本文件由 mail_cf.py 迁移而来，取件逻辑逐字保留未改。
mail_cf.py 现为转发壳，旧 import 路径继续可用。
"""
from __future__ import annotations

import json as _json
import logging
import random
import socket
import string
import time
import urllib.parse
from smsinvit.invitation import urlclient as _secure_urllib
from datetime import datetime, timezone
from email import message_from_string as _message_from_string
from email.utils import parseaddr as _parseaddr
from typing import Optional

from .base import (
    ConfigField,
    MailProvider,
    MailProviderError,
    extract_otp,
    parse_mail_ts,
    register,
)

logger = logging.getLogger(__name__)


class MailboxConfigurationError(MailProviderError):
    """Invalid configuration or deployment policy; no creation was sent."""


class MailboxConnectionError(MailProviderError):
    """Connection failed before dispatch, or the provider explicitly rejected it."""


class MailboxCreationUncertainError(MailProviderError):
    """Creation may have reached the provider; never automatically resend."""


def _creation_not_sent(error: Exception) -> bool:
    """Only transport failures which prove no HTTP request was sent."""
    from curl_cffi.curl import CurlError
    from requests.exceptions import ConnectTimeout

    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (socket.gaierror, ConnectionRefusedError, ConnectTimeout)):
            return True
        # libcurl's DNS/proxy DNS and connect failures happen before HTTP dispatch.
        if isinstance(current, CurlError) and getattr(current, 'code', None) in (5, 6, 7):
            return True
        current = current.__cause__ or current.__context__
    return False


def _resolve_proxy(explicit: str = "") -> str:
    """给 CF 取件解析代理：显式配置 > 系统代理(macOS) > 环境变量。

    为什么要自动跟随系统代理：curl_cffi 是唯一带 chrome 指纹、能过 CF 1010 的通道，
    但它默认不读系统/环境代理；本机靠 Clash(1082) 之类上外网时，
    不给它代理就会直连超时，而兜底的 urllib 走系统代理能连通却必被 1010 拦 ——
    表现出来就是「Worker 明明好的，就是取不到件」。
    """
    import os
    candidates: list = []
    if explicit:
        candidates.append(explicit)
    else:
        try:
            prox = _secure_urllib.getproxies() or {}
            candidates += [prox.get("https"), prox.get("http")]
        except Exception:
            pass
        candidates += [
            os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
            os.environ.get("HTTP_PROXY"), os.environ.get("http_proxy"),
            os.environ.get("ALL_PROXY"), os.environ.get("all_proxy"),
        ]
    for c in candidates:
        c = str(c or "").strip()
        if not c:
            continue
        try:
            from http_client import normalize_proxy
            n = normalize_proxy(c)
        except Exception:
            n = c
        if n:
            return n
    return ""



def _gen_local_part(rng: Optional[random.Random] = None, length: int = 10) -> str:
    """生成随机邮箱前缀。参考 any-auto-register 用 10 位 lowercase+digits。"""
    r = rng or random
    return "".join(r.choices(string.ascii_lowercase + string.digits, k=length))


# OTP 抽取已上提到 base.extract_otp（与 outlook 共用同一套防误判规则）。
# 保留旧名给可能的外部调用。
_extract_otp = extract_otp


@register
class CFTempEmailProvider(MailProvider):
    """Cloudflare Worker 自建临时邮箱 provider。

    使用方式：
        mail = CFTempEmailProvider(
            api_url="https://mail.example.com",
            admin_token="<YOUR_ADMIN_PASSWORDS>",
            domain="example.com",
        )
        auth_flow.run_register(mail)
    """

    kind = "cf_temp"
    display_name = "CF Worker 临时邮箱"
    pooled = False         # 地址自己造，无限量，不走号池
    ephemeral = True       # 每次新地址 → OpenAI 永远当新号

    line_segments = 0      # 不支持导入
    import_hint = ""
    import_placeholder = ""

    config_fields = [
        ConfigField(
            "cf_api_url", "Worker 地址",
            placeholder="https://mail.example.com",
            help="Cloudflare Worker 的 HTTPS 地址，末尾不带斜杠",
        ),
        ConfigField(
            "cf_admin_token", "Admin Token", type="password",
            help="Worker 环境变量 ADMIN_PASSWORDS 的值",
        ),
        ConfigField(
            "cf_domain", "收件域名",
            placeholder="example.com",
            help="已配置 catch-all 的域名",
        ),
    ]

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        domain: str = "",
        session=None,
        proxy: str = "",
    ):
        if not api_url:
            raise MailboxConfigurationError("邮箱服务地址未配置。")
        if not domain:
            raise MailboxConfigurationError("收件域名未配置。")
        self.api_url = api_url.rstrip("/")
        self.admin_token = admin_token
        self.domain = domain
        self._jwt: str = ""
        self._current_email: str = ""
        self._pinned_email: str = ""
        self._seen_mail_ids: set = set()
        self._rng = random.Random()
        self.last_persona = None

        # ⚠️ 代理是 CF 取件能不能通的关键：curl_cffi 带 chrome 指纹才过得了
        #    CF 的 1010，而 curl_cffi 默认 trust_env=False（不读环境变量/系统代理）。
        #    本机若靠 Clash 之类上外网，不给它代理就会直连超时（curl 28），
        #    反而只有兜底的 urllib 走系统代理能连上 —— 但 urllib 的指纹必被 1010 拦。
        #    所以：显式 cf_proxy > 系统代理(urllib.getproxies) > 环境变量。
        self.proxy = _resolve_proxy(proxy)

        # 用 curl_cffi 模拟 Chrome 指纹，过 CF Bot Fight Mode
        if session is not None:
            self._session = session
        else:
            try:
                from smsinvit.invitation.safety import GuardedCffiSession as CffiSession
                self._session = CffiSession(impersonate="chrome136")
                self._session.trust_env = False
                if self.proxy:
                    self._session.proxies = {"http": self.proxy, "https": self.proxy}
            except ImportError:
                self._session = None

    # ──────────────────────── 构造入口 ────────────────────────

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        api_url = (settings.get("cf_api_url") or "").strip()
        domain = (settings.get("cf_domain") or "").strip()
        token = (settings.get("cf_admin_token") or "").strip()
        if not api_url or not domain or not token:
            raise MailboxConfigurationError("CF 邮箱配置不完整，请检查邮箱配置。")
        return cls(
            api_url=api_url, admin_token=token, domain=domain,
            proxy=(settings.get("cf_proxy") or "").strip(),
        )

    # ──────────────────────── HTTP 工具 ────────────────────────

    def _headers(self) -> dict:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-admin-auth": self.admin_token,
        }

    def _request(self, method: str, path: str, **kwargs):
        """One creation dispatch; read-only requests share a three-attempt budget."""
        from smsinvit.invitation.safety import require_proxy, require_url

        url = f"{self.api_url}{path}"
        m = method.upper()
        if m not in ("GET", "POST"):
            raise MailboxConfigurationError("邮箱请求类型不支持。")
        timeout = kwargs.get("timeout", 15)
        headers = dict(kwargs.get("headers") or self._headers())
        json_body = kwargs.get("json")
        params = kwargs.get("params")

        # Reject invalid destinations before either transport can send a POST.
        try:
            require_url(url)
            if self.proxy:
                require_proxy(self.proxy)
        except ValueError:
            raise MailboxConfigurationError("邮箱服务地址或代理未获部署授权，请联系管理员检查配置。") from None
        except Exception:
            raise MailboxConnectionError("邮箱服务暂时无法连接，请稍后重试。") from None

        px = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        attempts = 3 if m == "GET" else 1
        for attempt in range(attempts):
            try:
                # For reads the last attempt may use the guarded fallback adapter.
                # A POST chooses its transport before dispatch and never falls back.
                primary = self._session is not None and (m == "POST" or attempt < 2)
                if primary:
                    if m == "GET":
                        response = self._session.get(url, headers=headers, params=params,
                                                     timeout=timeout, proxies=px,
                                                     allow_redirects=True)
                    else:
                        body = _json.dumps(json_body, separators=(",", ":")) if json_body is not None else None
                        response = self._session.post(url, headers=headers, data=body,
                                                      timeout=timeout, proxies=px,
                                                      allow_redirects=False)
                else:
                    response = self._urllib_request(m, url, headers, json_body, params, timeout)
                if m == "GET" and getattr(response, "status_code", 0) in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                return response
            except ValueError:
                if m == "POST":
                    raise MailboxCreationUncertainError("邮箱创建结果未确认，请联系管理员核查，勿重复领取。") from None
                raise MailboxConfigurationError("邮箱读取请求被部署策略拒绝，请联系管理员检查配置。") from None
            except Exception as error:
                if m == "POST":
                    if _creation_not_sent(error):
                        raise MailboxConnectionError("邮箱服务暂时无法连接，请稍后重试。") from None
                    raise MailboxCreationUncertainError("邮箱创建结果未确认，请联系管理员核查，勿重复领取。") from None
                if attempt == attempts - 1:
                    raise MailboxConnectionError("邮箱读取服务暂时无法连接，请稍后重试。") from None
                logger.warning("[cf_temp] 邮箱读取暂时失败，准备第 %d 次尝试", attempt + 2)
                time.sleep(0.8 * (attempt + 1))

    def _urllib_request(self, method, url, headers, json_body, params, timeout):
        """One guarded fallback attempt; writes never follow redirects."""
        from smsinvit.invitation.safety import GuardedRequestsSession

        if params:
            qs = urllib.parse.urlencode(params)
            url = f"{url}?{qs}"
        body = _json.dumps(json_body).encode() if json_body is not None else None
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else {}
        # This is the same policy/pinning adapter used by urlclient, with the
        # explicit redirect option its urllib compatibility wrapper cannot pass.
        with GuardedRequestsSession() as session:
            return session.request(method, url, data=body, headers=headers,
                                   timeout=timeout, proxies=proxies,
                                   allow_redirects=method == "GET")

    @staticmethod
    def _mail_epoch(mail: dict) -> Optional[float]:
        """把邮件的 created_at 解析成 epoch 秒；解析不出来返回 None。

        统一走 `mail_providers.base.parse_mail_ts`（int / 毫秒 / 数字串 /
        裸 UTC 时间串 / ISO8601 / None 全都认）—— 这里以前只认字符串，
        遇到整数时间戳直接 AttributeError，害得邀请信明明在邮箱里却白等 150 秒。
        """
        if not isinstance(mail, dict):
            return None
        return parse_mail_ts(
            mail.get("created_at") or mail.get("received")
            or mail.get("timestamp") or mail.get("date")
        )

    @staticmethod
    def _parse_json(resp) -> dict:
        try:
            return resp.json() if callable(getattr(resp, "json", None)) else _json.loads(resp.text)
        except Exception:
            return {}

    # ──────────────────────── 公共 API ────────────────────────

    def pin_mailbox(self, address: str) -> None:
        """钉住兑换阶段就已经分配出去的地址。

        CF 临时邮箱默认「每次新造」—— 但 CDK 兑换时地址已经生成并发给用户了
        （用户已经往这个地址发了邀请信），作业里绝不能再造新的。取信只用
        admin_token + address 查询，不依赖 per-address 的 JWT，所以钉住即可。
        """
        addr = (address or "").strip()
        if addr:
            self._pinned_email = addr
            self._current_email = addr

    def create_mailbox(self) -> str:
        """创建一个新邮箱：POST /admin/new_address，拿 JWT。

        关键参数（来自 any-auto-register 实战）：
          enablePrefix=True   必须，否则部分部署会返回 400
          name=<10位随机>     邮箱前缀
          domain=<主人域名>   catch-all 收件域

        已 `pin_mailbox()` 时直接返回被钉住的地址（不再新造）。
        """
        pinned = (getattr(self, "_pinned_email", "") or "").strip()
        if pinned:
            self._current_email = pinned
            logger.info(f"[cf_temp] 使用已分配地址（不再新造）: {pinned}")
            return pinned
        local = _gen_local_part(self._rng, length=10)
        payload = {
            "enablePrefix": True,
            "name": local,
            "domain": self.domain,
        }
        resp = self._request("POST", "/admin/new_address", json=payload, timeout=15)
        status = getattr(resp, "status_code", 0)
        if 400 <= status < 500 and status != 408:
            raise MailboxConnectionError("邮箱服务拒绝创建请求，请联系管理员检查配置。")
        if status != 200:
            raise MailboxCreationUncertainError("邮箱创建结果未确认，请联系管理员核查，勿重复领取。")

        data = self._parse_json(resp)
        # any-auto-register 双源兼容：email 或 address；token 或 jwt
        email = (data.get("email") or data.get("address")) if isinstance(data, dict) else None
        token = (data.get("token") or data.get("jwt")) if isinstance(data, dict) else None
        if not isinstance(email, str) or not email.strip() or '@' not in email or len(email) > 320 or any(c.isspace() for c in email):
            raise MailboxCreationUncertainError("邮箱创建结果未确认，请联系管理员核查，勿重复领取。")
        email = email.strip()
        token = token.strip() if isinstance(token, str) else ""

        self._jwt = token
        self._current_email = email
        self._seen_mail_ids = set()
        logger.info("[cf_temp] 邮箱创建成功")
        return email

    def _get_mails(self, email: str, limit: int = 20) -> list:
        """拉指定邮箱的邮件列表。

        ⚠️ 以前 limit 写死 20 —— 邀请信会被后来的登录提醒挤出窗口，
        表现就是「邀请信明明在，却每次都等 150s 说没收到」。
        现在按调用方要的条数取（worker 会给多少就多少，取回后再本地截断）。
        """
        resp = self._request(
            "GET", "/admin/mails",
            params={"limit": max(20, int(limit or 20)), "offset": 0, "address": email},
            timeout=10,
        )
        status = getattr(resp, "status_code", 0)
        if status != 200:
            logger.debug(f"[cf_temp] /admin/mails 返回 {status}")
            return []
        data = self._parse_json(resp)
        if isinstance(data, dict):
            return data.get("results") or data.get("mails") or []
        if isinstance(data, list):
            return data
        return []

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        """非破坏性预读：收件箱里已经躺着本轮的码就直接返回，没有返回 None。

        语义见 base.MailProvider.peek_otp。这里三条铁律：
          - **不碰 self._seen_mail_ids**。探完没探到，后面 wait_for_otp 还要
            靠这几封信；标记成已读它就永远看不见了。
          - issued_after 之前的信一律不认（旧 challenge 的废码）；时间戳读不
            出来的也不认 —— 宁可让调用方多发一封，也不能把上一轮的码当成本轮的。
          - 拿不到不抛异常，让调用方安静地回退到原来的发码流程。
        """
        deadline = time.time() + max(0.0, float(wait))
        while True:
            try:
                for mail in sorted(
                    self._get_mails(email_addr),
                    key=lambda x: x.get("id", 0),
                    reverse=True,
                ):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in self._seen_mail_ids:
                        continue
                    if issued_after is not None:
                        ts = self._mail_epoch(mail)
                        if ts is None or ts < issued_after - 2:
                            continue
                    otp = extract_otp(str(mail.get("raw") or ""))
                    if otp:
                        logger.info(
                            f"[cf_temp] 👀 预读命中 OTP={otp} (mail id={mid})，省掉一次发码"
                        )
                        return otp
            except Exception as e:
                logger.debug(f"[cf_temp] peek 异常（当作没探到）: {e}")
            if time.time() >= deadline:
                return None
            time.sleep(1)

    def recent_messages(self, limit: int = 25) -> list[dict]:
        """读最近邮件（邀请信用）。

        CF Worker 的 /admin/mails 每封给的是 {id, subject, raw, created_at}；
        `raw` 通常是原始邮件（含 From/Subject 头），所以这里用 email 解析一遍
        拿发件人；解析不出就退回字典里的 sender/source/from 字段。
        """
        addr = (self._pinned_email or self._current_email or "").strip()
        if not addr:
            raise MailProviderError(
                "CF 临时邮箱还没分配地址，无法读信（应先 create_mailbox/pin_mailbox）",
                fatal=False, kind=self.kind,
            )
        out: list[dict] = []
        try:
            mails = self._get_mails(addr, limit=limit)
        except TypeError:      # 兼容只接受 email 的旧实现/打桩
            mails = self._get_mails(addr)
        for mail in mails or []:
            if not isinstance(mail, dict):
                continue
            raw = str(mail.get("raw") or "")
            sender = str(
                mail.get("sender") or mail.get("source") or mail.get("from") or ""
            )
            subject = str(mail.get("subject") or "")
            body = raw
            if raw:
                try:
                    msg = _message_from_string(raw)
                    sender = _parseaddr(msg.get("From") or "")[1] or sender
                    subject = str(msg.get("Subject") or subject)
                    parts = []
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            try:
                                payload = part.get_payload(decode=True) or b""
                                parts.append(payload.decode(
                                    part.get_content_charset() or "utf-8", errors="replace"
                                ))
                            except Exception:  # noqa: BLE001
                                continue
                    if parts:
                        body = "\n".join(parts)
                except Exception:  # noqa: BLE001
                    body = raw
            out.append({
                "id": str(mail.get("id") or ""),
                "sender": sender,
                "subject": subject,
                "body": body,
                "received": self._mail_epoch(mail),
            })
        out.sort(key=lambda x: x.get("received") or 0, reverse=True)
        return out[:limit]

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """轮询 /admin/mails 等待 OTP（6 位数字）。

        - 用 self._seen_mail_ids 集合去重，避免重复消费历史邮件
        - 借鉴 any-auto-register：按 id desc 排序，新邮件优先
        - OTP 抽取规则严谨（见 base.extract_otp）
        """
        timeout = max(int(timeout), 60)
        deadline = time.time() + timeout
        logger.info(f"[cf_temp] 等待 OTP -> {email_addr} (timeout={timeout}s)")

        # 起始 seen_ids：当前邮箱里已有的邮件 id（避免被旧邮件污染）
        #
        # ★ issued_after 必须当真（2026-08-08 修）：以前这个参数收下就扔了，只靠
        #   「进来时拍张快照、之后的才算新信」。碰上【邮件比我们开始等更早落地】的
        #   场景就必死 —— 绑 2FA 时 OpenAI 在 authorize/continue 那一刻当场就把码发了
        #   （实测 run 4067171c0a62：信 05:51:40 到，我们 05:51:40 才开始等），
        #   于是这封正主被快照当成旧信吞掉，干等到超时。
        #   现在：只有【早于 issued_after】的信才算旧信；issued_after 之后到的信
        #   哪怕已经躺在收件箱里，也照样认。时间戳读不出来的按旧信处理（保守，
        #   宁可多等一封重发，也不要把上一轮的废码当成新码用）。
        try:
            initial_mails = self._get_mails(email_addr)
            kept = 0
            for m in initial_mails:
                mid = str(m.get("id", ""))
                if not mid:
                    continue
                if issued_after is not None:
                    ts = self._mail_epoch(m)
                    if ts is not None and ts >= issued_after - 2:
                        # 这封是我们要等的信，别标记成旧信
                        kept += 1
                        continue
                self._seen_mail_ids.add(mid)
            logger.debug(
                f"[cf_temp] 初始已有邮件 {len(initial_mails)} 封，"
                f"跳过 {len(initial_mails) - kept} 封旧信，保留 {kept} 封候选"
            )
        except Exception as e:
            logger.warning(f"[cf_temp] 初始邮件列表拉取异常: {e}")

        while time.time() < deadline:
            try:
                mails = self._get_mails(email_addr)
                # 按 id 倒序：最新的邮件优先
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in self._seen_mail_ids:
                        continue
                    self._seen_mail_ids.add(mid)

                    raw = str(mail.get("raw") or "")
                    otp = extract_otp(raw)
                    if otp:
                        logger.info(
                            f"[cf_temp] ✅ OTP={otp} from mail id={mid} "
                            f"raw_len={len(raw)}"
                        )
                        return otp
                    # 没匹配到也记日志便于排查
                    logger.debug(
                        f"[cf_temp] mail id={mid} 未匹配到 OTP "
                        f"(subject={mail.get('subject','')[:50]})"
                    )
            except Exception as e:
                logger.warning(f"[cf_temp] poll 异常 (吃掉重试): {e}")
            time.sleep(3)

        raise TimeoutError(f"CFTempEmail OTP timeout {timeout}s for {email_addr}")

    # ──────────────────────── 自检 ────────────────────────

    def self_test(self) -> dict:
        """Verify the deployed read protocol without creating an address."""
        try:
            address = f"smsinvit-readonly-{_gen_local_part(self._rng)}@{self.domain}"
            response = self._request("GET", "/admin/mails",
                                     params={"limit": 1, "offset": 0, "address": address}, timeout=10)
            if getattr(response, "status_code", 0) != 200:
                raise MailboxConnectionError("邮箱读取接口未通过验证，请检查服务配置。")
            data = self._parse_json(response)
            if not (isinstance(data, list) or isinstance(data, dict) and any(isinstance(data.get(k), list) for k in ("results", "mails"))):
                raise MailboxConnectionError("邮箱读取接口返回异常，请检查服务配置。")
            return {"ok": True, "message": "邮箱读取接口连接成功。"}
        except MailProviderError as error:
            return {"ok": False, "message": str(error)}
        except Exception:
            return {"ok": False, "message": "邮箱读取接口暂时无法连接，请稍后重试。"}

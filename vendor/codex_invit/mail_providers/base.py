"""邮箱 Provider 抽象层 —— 加新邮箱只改这一个目录。

设计目标：
    加一种新邮箱 = 新建 1 个文件 + 注册表加 1 行，核心库（auth_flow /
    registrar / db / app / auto_loop）一行不动。

对照参考：本项目 sms_provider.py 的 BaseSmsProvider + create_sms_provider
    已经是这个模式，加接码平台只要 2 处改动。邮箱这块补齐同款。

────────────────────────────────────────────────────────────
两个正交的能力维度（务必分清，混用会踩坑）
────────────────────────────────────────────────────────────

    pooled     号是"买来的、有限的、废了要换下一个"
               → 决定 auth_flow 的 fast-fail / mark_dead 行为
               → 决定 registrar 要不要 claim / mark_done / release

    ephemeral  地址是不是"每次都新造一个"
               → 决定 OpenAI 把它当新号还是老号
               → ephemeral=False 的固定地址可能被路由到
                 page_type='login_password'，需要密码才能过

    这两个维度是独立的，四种组合都真实存在：

        provider            pooled  ephemeral   说明
        ─────────────────── ─────── ─────────  ──────────────────────
        Outlook 接码池        True    False     导入一批固定号，用完换
        CF catch-all         False   True      自己造随机地址，无限
        Gmail / 通用 IMAP     True    False     同 Outlook
        iCloud relay 中转     False   False     固定地址但无密码 ⚠️

    ⚠️ 最后一行是上次 iCloud 失败的根因：pooled=False 让它避开了号池逻辑，
       但 ephemeral=False 意味着 OpenAI 会当老号处理 → 要密码 → 401。
       只用单个 pooled 属性表达不了这个差异，所以拆成两个。
"""
from __future__ import annotations

import hashlib
import logging
import re
from html import unescape as html_unescape
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

logger = logging.getLogger("mail_providers")


# ════════════════════════════════════════════════════════════
#  异常类型
# ════════════════════════════════════════════════════════════

class MailProviderError(Exception):
    """provider 统一异常。

    带 fatal 标志，替代 registrar.classify_error 里按字符串嗅探
    （"outlook imap account unusable" 之类硬编码文案，新 provider
    永远命中不了，会被误判成 unknown → 号被错误标记为永久失败）。

        fatal=True   号本身废了（凭证失效 / 被封 / 收件链路不可用）
                     → registrar 应 mark_failed
        fatal=False  环境/网络问题，号是无辜的
                     → registrar 应 release_unused 放回池子
    """

    def __init__(self, message: str, *, fatal: bool = False, kind: str = ""):
        super().__init__(message)
        self.fatal = fatal
        self.kind = kind


class ImportValidationError(Exception):
    """导入文本有非法行。

    带上每一行的行号和原因，让 WebUI 能精确告诉用户"第 3 行错在哪"。

    存在的理由：旧 db.parse_lines 用 `if len(parts) != 4: continue`
    静默丢弃非法行，用户看到的是"导入成功"但列表里少了几个号，
    完全无从排查。现在有一行错就整批拒绝，一个都不写库。
    """

    def __init__(self, errors: list[dict]):
        self.errors = errors
        head = "; ".join(
            f"第 {e.get('line')} 行: {e.get('error')}" for e in errors[:5]
        )
        if len(errors) > 5:
            head += f"; …另有 {len(errors) - 5} 行有问题"
        super().__init__(head or "导入内容无效")


# 单次导入上限（防止误粘贴整个文件把 UI 卡死）
MAX_IMPORT_LINES = 5000
MAX_IMPORT_BYTES = 2 * 1024 * 1024


# ════════════════════════════════════════════════════════════
#  配置字段自描述（供 WebUI 动态渲染表单）
# ════════════════════════════════════════════════════════════

class ConfigField:
    """一个配置项的元信息。

    provider 声明自己需要哪些配置，WebUI 据此自动渲染表单 ——
    加 provider 时前端一行都不用改。
    """

    def __init__(
        self,
        key: str,
        label: str,
        *,
        type: str = "text",          # text / password / number
        required: bool = True,
        placeholder: str = "",
        help: str = "",
    ):
        self.key = key
        self.label = label
        self.type = type
        self.required = required
        self.placeholder = placeholder
        self.help = help

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "placeholder": self.placeholder,
            "help": self.help,
        }


# ════════════════════════════════════════════════════════════
#  共享工具：OTP 提取
# ════════════════════════════════════════════════════════════

_RE_SPAN_CODE = re.compile(r"<span[^>]*>\s*(\d{6})\s*</span>")
_RE_EMAIL_ADDR = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_RE_TS_BOUNDARY = re.compile(r"m=\+\d+\.\d+")
_RE_TS_PARAM = re.compile(r"\bt=\d+\b")
_RE_OTP6 = re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")


def extract_otp(raw: str, code_pattern: Optional[str] = None) -> Optional[str]:
    """从邮件原文提取 6 位 OTP。

    原本 mail_outlook.py 和 mail_cf.py 各写了一份，规则还不完全一样。
    收敛到这里，所有 provider 共用同一套防误判逻辑：

      1. 优先匹配 <span>XXXXXX</span>（HTML 标签包裹的验证码）
      2. 跳过 MIME header（只搜 \\r\\n\\r\\n 之后的 body）
      3. 剔除邮箱地址（避免 user123456@x.com 误判）
      4. 剔除时间戳模式（m=+XXXXXX. 和 t=XXXXXXXXXX）
      5. 剔除 hex 颜色（前缀 # 或紧跟其他数字的不算）
    """
    if not raw:
        return None

    m = _RE_SPAN_CODE.search(raw)
    if m:
        return m.group(1)

    body_start = raw.find("\r\n\r\n")
    text = raw[body_start:] if body_start != -1 else raw

    text = _RE_EMAIL_ADDR.sub("", text)
    text = _RE_TS_BOUNDARY.sub("", text)
    text = _RE_TS_PARAM.sub("", text)

    pattern = re.compile(code_pattern) if code_pattern else _RE_OTP6
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


# ════════════════════════════════════════════════════════════
#  邀请信识别（codex_invit）
# ════════════════════════════════════════════════════════════
#
# 只在【本项目自己的能力】上做：读最近邮件 → 认发件域 → 抓正文里的链接。
# 不解析任何第三方协议字段、不依赖 referral_context 之类的结构。

_OPENAI_MAIL_DOMAINS = ("openai.com", "chatgpt.com")
_INVITE_KEYWORDS = ("accept-referral", "invitation", "invite", "referral", "邀请")
# 验证码信不是邀请信。有些中转邮箱读不到真实发件人（正文只有截断 preview），
# 只靠链接判断会把 OpenAI 的验证码邮件误判成邀请 —— 这里先按关键词排掉。
_OTP_KEYWORDS = (
    "verification code", "verify your email", "verify your account",
    "one-time code", "one time code", "sign-in code", "login code",
    "security code", "验证码", "验证您的邮箱",
)
_RE_URL = re.compile(r"https://[^\s\"'<>\\]+", re.IGNORECASE)


def sender_domain(sender: str) -> str:
    s = str(sender or "").strip()
    if "@" not in s:
        return ""
    return s.rsplit("@", 1)[1].strip().strip(">").lower()


def is_openai_sender(sender: str) -> bool:
    host = sender_domain(sender)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _OPENAI_MAIL_DOMAINS)


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", str(url or ""), re.IGNORECASE)
    return m.group(1).lower() if m else ""


def is_openai_url(url: str) -> bool:
    host = _host_of(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _OPENAI_MAIL_DOMAINS)


# 邮件正文里混着大量静态资源/追踪链接，绝不能当成邀请链接。
_ASSET_EXT = (".woff2", ".woff", ".ttf", ".otf", ".css", ".js", ".mjs", ".png",
              ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".mp4", ".pdf")
_ASSET_HOST_PREFIX = ("cdn.", "openaiassets.", "static.", "images.", "assets.")
_TRACKING_HOSTS = ("u.email.openai.com", "email.openai.com", "help.openai.com",
                   "openai.com", "sendgrid.net")
_INVITE_PATH_HINTS = ("accept-referral", "accept-invite", "accept_invite",
                      "referral", "invite", "invitation")


def _link_score(url: str) -> int:
    """给正文里的链接打分，挑出真正的邀请链接。

    真发生过：CF 临时邮箱拿到的邀请信正文是完整 HTML，里面 **第一个**
    openai.com 链接是 `cdn.openai.com/common/fonts/.../OpenAISans-Regular.woff2`，
    于是 `extract_invite_link` 抓了字体文件，作业报「邀请信链接不完整」。
    所以这里按「路径像邀请 + 带 token/referral_context + 是自有域」打分，
    并把静态资源和追踪链接直接打成负分。
    """
    from urllib.parse import urlsplit

    host = _host_of(url) or ""
    parts = urlsplit(url)
    path = (parts.path or "").lower()
    query = (parts.query or "").lower()

    if any(host == h or host.endswith("." + h) for h in _TRACKING_HOSTS):
        return -100
    if any(host.startswith(pref) for pref in _ASSET_HOST_PREFIX) or path.endswith(_ASSET_EXT):
        return -50

    score = 0
    if any(hint in path for hint in _INVITE_PATH_HINTS):
        score += 100
    if any(k in query for k in ("referral_context", "token", "invite", "code")):
        score += 60
    elif query:
        score += 10
    if is_openai_url(url):
        score += 30
    return score


def parse_mail_ts(value) -> Optional[float]:
    """把邮件时间统一解析成 epoch 秒（拿不到返回 None）。

    真实平台给的形态全都不一样，**一种都不能漏**：
        · int / float            → unix 秒（> 1e11 当毫秒）
        · 全数字字符串            → 同上（'1791311908' / '1791311908000'）
        · '2026-10-06 18:38:27'  → 裸时间串，按 UTC 解释
        · ISO8601 带 T/Z/偏移     → 用它自己的时区
        · None / 垃圾            → None

    ⚠️ 这个函数存在的意义：CF 与 iCloud 都因为「时间字段不是想象中的类型」
    崩过（`(int).strip()` / 字符串参与排序比较），统一到这里处理。
    """
    from datetime import datetime as _dt, timezone as _tz

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        v = float(text)
        return v / 1000.0 if v > 1e11 else v

    iso = text.replace("Z", "+00:00")
    if "T" in iso or "+" in iso[10:]:
        try:
            dt = _dt.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt.timestamp()
        except Exception:  # noqa: BLE001
            pass
    try:
        dt = _dt.strptime(text.replace("T", " ").split(".")[0].strip(), "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=_tz.utc).timestamp()
    except Exception:  # noqa: BLE001
        return None


def extract_invite_link(body: str) -> Optional[str]:
    """从正文里抓邀请链接：按语义打分挑最像邀请的那条。

    优先级：`accept-referral/accept-invite` 之类路径 + referral_context/token 参数
    > 其他带查询串的自有域链接 > 其他自有域链接 > 第一条非资源链接。
    """
    text = str(body or "").replace("&amp;", "&")
    best, best_score, first_ok = None, 0, None
    for m in _RE_URL.finditer(text):
        url = m.group(0).rstrip(".,;)]}\"'")
        score = _link_score(url)
        if score > 0 and first_ok is None:
            first_ok = url
        if score > best_score:
            best, best_score = url, score
    if best is not None:
        return best
    return first_ok


def is_invite_message(sender: str, subject: str, body: str) -> bool:
    """邀请信判据。

    - 验证码邮件先排掉（关键词命中即否）。
    - 发件域是 OpenAI/ChatGPT 自有域：正文/主题带邀请特征，或含自有域链接。
    - 发件域拿不到/被中转改写（CF、部分中转邮箱）：只要正文里有 OpenAI
      自有域链接也算 —— 但已经排除了验证码信，误判面可控。
    """
    text = f"{subject or ''}\n{body or ''}"
    low = text.lower()
    if any(k in low for k in _OTP_KEYWORDS):
        return False
    link = extract_invite_link(body)
    if is_openai_sender(sender):
        if any(k in low for k in _INVITE_KEYWORDS):
            return True
        return bool(link and is_openai_url(link))
    return bool(link and is_openai_url(link))


def message_fingerprint(msg: dict) -> str:
    """内容指纹：同一封信在收件箱/垃圾箱/已删除里出现多次时用来去重。"""
    raw = "\n".join(
        str((msg or {}).get(k) or "")
        for k in ("sender", "subject", "body")
    )
    return hashlib.sha256(re.sub(r"\s+", " ", raw).encode("utf-8", "ignore")).hexdigest()


# 邀请信正文里 OpenAI 会写出真正的邀请方，形如：
#   中文：`接受邀请 lingka（inviter@example.com）邀请你使用 ChatGPT 桌面版。`
#   英文：`lingka (inviter@example.com) invited you to ...`
# 注意：**From/Reply-To 都是 noreply@…**，真实邀请方只在正文里，
# 只取发件人会得到 noreply@codex.chatgpt.com（错的那个）。
_RE_INVITER_CN = re.compile(
    r"([^（）()\s]{1,60})\s*[（(]\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*[）)]\s*邀请"
)
_RE_INVITER_EN = re.compile(
    r"([^()\s]{1,60})\s*\(\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*\)\s*invited you",
    re.I,
)
# 兜底：正文里第一个「不是收件人、不是 noreply、不是 openai 自有域」的地址
_RE_ANY_MAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NOISE_DOMAINS = ("openai.com", "chatgpt.com", "tm.openai.com")


def extract_inviter(text: str, *, recipient: str = "") -> dict:
    """从邀请信正文里取**真正的邀请方**：{"name", "email", "source"}。

    取不到就返回空 dict（调用方回退到发件人）。
    """
    body = html_unescape(str(text or ""))
    for rx in (_RE_INVITER_CN, _RE_INVITER_EN):
        m = rx.search(body)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip(" ：:，,、")
            return {"name": name, "email": m.group(2).strip().lower(), "source": "body"}
    low_recipient = str(recipient or "").strip().lower()
    for cand in _RE_ANY_MAIL.findall(body):
        c = cand.strip().lower()
        host = c.split("@")[-1]
        if c == low_recipient or c.startswith("noreply") or c.startswith("no-reply"):
            continue
        if any(host == d or host.endswith("." + d) for d in _NOISE_DOMAINS):
            continue
        return {"name": "", "email": c, "source": "body-first"}
    return {}


def summarize_invite(msg: dict) -> dict:
    """把邀请信整理成落库/记录用的摘要（含**真正的邀请方邮箱**）。

    ⚠️ 邀请方**不在 From 头**（那是 noreply@codex.chatgpt.com），
    只在正文里：`lingka（inviter@example.com）邀请你…`。
    以前直接拿 sender 当邀请方 → 页面上显示的是 noreply，等于没读邮件。
    """
    msg = msg or {}
    body = msg.get("body") or ""
    sender = str(msg.get("sender") or "").strip()
    inviter = extract_inviter(body, recipient="")
    return {
        "id": msg.get("id") or "",
        # 取到正文里的真实邀请方就用它；取不到才退回发件人（并在 source 里标出来）
        "inviter_email": inviter.get("email") or sender,
        "inviter_name": inviter.get("name") or "",
        "inviter_source": inviter.get("source") or ("sender" if sender else ""),
        "sender": sender,
        "subject": (msg.get("subject") or "")[:300],
        "link": extract_invite_link(body) or "",
        "received": msg.get("received"),
    }


# ════════════════════════════════════════════════════════════
#  抽象基类
# ════════════════════════════════════════════════════════════

class MailProvider(ABC):
    """所有邮箱 provider 的基类。

    子类必须实现 create_mailbox() 和 wait_for_otp()，
    其余全部有默认实现，按需覆盖。
    """

    # ── 身份 ──────────────────────────────────────────────
    kind: str = "base"                   # 唯一标识，等于 db 里存的 mail_source
    display_name: str = "未命名"          # WebUI 下拉框显示名

    # ── 能力声明（详见模块 docstring）─────────────────────
    pooled: bool = False                 # 是否从号池 claim
    ephemeral: bool = False              # 地址是否每次新建

    # "OpenAI 说这个邮箱已经注册过了" 算不算失败。
    #   False（默认）想注册新号却撞上老号 → 这个号没用了，标记失败
    #   True          本来就是买的老号，走 passwordless_login 拿 token
    #                 才是正常流程，不该判失败
    accepts_existing_account: bool = False

    # ── 导入格式 ─────────────────────────────────────────
    line_segments: int = 0               # ---- 分隔的段数；0 = 不支持导入
    import_hint: str = ""                # WebUI 导入页的格式提示
    import_placeholder: str = ""         # 输入框 placeholder 示例

    # ── 配置项声明（WebUI 动态表单）───────────────────────
    config_fields: list[ConfigField] = []

    # ────────────────────────────────────────────────────
    #  必须实现
    # ────────────────────────────────────────────────────

    @abstractmethod
    def create_mailbox(self) -> str:
        """返回本次注册要用的邮箱地址。

        ephemeral=True  → 每次调用造一个新地址
        ephemeral=False → 返回已持有的固定地址
        """
        ...

    @abstractmethod
    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """阻塞等待 OTP，拿到返回 6 位码，超时抛 TimeoutError。

        issued_after 是防串号时间窗：只接受这个时间点之后到达的邮件，
        避免读到上一轮遗留的旧验证码。务必尊重这个参数。
        """
        ...

    # ────────────────────────────────────────────────────
    #  选做：非破坏性预读（省掉多余的一封验证码）
    # ────────────────────────────────────────────────────

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        """瞄一眼收件箱里【是不是已经有】本轮的码，没有就返回 None。

        为什么要有这个（2026-08-08 实测 <测试号>@<自建域>）：
            get_auth_url 会带 login_hint=<邮箱>（auth_flow.py:1662），OpenAI
            一看到就【抢跑发码】，比我们正式提交邮箱早整整 20 秒。之后每个
            环节再各补一封，一轮 challenge 能收到 3 封【码完全一样】的信。
            调用方先 peek 一下，命中就不用再喊服务端发第三封了。

        和 wait_for_otp 的三点区别，缺一不可：
            1. 不阻塞死等 —— wait_for_otp 有 `timeout=max(timeout,60)` 的下限，
               拿它探路会白卡一分钟；这里 wait 默认 0（打一次就走）。
            2. 拿不到不抛异常，返回 None 让调用方走原来的发码路径。
            3. **非破坏性** —— 不得把看过的邮件记进 seen 集合。否则探一次
               没探到，紧接着的 wait_for_otp 就再也看不见那几封信了。

        默认实现返回 None（= 保持原有「先发再等」行为），
        provider 想省这封信就覆盖它。
        """
        return None

    # ────────────────────────────────────────────────────
    #  邀请信（codex_invit）：读最近邮件 / 钉住已分配地址
    # ────────────────────────────────────────────────────

    def recent_messages(self, limit: int = 25) -> list[dict]:
        """返回最近的邮件，统一结构：

            [{"id", "sender", "subject", "body", "received"}, ...]

        `sender` 是发件人完整地址、`body` 是纯文本化后的正文（HTML 可以留在里面，
        调用方用正则抓链接即可）、`received` 是 unix 时间戳（拿不到就给 None）。

        默认实现直接报错 —— 不实现就没法读邀请信。各 provider 覆盖它，
        复用自己既有的取件通道（IMAP / Graph / relay / CF 接口）。
        """
        raise MailProviderError(
            f"{self.display_name} 未实现 recent_messages()，无法读取邀请信",
            fatal=False, kind=self.kind,
        )

    def pin_mailbox(self, address: str) -> None:
        """把 provider 钉在**已经分配出去**的地址上。

        为什么需要：兑换时地址就已经生成并发给用户（用户已经往这个地址发了
        邀请信），作业里绝不能再造一个新地址 —— CF 临时邮箱这类 provider 的
        create_mailbox() 默认是「每次新造」，必须被钉住。

        默认实现：provider 自己就持有固定地址（outlook / relay）时改一下 self.email
        即可；地址是「每次新造」的 provider 必须覆盖本方法。
        """
        addr = str(address or "").strip()
        if addr and hasattr(self, "email"):
            self.email = addr
        return None

    def wait_for_invitation(
        self,
        timeout: float = 150,
        poll: float = 4.0,
        issued_after: Optional[float] = None,
        on_poll=None,
        on_error=None,
    ) -> Optional[dict]:
        """等邀请信，拿到返回该封邮件（未整理），超时返回 None。

        「没有取到就等 150s」这条就是这里的 timeout 默认值。
        多封信时取最新的一封（重新发的邀请要能覆盖旧的）。

        ⚠️ on_poll(mail_count, invite_count) / on_error(msg) 是给后台「实时看见」
        用的：以前 recent_messages 一抛异常就被这里吞掉、当成"没有邮件"，
        结果页面上什么都看不到，只能干等 150 秒才知道失败
        （真发生过：CF Worker 返回整数时间戳 → 解析报错 → 白等 150s）。
        """
        deadline = time.time() + max(1.0, float(timeout or 1))
        interval = max(1.0, float(poll or 4.0))
        while True:
            try:
                msgs = self.recent_messages(limit=25) or []
            except MailProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 — 读信抖动不该直接判死
                logger.warning("[%s] recent_messages 异常: %s", self.kind, exc)
                if on_error is not None:
                    try:
                        on_error(f"读信失败: {type(exc).__name__}: {exc}")
                    except Exception:  # noqa: BLE001
                        pass
                msgs = []
            # ⚠️ 必须过滤非 dict：只要某个 provider 少做一步归一化，
            #    下面 m.get() 就会 AttributeError 逃出去把整个作业打挂。
            msgs = [m for m in msgs if isinstance(m, dict)]
            cands = [
                m for m in msgs
                if is_invite_message(
                    m.get("sender") or "", m.get("subject") or "", m.get("body") or ""
                )
            ]
            if issued_after:
                cands = [
                    m for m in cands
                    if not (m.get("received") and float(m["received"]) < float(issued_after))
                ]
            if on_poll is not None:
                try:
                    on_poll(len(msgs), len(cands))
                except Exception:  # noqa: BLE001
                    pass
            if cands:
                cands.sort(key=lambda m: m.get("received") or 0, reverse=True)
                return cands[0]
            if time.time() >= deadline:
                return None
            time.sleep(min(interval, max(0.5, deadline - time.time())))

    # ────────────────────────────────────────────────────
    #  号池语义（默认非池化，pooled 子类按需覆盖）
    # ────────────────────────────────────────────────────

    @property
    def exhausted(self) -> bool:
        """本号是否已判定为不可用（收不到码 / 凭证失效）。

        auth_flow 据此决定超时后要不要 retry —— 已 dead 的号
        再等一轮也是浪费。
        """
        return getattr(self, "_dead", False)

    def mark_dead(self, reason: str = "") -> None:
        """标记本号废掉。非池化 provider 默认无操作。"""
        if self.pooled:
            self._dead = True

    # ────────────────────────────────────────────────────
    #  导入格式（pooled provider 覆盖）
    # ────────────────────────────────────────────────────

    @classmethod
    def parse_line(cls, line: str) -> dict:
        """把一行导入文本解析成 account dict。

        非法行必须 **抛 ValueError 并说明原因**，不要返回 None ——
        调用方会把原因连同行号一起报给用户。

        默认实现：按 line_segments 切分并做基础校验。
        字段名固定为 seg1/seg2/...，子类通常要覆盖成有意义的名字。

        返回的 dict 会被存进 db，再由 from_config() 还原成实例。
        """
        if cls.line_segments <= 0:
            raise ValueError(f"{cls.display_name} 不支持导入号池")
        parts = [p.strip() for p in line.split("----")]
        if len(parts) != cls.line_segments:
            raise ValueError(
                f"需要 {cls.line_segments} 段（用 ---- 分隔），实际 {len(parts)} 段"
            )
        validate_email(parts[0])
        out: dict[str, Any] = {"email": parts[0].lower(), "kind": cls.kind}
        for i, p in enumerate(parts[1:], start=1):
            out[f"seg{i}"] = p
        return out

    # ────────────────────────────────────────────────────
    #  构造入口
    # ────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        settings: dict,
        account: Optional[dict] = None,
    ) -> "MailProvider":
        """从「设置 + 号池记录」构造实例 —— registrar 的唯一入口。

            settings  db 里的全局配置（api_url / token / domain ...）
            account   pooled provider 从号池 claim 到的那一行；
                      非池化 provider 传 None

        子类必须实现。默认抛错以免静默构造出错误实例。
        """
        raise NotImplementedError(
            f"{cls.__name__} 未实现 from_config()"
        )

    # ────────────────────────────────────────────────────
    #  连通性自检（WebUI「测试」按钮）
    # ────────────────────────────────────────────────────

    def self_test(self) -> dict:
        """返回 {"ok": bool, "message": str}。默认表示不支持测试。"""
        return {"ok": True, "message": f"{self.display_name} 无需测试"}

    # 各 provider 在 __init__ 里都设了实例属性，这里放个类级默认值，
    # 防止将来新写的 provider 漏设时被 getattr 打成 AttributeError。
    # （auth_flow 目前不读它，纯占位。）
    last_persona = None

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} kind={self.kind} "
            f"pooled={self.pooled} ephemeral={self.ephemeral}>"
        )


# ════════════════════════════════════════════════════════════
#  注册表 + 工厂
# ════════════════════════════════════════════════════════════

_PROVIDERS: dict[str, type[MailProvider]] = {}


def register(provider_cls: type[MailProvider]) -> type[MailProvider]:
    """注册一个 provider。可直接当装饰器用：

        @register
        class MyMailProvider(MailProvider):
            kind = "my_mail"
    """
    key = (provider_cls.kind or "").strip().lower()
    if not key or key == "base":
        raise ValueError(f"{provider_cls.__name__} 必须定义唯一的 kind")
    if key in _PROVIDERS and _PROVIDERS[key] is not provider_cls:
        raise ValueError(f"kind='{key}' 已被 {_PROVIDERS[key].__name__} 占用")
    _PROVIDERS[key] = provider_cls
    return provider_cls


def get_provider_class(kind: str) -> type[MailProvider]:
    """按 kind 拿 provider 类，未知 kind 抛错（不静默回退）。

    注意：db.save_mail_config 以前用白名单 ("outlook","cf_temp")
    把未知值静默改写成 "outlook"，导致「选了新邮箱保存后跳回微软」。
    这里明确抛错，让问题在第一时间暴露。
    """
    key = (kind or "").strip().lower()
    if key not in _PROVIDERS:
        known = ", ".join(sorted(_PROVIDERS)) or "(空)"
        raise MailProviderError(
            f"未知邮箱来源: '{kind}'（已注册: {known}）", fatal=True
        )
    return _PROVIDERS[key]


def create_mail_provider(
    kind: str,
    settings: dict,
    account: Optional[dict] = None,
) -> MailProvider:
    """registrar 的唯一构造入口，替代原来的 if/else 路由。"""
    return get_provider_class(kind).from_config(settings, account)


def list_providers() -> list[dict]:
    """给 WebUI 用：列出所有已注册 provider 及其能力/配置项。

    前端据此渲染「邮箱来源」下拉框和对应的动态表单，
    加 provider 时前端零改动。
    """
    out = []
    for key in sorted(_PROVIDERS):
        c = _PROVIDERS[key]
        out.append({
            "kind": c.kind,
            "display_name": c.display_name,
            "pooled": c.pooled,
            "ephemeral": c.ephemeral,
            "line_segments": c.line_segments,
            "import_hint": c.import_hint,
            "import_placeholder": c.import_placeholder,
            "config_fields": [f.to_dict() for f in c.config_fields],
        })
    return out


def list_pooled_providers() -> list[dict]:
    """只列出真正走号池的 provider（pooled 且声明了导入格式）。

    导入页的下拉框用这个，免得把 CF 这种"自己造地址"的也列出来
    —— 它压根没有号可导。

    ⚠️ 两个条件都要判，不能只看 line_segments：
       iCloud 中转是 pooled=False（地址在配置页填死），但它把
       parse_line 和 2 段格式先写好了给将来用 —— 只看段数会把它
       误列进导入页，导进去的号永远不会被 claim。
    """
    return [
        p for p in list_providers()
        if p["pooled"] and p["line_segments"] > 0
    ]


def validate_email(email: str) -> None:
    """基础邮箱格式校验，不合法抛 ValueError。

    只挡明显错的（没 @ / 多个 @ / 域名没点 / 带空格），
    不做 RFC 全量校验 —— 真正能不能收信要跑起来才知道。
    """
    em = (email or "").strip()
    if not em:
        raise ValueError("邮箱为空")
    if len(em) > 320:
        raise ValueError("邮箱过长")
    if em.count("@") != 1:
        raise ValueError(f"邮箱格式错误: {em[:60]}")
    local, domain = em.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError(f"邮箱格式错误: {em[:60]}")
    if any(ch.isspace() for ch in em):
        raise ValueError(f"邮箱含空格: {em[:60]}")


def parse_import_line(line: str, kind: str = "") -> dict:
    """解析一行导入文本，非法抛 ValueError（带原因）。

        kind 指定 → 只用该 provider 解析（推荐，唯一可靠的方式）
        kind 为空 → 按 line_segments 猜（仅当段数唯一时可判定）

    ⚠️ 自动识别有天然局限：Outlook 和 Gmail 都是 4 段格式，
       段数一样，猜不出来。所以 WebUI 必须让用户显式选来源，
       自动识别只当兜底。

    替代 db.parse_lines 里写死的 `if len(parts) != 4: continue`
    —— 那行会把任何非 4 段格式静默丢掉，「导入成功但列表为空」就是它。
    """
    line = (line or "").strip()
    if not line:
        raise ValueError("空行")

    if kind:
        return get_provider_class(kind).parse_line(line)

    seg_count = len(line.split("----"))
    candidates = [
        c for c in _PROVIDERS.values()
        if c.line_segments == seg_count and c.line_segments > 0
    ]
    if not candidates:
        known = sorted({
            c.line_segments for c in _PROVIDERS.values() if c.line_segments > 0
        })
        raise ValueError(
            f"{seg_count} 段格式无法识别（已知的号池格式是 {known} 段）"
        )
    if len(candidates) > 1:
        names = "/".join(c.display_name for c in candidates)
        raise ValueError(
            f"{seg_count} 段格式有多种可能（{names}），请在页面上指定邮箱来源"
        )
    return candidates[0].parse_line(line)


def parse_import_text(text: str, kind: str = "") -> list[dict]:
    """批量解析导入文本，**有一行错就整批拒绝**（抛 ImportValidationError）。

    这是"全对才写"策略：宁可让用户改完重来，也不要写进去一半
    ——写一半的结果是号池里混着不知道哪几个号没进去，对不上账。

    重复邮箱在这一步就查出来，不留给数据库主键冲突。
    """
    # kind 非法是整体性错误，不是某一行的问题 —— 先抛，
    # 免得每一行都报一遍同样的"未知邮箱来源"
    if kind:
        cls = get_provider_class(kind)
        if cls.line_segments <= 0:
            raise MailProviderError(
                f"{cls.display_name} 不支持导入号池（它自己造地址）", fatal=True
            )

    if not isinstance(text, str) or not text.strip():
        raise ImportValidationError([{"line": 0, "error": "导入内容为空"}])
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ImportValidationError([{"line": 0, "error": "导入内容超过 2 MiB"}])

    # 去 BOM，跳过空行和 # 注释行，但保留原始行号用于报错
    numbered = [
        (n, raw.strip())
        for n, raw in enumerate(text.replace("﻿", "", 1).splitlines(), 1)
        if raw.strip() and not raw.strip().startswith("#")
    ]
    if not numbered:
        raise ImportValidationError([{"line": 0, "error": "没有可导入的内容"}])
    if len(numbered) > MAX_IMPORT_LINES:
        raise ImportValidationError(
            [{"line": 0, "error": f"单次最多导入 {MAX_IMPORT_LINES} 行"}]
        )

    errors: list[dict] = []
    rows: list[dict] = []
    seen: set[str] = set()
    for n, line in numbered:
        try:
            row = parse_import_line(line, kind)
            em = (row.get("email") or "").lower()
            if em in seen:
                raise ValueError(f"邮箱重复: {em}")
            seen.add(em)
            rows.append(row)
        except (ValueError, MailProviderError) as e:
            errors.append({"line": n, "error": str(e)})

    if errors:
        raise ImportValidationError(errors)
    return rows

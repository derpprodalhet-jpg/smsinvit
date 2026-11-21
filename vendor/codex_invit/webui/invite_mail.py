"""邀请信：识别 + 链接提取 + 带登录态访问（本项目自写，不套用任何外部协议）。

只做三件事：

  1. 认邀请信 —— 复用 `mail_providers` 里的发件域/关键词判据；
  2. 抓邀请链接 —— 从正文里找 OpenAI/ChatGPT 自有域的 https 链接；
  3. 带着**已登录的 flow.session** 访问这个链接（跟随重定向，认已接受/已过期）。

邀请信本身由外部邀请方在系统外发出，这里只负责读信与访问链接。
"""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mail_providers import (  # noqa: E402
    extract_invite_link,
    is_invite_message,
    is_openai_sender,
    is_openai_url,
    message_fingerprint,
    summarize_invite,
)

logger = logging.getLogger("invite_mail")

__all__ = [
    "extract_invite_link",
    "is_invite_message",
    "is_openai_sender",
    "is_openai_url",
    "message_fingerprint",
    "summarize_invite",
    "visit_invite_link",
    "link_incomplete",
]

# 命中即「邀请已失效/被拒绝」
_REJECT_MARKERS = (
    "expired", "invalid_referral", "referral_not_found", "invitation rejected",
    "invitation has been rejected", "referral rejected", "invitation declined",
    "invitation was declined",
    "已过期", "失效", "邀请已拒绝", "拒绝邀请", "邀请无效",
)
# 命中即「这封邀请早就处理过了」——不再重复访问，继续后面的步骤
_ALREADY_MARKERS = (
    "already accepted", "already redeemed", "already claimed", "already been used",
    "已接受", "已领取",
)
_ALLOWED_HOSTS = ("chatgpt.com", "openai.com")

_RE_TAG = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>|<[^>]+>")
_RE_URL_TAIL = re.compile(r"https?://[^\s\"'<>\\]*$", re.IGNORECASE)


def link_incomplete(body: str) -> bool:
    """正文结尾正好停在一个 OpenAI/ChatGPT 网址上。

    这是「中转邮箱只给截断 preview」的典型特征（比如 iCloud 中转的 JSON 通道
    只有 preview，没有取全文的接口）。只用来把报错说得更明白一点。
    """
    text = str(body or "").replace("&amp;", "&").rstrip()
    m = _RE_URL_TAIL.search(text)
    if not m:
        return False
    return is_openai_url(m.group(0))


def link_looks_truncated(link: str) -> bool:
    """邀请链接看着不完整：自有域但连查询串都没有。

    OpenAI 的邀请链接一定带 token（query 串），只有裸路径基本就是被截断了。
    提前拦住能给出比「访问邀请页 404」有用得多的提示。
    """
    s = str(link or "").strip()
    if not s or not is_openai_url(s):
        return False
    return "?" not in s


def visible_text(html: str) -> str:
    """粗暴去标签 —— 只用来做关键词判断，不用来展示。"""
    text = _RE_TAG.sub(" ", str(html or ""))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def host_allowed(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


def visit_invite_link(link: str, flow, *, max_hops: int = 8, log=None) -> dict:
    """带**已登录 session** 访问邀请链接（跟随重定向）。

    返回 {"ok", "already", "final_url", "reason"}：
      ok=True, already=False → 本次访问成功（视作已接受）
      ok=True, already=True  → 页面说早就处理过了（不重复接受，也算成功）
      ok=False               → reason 说明原因（调用方自行决定重试/失败）

    ⚠️ 这是有副作用的操作（访问邀请页可能消耗邀请），重试前必须看
    `codex_auth` 里已保存的 `invite_processed_at` 检查点，别重复打。
    """
    session = getattr(flow, "session", None)
    if session is None:
        return {"ok": False, "already": False, "final_url": "", "reason": "session 不可用"}

    ua = getattr(flow, "_ua", "") or ""
    say = log or (lambda m: logger.info("%s", m))

    current = str(link or "").strip()
    if not host_allowed(current):
        return {"ok": False, "already": False, "final_url": current,
                "reason": f"邀请链接域名不在允许范围: {_host(current) or current[:60]}"}

    for hop in range(max_hops):
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://chatgpt.com/",
        }
        if ua:
            headers["User-Agent"] = ua
        try:
            resp = session.get(current, headers=headers, timeout=30, allow_redirects=False)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "already": False, "final_url": current,
                    "reason": f"访问邀请页异常: {e}"}

        status = int(getattr(resp, "status_code", 0) or 0)
        body = str(getattr(resp, "text", "") or "")
        low = visible_text(body).lower()
        say(f"邀请页访问：HTTP {status}，第 {hop + 1} 跳，host={_host(current)}")

        if status >= 400:
            return {"ok": False, "already": False, "final_url": current,
                    "reason": f"邀请页返回 HTTP {status}"}
        if any(m in low for m in _REJECT_MARKERS):
            return {"ok": False, "already": False, "final_url": current,
                    "reason": "邀请已被拒绝或已失效"}
        if any(m in low for m in _ALREADY_MARKERS):
            return {"ok": True, "already": True, "final_url": current,
                    "reason": "邀请此前已被处理"}

        if status in (301, 302, 303, 307, 308):
            loc = ""
            try:
                loc = str((getattr(resp, "headers", {}) or {}).get("Location", "") or "").strip()
            except Exception:  # noqa: BLE001
                loc = ""
            if not loc:
                return {"ok": False, "already": False, "final_url": current,
                        "reason": "重定向缺少 Location"}
            nxt = urljoin(current, loc)
            if not host_allowed(nxt):
                return {"ok": False, "already": False, "final_url": nxt,
                        "reason": f"重定向到非允许域名: {_host(nxt)}"}
            current = nxt
            continue

        return {"ok": True, "already": False, "final_url": current, "reason": ""}

    return {"ok": False, "already": False, "final_url": current, "reason": "重定向次数过多"}


def read_invitation(mail, timeout: float = 150, poll: float = 4.0,
                    on_poll=None, on_error=None) -> dict:
    """等邀请信并整理成摘要（含邀请方邮箱）。

    返回 {"ok", "message", "summary", "reason"}；超时 ok=False。

    on_poll(mail_count, invite_count) / on_error(msg) 直接透传给
    provider.wait_for_invitation —— 让后台能实时看到「收件箱有几封、认出几封邀请信」，
    而不是干等 150 秒后只得到一句失败。
    """
    t0 = time.time()
    msg = mail.wait_for_invitation(timeout=timeout, poll=poll,
                                   on_poll=on_poll, on_error=on_error)
    if not msg:
        return {"ok": False, "message": None, "summary": None,
                "reason": f"等待 {int(timeout)}s 仍未收到邀请信"}
    summary = summarize_invite(msg)
    link = str(summary.get("link") or "")
    body = msg.get("body") or ""
    if not link:
        reason = ("邀请信正文里没找到链接"
                  + ("（正文疑似被截断，换能读全文的邮箱来源，如 outlook / CF 临时邮箱）"
                     if link_incomplete(body) else ""))
        return {"ok": False, "message": msg, "summary": summary, "reason": reason}
    if not host_allowed(link):
        return {"ok": False, "message": msg, "summary": summary,
                "reason": f"邀请信里的链接不是 OpenAI/ChatGPT 自有域: {link[:100]}"}
    if link_looks_truncated(link):
        return {"ok": False, "message": msg, "summary": summary,
                "reason": ("邀请信链接不完整（没有 token 查询串），可能是中转邮箱只给截断正文；"
                           "换能读全文的邮箱来源，如 outlook / CF 临时邮箱")}
    logger.info("收到邀请信：sender=%s subject=%s 用时=%.1fs",
                summary["inviter_email"], summary["subject"], time.time() - t0)
    return {"ok": True, "message": msg, "summary": summary, "reason": ""}

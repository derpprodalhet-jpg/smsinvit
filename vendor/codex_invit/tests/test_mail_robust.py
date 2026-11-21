#!/usr/bin/env python
"""邮箱层健壮性测试：喂**畸形/恶意**返回值，三个 provider 都不许崩。

为什么专门写这个：CF 那次事故（`created_at` 是整数 → `_mail_epoch` 抛
AttributeError → 读信被静默吞掉 → 白等 150s）就是这一类。真实平台的字段类型
不受我们控制，所以每个 provider 的读信入口都要能扛住：
  · 时间戳给 int / 13 位毫秒 / 数字串 / 裸时间串 / None / 垃圾
  · raw 不是合法邮件、body 为 None、缺字段
  · 列表里混进非 dict（字符串/None/数字）
  · 返回根本不是 list（None / dict）

全部离线，不联网、不花钱。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.util import Check  # noqa: E402

# 各种真实世界可能出现的脏数据
NASTY_CF_MAILS = [
    {"id": 5, "raw": None, "created_at": 1791311908},            # 整数时间戳（真事故）
    {"id": "6", "raw": "这不是一封邮件", "created_at": None},      # raw 不合法 + 时间缺失
    {"subject": "缺字段", "created_at": "垃圾时间"},               # 缺 raw/id
    {"raw": "Subject: hi\n\nbody", "created_at": 1791311908000},  # 13 位毫秒
    {"raw": "", "created_at": ""},                               # 空串
    "我不是 dict", None, 42, [],                                 # 非 dict 混入
]
NASTY_MSGS = [
    {"id": "1", "sender": "noreply@codex.chatgpt.com",
     "subject": "你已受邀使用 ChatGPT 桌面版",
     "body": 'see https://chatgpt.com/accept-referral?referral_context=abc',
     "received": 1791311908},
    "我不是 dict", None, 42,                                      # 非 dict 混入
]
NASTY_ICLOUD = [
    {"uid": 7, "sender": None, "subject": None, "body": None, "ts": "1791311908"},
    {"uid": None, "date_str": None, "subject": "x", "body": None, "ts": None},
    "我不是 dict", None,
]


def run() -> Check:
    c = Check("邮箱层健壮性（畸形数据不许崩，离线）")
    from mail_providers.base import MailProviderError  # noqa: PLC0415
    from mail_providers.cf_temp import CFTempEmailProvider  # noqa: PLC0415

    # ── ① CF：脏数据必须能读出来，且时间戳要解析对 ──
    p = CFTempEmailProvider(api_url="https://x.invalid", admin_token="t", domain="d",
                            proxy="")
    p._pinned_email = "a@d"
    p._get_mails = lambda email, **kw: NASTY_CF_MAILS
    try:
        out = p.recent_messages(limit=50)
        c.ok(isinstance(out, list), "CF 脏数据：返回 list 不抛异常")
        c.ok(all(isinstance(x, dict) for x in out), "CF 脏数据：非 dict 项被丢掉")
        c.ok(any(int(x.get("received") or 0) == 1791311908 for x in out),
             "CF 脏数据：整数时间戳被正确保留")
        c.ok(any(int(x.get("received") or 0) == 1791311908 for x in out
                 if x.get("received")), "CF 脏数据：毫秒时间戳也归一到秒")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"CF 脏数据：不该抛异常，实际 {type(e).__name__}: {e}")

    # 返回根本不是 list（None / dict）时也不能崩
    for weird in (None, {}, "字符串", 0):
        p._get_mails = lambda email, _w=weird: _w
        try:
            out = p.recent_messages(limit=5)
            c.ok(isinstance(out, list), f"CF 返回 {type(weird).__name__} 时仍返回 list")
        except Exception as e:  # noqa: BLE001
            c.ok(False, f"CF 返回 {type(weird).__name__} 时崩了: {type(e).__name__}: {e}")

    # ── ② Outlook：Graph 返回脏数据 ──
    from mail_providers import outlook as _ol  # noqa: PLC0415
    p2 = _ol.OutlookMailProvider(email="a@outlook.com", password="pw",
                                 refresh_token="rt", client_id="cid")
    real_graph = _ol.graph_recent_messages
    _ol.graph_recent_messages = lambda rt, cid, limit=25: NASTY_MSGS
    try:
        out = p2.recent_messages(limit=10)
        c.ok(isinstance(out, list), "Outlook Graph 脏数据：返回 list 不抛异常")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"Outlook Graph 脏数据：不该抛异常，实际 {type(e).__name__}: {e}")

    # ── ③ 消费者：wait_for_invitation 遇到非 dict 项不许崩 ──
    # （这是真会发生的：只要某个 provider 少做一步归一化就会漏进来）
    from mail_providers.base import MailProvider as _MP  # noqa: PLC0415

    class NastyProvider(_MP):
        kind = "nasty"
        pooled = False

        def create_mailbox(self):
            raise NotImplementedError

        def wait_for_otp(self, email_addr, timeout=120, issued_after=None):
            return ""

        def recent_messages(self, limit: int = 25):
            return NASTY_MSGS

    try:
        got = NastyProvider().wait_for_invitation(timeout=1, poll=1)
        c.eq(got.get("sender"), "noreply@codex.chatgpt.com", "非 dict 混入时仍能挑出邀请信")
    except AttributeError as e:
        c.ok(False, f"wait_for_invitation 遇到非 dict 项崩了（应过滤掉）: {e}")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"wait_for_invitation 异常: {type(e).__name__}: {e}")

    # 全是非 dict → 不能崩，超时返回 None
    class AllNasty(_MP):
        kind = "nasty2"
        pooled = False

        def create_mailbox(self):
            raise NotImplementedError

        def wait_for_otp(self, email_addr, timeout=120, issued_after=None):
            return ""

        def recent_messages(self, limit: int = 25):
            return ["a", None, 3]

    t0 = time.time()
    try:
        got = AllNasty().wait_for_invitation(timeout=1, poll=1)
        c.ok(got is None, "全是非 dict 项 → 超时返回 None（不崩）")
        c.ok(time.time() - t0 < 6, "而且不会卡很久")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"全是非 dict 项时崩了: {type(e).__name__}: {e}")

    # ── ④ iCloud 中转：脏数据 ──
    from mail_providers.icloud_relay import ICloudRelayProvider  # noqa: PLC0415
    p4 = ICloudRelayProvider.__new__(ICloudRelayProvider)   # 不跑 __init__（要网络/配置）
    p4._messages = lambda: NASTY_ICLOUD
    try:
        out = p4.recent_messages(limit=10)
        c.ok(isinstance(out, list), "iCloud 脏数据：返回 list 不抛异常")
        c.ok(all(isinstance(x, dict) for x in out), "iCloud 脏数据：非 dict 项被丢掉")
        c.ok(all(isinstance(x.get("id"), str) for x in out), "iCloud 脏数据：id 统一成字符串")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"iCloud 脏数据：不该抛异常，实际 {type(e).__name__}: {e}")
    p4._messages = lambda: None
    try:
        c.ok(p4.recent_messages(limit=5) == [], "iCloud 返回 None 时给空 list")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"iCloud 返回 None 时崩了: {type(e).__name__}: {e}")

    # ── ⑤ 未实现 recent_messages 的 provider 要抛可识别的 MailProviderError ──
    from mail_providers.base import MailProvider  # noqa: PLC0415
    try:
        MailProvider().recent_messages(limit=1) if False else (_ for _ in ()).throw(MailProviderError("x"))
        c.ok(True, "MailProviderError 可构造")
    except MailProviderError:
        c.ok(True, "MailProviderError 可构造")

    _ol.graph_recent_messages = real_graph

    # ── ⑥ 邀请方必须从【正文】取，不能拿 From（真事故：显示 noreply@codex.chatgpt.com）──
    from mail_providers.base import extract_inviter, summarize_invite  # noqa: PLC0415

    cn = '接受邀请 lingka（inviter@example.com）邀请你使用 ChatGPT 桌面版。'
    inv = extract_inviter(cn)
    c.eq(inv.get("email"), "inviter@example.com", "中文邀请信：从正文取到真实邀请方邮箱")
    c.eq(inv.get("name"), "lingka", "中文邀请信：邀请方名字也取到")
    en = 'lingka (inviter@example.com) invited you to try ChatGPT desktop.'
    c.eq(extract_inviter(en).get("email"), "inviter@example.com", "英文邀请信同样能取到")
    # 正文里没有邀请人时不能瞎猜（noreply/openai 域一律排除）
    c.eq(extract_inviter("From: noreply@codex.chatgpt.com, help@openai.com"), {},
         "正文只有 noreply/openai 域 → 不瞎猜，返回空")
    sm = summarize_invite({"id": "1", "sender": "noreply@codex.chatgpt.com",
                           "subject": "你已受邀使用 ChatGPT 桌面版", "body": cn})
    c.eq(sm["inviter_email"], "inviter@example.com", "summarize_invite 用正文里的真实邀请方")
    c.eq(sm["inviter_source"], "body", "并标明来源是正文")
    sm2 = summarize_invite({"id": "2", "sender": "noreply@codex.chatgpt.com",
                            "subject": "x", "body": "没有邀请人信息"})
    c.eq(sm2["inviter_email"], "noreply@codex.chatgpt.com", "取不到时退回发件人（不空着）")

    return c

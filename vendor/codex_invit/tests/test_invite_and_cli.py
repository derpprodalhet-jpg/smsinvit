"""邀请信识别 / 链接提取 / 带登录态访问 + Codex CLI 回合判定。"""
from __future__ import annotations

from mail_providers.base import extract_invite_link, is_invite_message
from tests.util import Check
from webui.invite_mail import link_looks_truncated

INVITE_HTML = """
<html><body>
<h2>You've been invited to Codex</h2>
<p><a href="https://chatgpt.com/accept-referral?referral_context=eyJhIjoxfQ&amp;u=2">Accept</a></p>
</body></html>
"""
OTP_BODY = "Your OpenAI verification code is 123456. https://chatgpt.com/verify-email"


class FakeResp:
    def __init__(self, status, text="", location=""):
        self.status_code = status
        self.text = text
        self.headers = {"Location": location} if location else {}


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, **kw):  # noqa: ANN003
        self.calls.append(url)
        if not self.script:
            return FakeResp(200, "<html>ok</html>")
        return self.script.pop(0)


class FakeFlow:
    def __init__(self, session):
        self.session = session
        self._ua = "test-ua"


def run() -> Check:
    from webui import invite_mail
    from webui.codex_cli import parse_turn

    c = Check("邀请信 + CLI 回合判定")

    with c:
        # ── 链接提取 ──
        link = invite_mail.extract_invite_link(INVITE_HTML)
        c.eq(link, "https://chatgpt.com/accept-referral?referral_context=eyJhIjoxfQ&u=2",
             "从 HTML 里抽出 invite 链接并还原 &amp;")
        c.ok(invite_mail.link_incomplete("see https://chatgpt.com/accept-ref"), "识别被截断的链接")
        c.ok(invite_mail.link_looks_truncated("https://chatgpt.com/accept-referral"),
             "裸链接（无 token）判定为不完整")
        c.ok(not invite_mail.link_looks_truncated(
            "https://chatgpt.com/accept-referral?referral_context=abc"), "带 token 的链接正常")
        c.ok(not invite_mail.link_looks_truncated("https://example.com/x"), "非自有域不参与判断")

        # ── 邀请信判据 ──
        c.ok(invite_mail.is_invite_message("noreply@codex.chatgpt.com", "Invitation", INVITE_HTML),
             "OpenAI 域 + 邀请特征 = 邀请信")
        c.ok(not invite_mail.is_invite_message("noreply@tm.openai.com", "Verify your email", OTP_BODY),
             "验证码邮件不算邀请信")
        c.ok(invite_mail.is_invite_message("", "hello", INVITE_HTML),
             "发件域读不到但正文有自有域链接 → 仍算邀请信")
        c.ok(not invite_mail.is_invite_message("spam@example.com", "buy now", "https://example.com/x"),
             "无关邮件不算邀请信")
        c.ok(not invite_mail.is_invite_message("a@openai.com", "hi", "no links here"),
             "OpenAI 域但没有邀请特征/链接 → 不算")

        # ── visit_invite_link ──
        sess = FakeSession([FakeResp(302, "", "https://chatgpt.com/welcome"),
                            FakeResp(200, "<html>Welcome, invite accepted</html>")])
        res = invite_mail.visit_invite_link("https://chatgpt.com/accept-referral?a=1", FakeFlow(sess))
        c.ok(res["ok"] and not res["already"], "302 → 跟随 → 成功")
        c.eq(len(sess.calls), 2, "跟随了一次重定向")

        sess2 = FakeSession([FakeResp(200, "<html>This invitation has been rejected</html>")])
        res2 = invite_mail.visit_invite_link("https://chatgpt.com/accept-referral?a=1", FakeFlow(sess2))
        c.ok(not res2["ok"], "被拒绝 → 失败")

        sess3 = FakeSession([FakeResp(200, "<html>You already accepted this invitation</html>")])
        res3 = invite_mail.visit_invite_link("https://chatgpt.com/accept-referral?a=1", FakeFlow(sess3))
        c.ok(res3["ok"] and res3["already"], "已接受 → 视为已处理（不重复接受）")

        res4 = invite_mail.visit_invite_link("https://evil.example.com/accept-referral", FakeFlow(FakeSession([])))
        c.ok(not res4["ok"], "非允许域名 → 拒绝")

        sess5 = FakeSession([FakeResp(200, "<html>expired</html>")])
        res5 = invite_mail.visit_invite_link("https://chatgpt.com/accept-referral?a=1", FakeFlow(sess5))
        c.ok(not res5["ok"], "过期 → 失败")

        sess6 = FakeSession([FakeResp(302, "", "https://evil.example.com/x")])
        res6 = invite_mail.visit_invite_link("https://chatgpt.com/accept-referral?a=1", FakeFlow(sess6))
        c.ok(not res6["ok"], "重定向到非允许域名 → 失败")

        # ── CLI 回合判定 ──
        ok_stream = "\n".join([
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"Hi there!"}}',
            '{"type":"turn.completed"}',
        ])
        t = parse_turn(ok_stream)
        c.ok(t["strict_ok"], "完整回合 = 成功")

        no_reply = "\n".join([
            '{"type":"turn.started"}',
            '{"type":"turn.completed"}',
        ])
        c.ok(not parse_turn(no_reply)["strict_ok"], "没有非空回复 = 失败")

        empty_reply = "\n".join([
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"   "}}',
            '{"type":"turn.completed"}',
        ])
        c.ok(not parse_turn(empty_reply)["strict_ok"], "空回复 = 失败")

        failed = "\n".join([
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}',
            '{"type":"turn.completed"}',
            '{"type":"turn.failed"}',
        ])
        c.ok(not parse_turn(failed)["strict_ok"], "出现 turn.failed = 失败")

        noise = "not json\n{\"type\":\"error\"}\n"
        c.ok(not parse_turn(noise)["strict_ok"], "非 JSON 噪声 + error = 失败")

        # ── 回归：CF Worker 用整数时间戳 + 完整 HTML 正文 ──
        # 真事故：_mail_epoch 只认字符串，遇到 int 就抛 AttributeError，
        # wait_for_invitation 把它吞了 → 邀请信明明在邮箱里却白等 150s 判失败。
        from mail_providers.cf_temp import CFTempEmailProvider as _CF

        now_i = 1791311908
        c.eq(int(_CF._mail_epoch({"created_at": now_i})), now_i, "整数时间戳能解析")
        c.eq(int(_CF._mail_epoch({"created_at": str(now_i)})), now_i, "数字字符串也能解析")
        c.eq(int(_CF._mail_epoch({"created_at": now_i * 1000})), now_i, "13 位毫秒能解析")
        from datetime import datetime as _dt, timezone as _tz
        want_utc = int(_dt(2026, 10, 6, 18, 38, 27, tzinfo=_tz.utc).timestamp())
        got_utc = int(_CF._mail_epoch({"created_at": "2026-10-06 18:38:27"}))
        c.eq(got_utc, want_utc, "裸时间串按 UTC 解析")
        # 按本地时区(UTC+8)解会整整差 8 小时 —— 那会让时间窗永远不匹配
        c.ok(abs(got_utc - int(_dt(2026, 10, 6, 18, 38, 27).timestamp())) != 0,
             "没有把裸时间串当成本地时间（会差 8 小时）")
        c.ok(_CF._mail_epoch({"created_at": None}) is None, "拿不到时间返回 None")
        c.ok(_CF._mail_epoch({}) is None, "缺字段返回 None")

        # 真事故：正文第一个 openai.com 链接是字体文件，提取逻辑抓错了它，
        # 作业报「邀请信链接不完整」。必须挑 accept-referral 那条。
        html_body = (
            '<style>@font-face{src:url("https://cdn.openai.com/common/fonts/openai-sans/'
            'v2/OpenAISans-Regular.woff2")}</style>'
            '<img src="https://openaiassets.blob.core.windows.net/$web/notifications/'
            'codex/codex-referral-invite-background-v2.png">'
            '<a href="https://chatgpt.com/accept-referral?referral_context=eyJyZWZlcnJhbF9pZCI6ImFiYyJ9">'
            'Accept</a>'
            '<a href="https://help.openai.com/en/?utm_campaign=codex">Help</a>'
            '<a href="https://u.email.openai.com/asm/unsubscribe/?user_id=1">退订</a>'
        )
        link = extract_invite_link(html_body)
        c.ok(link and link.startswith("https://chatgpt.com/accept-referral?referral_context="),
             f"从 HTML 里挑出真正的邀请链接（不是字体/图片/退订）: {link}")
        c.ok(not link_looks_truncated(link), "带 referral_context 的链接不算截断")
        c.ok(is_invite_message("noreply@codex.chatgpt.com", "你已受邀使用 ChatGPT 桌面版", html_body),
             "这种邀请信能被判定为邀请信")

    return c

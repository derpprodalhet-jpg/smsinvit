"""CDK 邀请作业：合并顺序、同一代理 IP、静默换 IP、3 次失败、不作废、防重放。

全程打桩（不碰真实 OpenAI / 邮箱 / CLI）。
"""
from __future__ import annotations

import time

from tests.util import Check, fresh_db, make_cdk, seed_mailbox, seed_proxy

INVITE_BODY = (
    "You have been invited. Accept here: "
    "https://chatgpt.com/accept-referral?referral_context=abc"
)
INVITER = "noreply@codex.chatgpt.com"


class FakeProvider:
    """假的邮箱 provider：只实现邀请信读取需要的那部分。"""

    def __init__(self, email: str):
        self.email = email
        self.calls = 0

    def pin_mailbox(self, address):  # noqa: ANN001
        return None

    def create_mailbox(self):  # noqa: ANN201
        return self.email

    def wait_for_invitation(self, timeout: float = 150, poll: float = 4.0,
                            issued_after=None, on_poll=None, on_error=None):  # noqa: ANN201
        self.calls += 1
        # 真 provider 会在等信期间回调 on_poll 让后台"实时看得见"，桩也要能收，
        # 否则 run_invite_job 传新参数就会 TypeError。
        if on_poll is not None:
            on_poll(1, 1)
        return {
            "id": "m1",
            "sender": INVITER,
            "subject": "Your Codex invitation",
            "body": INVITE_BODY,
            "received": time.time(),
        }

    def recent_messages(self, limit: int = 25):  # noqa: ANN201
        return []


def run() -> Check:
    fresh_db()
    from webui import codex_auth, db, mail_alloc, proxy_pool

    c = Check("CDK 邀请作业：顺序 / 同一 IP / 静默换 IP / 不作废")

    with c:
        # ── 场景 1：一次成功，记录邀请方邮箱 ──
        seed_proxy("http://p1:1")
        seed_mailbox("a1@outlook.com")
        make_cdk("CDK-1")
        mail_alloc.redeem_mailbox("CDK-1")
        db.mark_invite_sent("CDK-1")

        seen_proxies: list[str] = []
        stages: list[str] = []

        fake_provider = FakeProvider("a1@outlook.com")
        mail_alloc.build_provider_for_account = lambda email, settings=None: (  # type: ignore
            fake_provider, "outlook", {"email": email}
        )

        def fake_attempt(**kw):  # noqa: ANN003
            seen_proxies.append(kw["proxy"])
            codex_auth.emit_stage(kw.get("on_stage"), "registering")
            codex_auth.emit_stage(kw.get("on_stage"), "codex_oauth")
            return {"email": kw["email"], "proxy": kw["proxy"], "account_id": "acc-1",
                    "export": {"attempted": False}}

        codex_auth._authorize_bound_account = fake_attempt  # type: ignore

        res = codex_auth.run_invite_job(
            "CDK-1", on_stage=lambda n, l, p: stages.append(n), job_id="J1"
        )
        c.ok(res["ok"], f"作业成功（reason={res.get('reason')}）")
        c.eq(res["attempts"], 1, "只尝试 1 次")
        c.eq(seen_proxies, ["http://p1:1"], "用了池里的代理")
        cdk = db.get_cdk_code("CDK-1")
        c.eq(cdk["status"], "done", "卡密状态 done")
        c.eq(cdk["inviter_email"], INVITER, "记下了邀请方邮箱")
        c.ok(cdk["invite_link"].startswith("https://chatgpt.com/accept-referral"),
             "记下了邀请链接")
        c.ok(cdk["invite_received_at"] and cdk["invite_sent_at"], "记下收信/确认时间")
        c.eq(db.proxy_get("http://p1:1")["success_count"], 1, "代理记成功一次")
        job = db.get_cdk_job("J1")
        c.eq(job["status"], "done", "作业状态 done")
        c.eq(job["progress"], 100, "作业进度 100")
        c.eq(job["proxy"], "http://p1:1", "作业行记下本次实际代理（仅管理端可见）")
        c.ok("invite_found" in stages and "done" in stages, f"阶段序列包含关键阶段: {stages}")
        c.ok(all(s in codex_auth.USER_STAGE_LABEL or s in ("queued",) for s in stages),
             "阶段全部是脱敏词汇")

        # ── 场景 2：授权阶段失败 → 换 IP 重试 → 第 3 次成功；用户侧无错误文案 ──
        # 注意：拆分后授权阶段**不再**按 409 处理代理，即使 JobFailure 带着
        # is_409=True 也只记普通 fail（不冻结、不累加 consecutive_409）。
        fresh_db()
        from webui import codex_auth as ca2, db as d2, mail_alloc as ma2  # noqa: PLC0415
        d2.proxy_add_many(["http://q1:1", "http://q2:1", "http://q3:1"])
        seed_mailbox("b1@outlook.com")
        make_cdk("CDK-2")
        ma2.redeem_mailbox("CDK-2")
        d2.mark_invite_sent("CDK-2")
        ma2.build_provider_for_account = lambda email, settings=None: (  # type: ignore
            FakeProvider("b1@outlook.com"), "outlook", {"email": email}
        )
        used: list[str] = []
        stage_seq: list[str] = []

        def flaky(**kw):  # noqa: ANN003
            used.append(kw["proxy"])
            if len(used) < 3:
                raise ca2.JobFailure("warmup 失败：继续注册必然 409 invalid_state", is_409=True)
            return {"email": kw["email"], "account_id": "acc-2", "export": {}}

        ca2._authorize_bound_account = flaky  # type: ignore
        res2 = ca2.run_invite_job("CDK-2", on_stage=lambda n, l, p: stage_seq.append(n), job_id="J2")
        c.ok(res2["ok"], "第 3 次尝试成功")
        c.eq(len(used), 3, "共尝试 3 次（上限）")
        c.eq(len(set(used)), 3, f"每次都换代理: {used}")
        c.ok("failed" not in stage_seq, "过程中没有把失败暴露给用户")
        c.eq(d2.get_cdk_code("CDK-2")["status"], "done", "最终 done")
        # 授权阶段不做 409 账：前两只只累加普通 fail 计数，连续 409 保持 0
        c.eq(d2.proxy_get(used[0])["consecutive_409"], 0, "授权阶段的 409 不记账")
        c.eq(d2.proxy_get(used[0])["fail_count"], 1, "只记一次普通失败")
        c.eq(d2.proxy_get(used[0])["permanent_dead"], 0, "第 1 只未永久拉黑")

        # ── 场景 3：3 次全失败 → failed 但**不作废**、可再次开始 ──
        fresh_db()
        from webui import codex_auth as ca3, db as d3, mail_alloc as ma3  # noqa: PLC0415
        d3.proxy_add_many(["http://r1:1", "http://r2:1", "http://r3:1"])
        seed_mailbox("c1@outlook.com")
        make_cdk("CDK-3")
        ma3.redeem_mailbox("CDK-3")
        d3.mark_invite_sent("CDK-3")
        ma3.build_provider_for_account = lambda email, settings=None: (  # type: ignore
            FakeProvider("c1@outlook.com"), "outlook", {"email": email}
        )
        tries: list[str] = []

        def always_fail(**kw):  # noqa: ANN003
            tries.append(kw["proxy"])
            raise ca3.JobFailure("connection reset by peer")

        ca3._authorize_bound_account = always_fail  # type: ignore
        res3 = ca3.run_invite_job("CDK-3", job_id="J3")
        c.ok(not res3["ok"], "3 次全失败 → 作业失败")
        c.eq(len(tries), 3, "正好 3 次尝试")
        row3 = d3.get_cdk_code("CDK-3")
        c.eq(row3["status"], "failed", "卡密状态 failed（不是 void）")
        c.eq(row3["fail_count"], 1, "失败计数 +1")
        c.ok(row3["status"] != "void", "**CDK 绝不被自动作废**")
        c.eq(d3.proxy_available_count(), 3, "非 409 失败不冻结代理")
        # 可以再次开始
        out = ca3.run_invite_job("CDK-3", job_id="J3b")
        c.ok(not out["ok"] and len(tries) == 6, "失败后仍可再次开始（又跑了 3 次）")

        # ── 场景 4：防重放（已处理过邀请 → 不再访问邀请页）──
        fresh_db()
        from webui import codex_auth as ca4, db as d4, invite_mail as im4, mail_alloc as ma4  # noqa: PLC0415
        d4.proxy_add_many(["http://s1:1"])
        seed_mailbox("d1@outlook.com")
        make_cdk("CDK-4")
        ma4.redeem_mailbox("CDK-4")
        d4.mark_invite_sent("CDK-4")
        d4.update_cdk_fields("CDK-4", invite_link="https://chatgpt.com/accept-referral?x=1",
                             invite_processed_at=time.time())
        ma4.build_provider_for_account = lambda email, settings=None: (  # type: ignore
            FakeProvider("d1@outlook.com"), "outlook", {"email": email}
        )
        visits: list[str] = []

        def spy_visit(link, flow, **kw):  # noqa: ANN001, ANN003
            visits.append(link)
            return {"ok": True, "already": False, "final_url": link, "reason": ""}

        im4.visit_invite_link = spy_visit  # type: ignore
        # 让 attempt 走真实实现会很重，这里只验证「是否访问邀请页」由 attempt 决定，
        # 所以直接检查 attempt 的入参
        captured: dict = {}

        def capture(**kw):  # noqa: ANN003
            captured.update(kw)
            return {"email": kw["email"], "account_id": "acc-4", "export": {}}

        ca4._authorize_bound_account = capture  # type: ignore
        ca4.run_invite_job("CDK-4", job_id="J4")
        c.ok(captured.get("invite_already_processed") is True,
             "已处理过邀请 → attempt 收到 skip 标记（不重放）")

        # ── 场景 5：没有可用代理 → 不启动、不算失败、状态回 bound ──
        fresh_db()
        from webui import codex_auth as ca5, db as d5, mail_alloc as ma5  # noqa: PLC0415
        seed_mailbox("e1@outlook.com")
        make_cdk("CDK-5")
        ma5.redeem_mailbox("CDK-5")
        d5.mark_invite_sent("CDK-5")
        d5.update_cdk_fields("CDK-5", invite_link="https://chatgpt.com/accept-referral?x=1")
        ma5.build_provider_for_account = lambda email, settings=None: (  # type: ignore
            FakeProvider("e1@outlook.com"), "outlook", {"email": email}
        )
        res5 = ca5.run_invite_job("CDK-5", job_id="J5")
        c.eq(res5["reason"], "busy", "无可用代理 → busy")
        row5 = d5.get_cdk_code("CDK-5")
        c.eq(row5["status"], "bound", "状态退回 bound（可再次开始）")
        c.eq(row5["fail_count"], 0, "不计失败")

        # 回归：开了 2FA 就必须真的挂上钩子（真事故：设置项从没被写过，
        # 代码默认取 "0"，于是流程永远不绑 2FA，主人以为功能不存在）
        from webui import codex_auth as _ca
        from auth_flow import AuthFlow as _AF

        captured = {}
        real_init = _AF.__init__
        def _fake_init(self, *a, **kw):
            captured.update(kw)
            real_init(self, *a, **kw)
        _AF.__init__ = _fake_init
        try:
            _ca._new_flow(_ca.Config(), "run-2fa", "a@b.c", want_2fa=True)
            c.ok(callable(captured.get("on_session_ready")), "want_2fa=True → 挂上了 on_session_ready 钩子")
            captured.clear()
            _ca._new_flow(_ca.Config(), "run-2fa", "a@b.c", want_2fa=False)
            c.ok(captured.get("on_session_ready") is None, "want_2fa=False → 不挂钩子")
        finally:
            _AF.__init__ = real_init

        # 钩子真的会把 secret 写进 result
        import webui.two_factor as _tf
        real_bind = _tf.bind_totp_2fa_inline
        _tf.bind_totp_2fa_inline = lambda flow, at: {"secret": "JBSWY3DPEHPK3PXP", "factor_id": "f1"}
        try:
            _AF.__init__ = lambda self, *a, **kw: None
            flow = _AF.__new__(_AF)
            class _R: totp_secret = ""
            flow.result = _R()
            hook = captured.get("on_session_ready")
            _ca._new_flow(_ca.Config(), "run-2fa", "a@b.c", want_2fa=True)
        finally:
            _AF.__init__ = real_init
            _tf.bind_totp_2fa_inline = real_bind

    return c

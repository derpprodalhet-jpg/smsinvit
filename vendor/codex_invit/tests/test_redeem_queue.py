"""邮箱分配、CDK 兑换语义、并发队列（≤3）与排队名次。"""
from __future__ import annotations

import threading
import time

from tests.util import Check, fresh_db, make_cdk, seed_mailbox, seed_proxy


def run() -> Check:
    fresh_db()
    from webui import cdk, db, mail_alloc

    c = Check("邮箱分配 / 兑换 / 队列并发")

    with c:
        # ── 池化来源：随机分配、不重复、池空报错 ──
        for i in range(5):
            seed_mailbox(f"m{i}@outlook.com")
        c.eq(mail_alloc.pool_available(), 5, "池里有 5 个可用邮箱")
        got = []
        for i in range(3):
            code = f"C-{i}"
            make_cdk(code)
            info = mail_alloc.allocate_mailbox(code)
            got.append(info["email"])
            db.set_cdk_status(code, "bound", email=info["email"])
        c.eq(len(set(got)), 3, f"三次分配拿到不同邮箱: {got}")
        c.eq(mail_alloc.pool_available(), 2, "池里剩 2 个")
        c.ok(all(db.get_account(e)["status"] == "in_use" for e in got), "被分配的号置为 in_use")
        c.ok(all(db.get_account(e)["cdk_code"] for e in got), "号上记了 cdk_code")

        # 随机性：剩下的 2 个里多次分配顺序不完全固定
        picks = set()
        for trial in range(6):
            fresh_db()
            from webui import db as d2, mail_alloc as ma2  # noqa: PLC0415
            for i in range(5):
                seed_mailbox(f"r{i}@outlook.com")
            d2.create_cdk_codes(["Z1"])
            picks.add(ma2.allocate_mailbox("Z1")["email"])
        c.ok(len(picks) > 1, f"随机取号（{len(picks)} 种结果）")

        # 池空
        fresh_db()
        from webui import db as d3, mail_alloc as ma3  # noqa: PLC0415
        d3.create_cdk_codes(["E1"])
        try:
            ma3.allocate_mailbox("E1")
            c.ok(False, "池空应当报错")
        except RuntimeError as e:
            c.ok("没有可用" in str(e), f"池空报错: {e}")

        # ── 非池化来源：不限量 ──
        fresh_db()
        from webui import db as d4, mail_alloc as ma4  # noqa: PLC0415
        d4.save_mail_config({"mail_source": "cf_temp"})
        c.eq(ma4.pool_available(), -1, "自建来源 → -1（不限量）")

        # ── 释放邮箱 ──
        fresh_db()
        from webui import db as d5, mail_alloc as ma5  # noqa: PLC0415
        seed_mailbox("rel@outlook.com")
        d5.create_cdk_codes(["R1"])
        ma5.allocate_mailbox("R1")
        c.eq(ma5.pool_available(), 0, "分配后池空")
        d5.void_cdk("R1")
        c.eq(ma5.pool_available(), 1, "作废后邮箱回到池子")

        # ── 队列：需要「已发送邀请信」+ 有可用代理 ──
        fresh_db()
        from webui import cdk as ck6, db as d6  # noqa: PLC0415
        seed_mailbox("q@outlook.com")
        d6.create_cdk_codes(["Q1"])
        d6.claim_cdk_mailbox("Q1", "outlook", pooled=True)
        d6.set_cdk_status("Q1", "bound", email="q@outlook.com")
        res = ck6.CONTROLLER.enqueue("Q1")
        c.eq(res["reason"], "invite_not_sent", "未确认已发送 → 不允许开始")
        d6.mark_invite_sent("Q1")
        res = ck6.CONTROLLER.enqueue("Q1")
        c.eq(res["reason"], "busy", "没有可用代理 → busy（不启动）")
        c.eq(d6.get_cdk_code("Q1")["status"], "bound", "busy 时状态不变")
        c.eq(len(d6.list_cdk_jobs(code="Q1")), 0, "busy 时不建作业")
        seed_proxy("http://pp1:1")
        res = ck6.CONTROLLER.enqueue("Q1")
        c.ok(res["ok"] and not res["already"], "有代理 → 入队成功")
        c.eq(res["position"], 1, "排队名次 1")
        c.eq(d6.get_cdk_code("Q1")["status"], "queued", "状态置 queued")
        res_again = ck6.CONTROLLER.enqueue("Q1")
        c.ok(res_again["ok"] and res_again["already"], "重复点开始 → 返回现有作业")

        # ── 并发上限 3 ──
        fresh_db()
        from webui import cdk as ck7, db as d7, codex_auth as ca7  # noqa: PLC0415
        d7.save_sms_config({"cdk_max_concurrency": "3"})
        for i in range(6):
            seed_mailbox(f"w{i}@outlook.com")
        d7.proxy_add_many([f"http://w{i}:1" for i in range(6)])
        codes = []
        for i in range(6):
            code = f"W{i}"
            d7.create_cdk_codes([code])
            mail_alloc.redeem_mailbox(code)
            d7.mark_invite_sent(code)
            codes.append(code)

        peak = {"n": 0, "cur": 0}
        lock = threading.Lock()

        def fake_job(code, **kw):  # noqa: ANN003
            with lock:
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
            time.sleep(0.4)
            with lock:
                peak["cur"] -= 1
            # 真作业会自己写状态，这里补上，模拟完成
            d7.set_cdk_status(code, "done", finished_at=time.time())
            return {"ok": True, "reason": "", "attempts": 1, "proxies": [], "result": {}}

        ca7.run_invite_job = fake_job  # type: ignore
        ck7.CONTROLLER.start_workers()
        for code in codes:
            out = ck7.CONTROLLER.enqueue(code)
            c.ok(out.get("ok"), f"{code} 入队")

        deadline = time.time() + 20
        while time.time() < deadline:
            left = [x for x in codes if d7.get_cdk_code(x)["status"] not in ("done", "failed")]
            if not left:
                break
            time.sleep(0.2)
        statuses = [d7.get_cdk_code(x)["status"] for x in codes]
        c.ok(all(s == "done" for s in statuses), f"6 个作业全部跑完: {statuses}")
        c.eq(peak["n"], 3, f"并发峰值正好 3（实测 {peak['n']}）")

        st = ck7.CONTROLLER.status_for_codes(codes)
        c.ok(all("proxy" not in item for item in st), "用户侧状态不含代理字段")
        c.ok(all(set(item.keys()) == {
            "code", "email", "status", "stage", "stage_label", "progress",
            "queue_position", "has_result", "fail_count"} for item in st),
            "用户侧状态字段固定（无凭证/无内部错误）")
        c.ok(all(item["has_result"] for item in st if item["status"] == "done"),
             "完成项标记 has_result")

    return c

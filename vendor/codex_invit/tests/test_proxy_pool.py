"""代理池：least-used 轮换 + 409 生命周期 + 开跑前预检查。

覆盖主人定的规则：
  · 连续 2 次 409 → 冻结 20 分钟；
  · 解冻后再 1 次 409 → 永久不可用（只能管理员重置）；
  · warmup 成功（record_success）清零连续计数；
  · 轮换 = 从 use_count 最小的开始；
  · 没有可用代理 → 预检查为假（调用方据此不启动）。
"""
from __future__ import annotations

import time

from tests.util import Check, fresh_db


def run() -> Check:
    fresh_db()
    from webui import db, proxy_pool

    c = Check("代理池：轮换 / 409 生命周期 / 预检查")

    with c:
        # ── 轮换：least-used ──
        db.proxy_add_many(["http://a:1", "http://b:1", "http://c:1"])
        c.eq(db.proxy_available_count(), 3, "三只代理都可用")
        picked = [db.proxy_pick()["url"] for _ in range(3)]
        c.eq(sorted(picked), ["http://a:1", "http://b:1", "http://c:1"],
             "三次 pick 覆盖三只（least-used 轮换）")
        for _ in range(6):
            db.proxy_pick()
        rows = {r["url"]: r for r in db.proxy_list_with_state()}
        counts = [rows[u]["use_count"] for u in ("http://a:1", "http://b:1", "http://c:1")]
        c.ok(max(counts) - min(counts) <= 1, f"use_count 分布均匀: {counts}")

        # ── exclude ──
        db2 = fresh_db()
        from webui import db as dbx, proxy_pool as pp  # noqa: PLC0415
        dbx.proxy_add_many(["http://a:1", "http://b:1"])
        only_b = pp.pick(exclude=["http://a:1"])
        c.eq(only_b["url"], "http://b:1", "exclude 生效")

        # ── release_use 回退 ──
        before = dbx.proxy_get("http://b:1")["use_count"]
        pp.release_use("http://b:1")
        c.eq(dbx.proxy_get("http://b:1")["use_count"], before - 1, "release_use 回退计数")

        # ── 409 生命周期 ──
        fresh_db()
        from webui import db as d, proxy_pool as p  # noqa: PLC0415
        d.proxy_add_many(["http://x:1"])
        r1 = p.record_409("http://x:1")
        c.eq(r1["action"], "count", "第 1 次 409 只计数")
        c.eq(p.available_count(), 1, "第 1 次 409 后仍可用")
        r2 = p.record_409("http://x:1")
        c.eq(r2["action"], "freeze", "第 2 次 409 → 冻结")
        c.eq(p.available_count(), 0, "冻结期间不可用")
        row = d.proxy_get("http://x:1")
        remain = row["frozen_until"] - time.time()
        c.ok(19 * 60 < remain <= 20 * 60, f"冻结时长≈20 分钟（剩 {remain/60:.1f} 分）")
        c.eq(row["frozen_once"], 1, "frozen_once 置位")

        # 解冻
        with d._lock:
            con = d._conn()
            con.execute("UPDATE proxy_pool SET frozen_until=? WHERE url=?",
                        (time.time() - 1, "http://x:1"))
            con.commit()
        c.eq(p.available_count(), 1, "到期后自动重新可用")

        r3 = p.record_409("http://x:1")
        c.eq(r3["action"], "permanent", "解冻后再 1 次 409 → 永久不可用")
        c.eq(p.available_count(), 0, "永久不可用后不参与轮换")
        c.ok(p.pick() is None, "pick 返回 None")

        c.ok(d.proxy_reset("http://x:1"), "管理员重置")
        c.eq(p.available_count(), 1, "重置后恢复可用")
        row = d.proxy_get("http://x:1")
        c.eq((row["total_409"], row["frozen_once"], row["permanent_dead"]), (0, 0, 0),
             "重置清空 409 计数与标记")

        # ── 成功清零连续计数 ──
        fresh_db()
        from webui import db as d2, proxy_pool as p2  # noqa: PLC0415
        d2.proxy_add_many(["http://y:1"])
        p2.record_409("http://y:1")
        p2.record_success("http://y:1")
        c.eq(d2.proxy_get("http://y:1")["consecutive_409"], 0, "成功后连续 409 清零")
        r = p2.record_409("http://y:1")
        c.eq(r["action"], "count", "清零后再 1 次不冻结")

        # ── 预检查：池空 / 全冻结 / 全禁用 ──
        fresh_db()
        from webui import db as d3, proxy_pool as p3  # noqa: PLC0415
        c.ok(not p3.has_available(), "池空 → 预检查为假")
        d3.proxy_add_many(["http://z:1"])
        c.ok(p3.has_available(), "有代理 → 预检查为真")
        d3.proxy_set_enabled("http://z:1", False)
        c.ok(not p3.has_available(), "全部禁用 → 预检查为假")
        d3.proxy_set_enabled("http://z:1", True)
        for _ in range(2):
            p3.record_409("http://z:1")
        c.ok(not p3.has_available(), "全部冻结 → 预检查为假")

        # ── 替换池保留统计 ──
        fresh_db()
        from webui import db as d4  # noqa: PLC0415
        d4.proxy_add_many(["http://k:1", "http://m:1"])
        d4.proxy_record_success("http://k:1")
        res = d4.proxy_replace_all(["http://k:1", "http://n:1"])
        c.eq(res["added"], 1, "replace 只加新代理")
        c.eq(d4.proxy_get("http://k:1")["success_count"], 1, "replace 保留原统计")
        c.ok(d4.proxy_get("http://m:1") is None, "replace 删掉不在列表里的")

        # 回归：池子比尝试次数还小时，必须轮换复用，不能直接放弃。
        # 真事故：池里只有 1 个代理，第 2 次尝试直接 PROXY_UNAVAILABLE → 作业 busy 结束，
        # 3 次重试根本没跑。
        proxy_pool.replace_from_text("http://u:p@1.1.1.1:1111")
        only = db.proxy_list_with_state()[0]["url"]
        again = db.proxy_pick(exclude=[only])
        c.ok(again is not None and again["url"] == only,
             "池内只有一个代理时，排除它之后仍能轮换复用（不再直接放弃）")

    return c

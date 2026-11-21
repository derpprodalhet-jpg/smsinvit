"""接码：参数归一化 / effective_params / 择优打分 / 成功率统计。"""
from __future__ import annotations

from tests.util import Check, fresh_db


def run() -> Check:
    fresh_db()
    from webui import db, sms_prices

    c = Check("接码：前缀归一化 / 择优 / effective_params / 成功率")

    with c:
        # ── 前缀归一化（官方格式：国家码 + 3~6 位掩码，逗号分隔）──
        c.eq(sms_prices.normalize_blocked_prefixes("52:66,52:81")[0], "5266,5281",
             "国家:前缀 归一化")
        c.eq(sms_prices.normalize_blocked_prefixes("+5266 5281")[0], "5266,5281",
             "带 + / 空格 归一化")
        c.eq(sms_prices.normalize_blocked_prefixes("")[0], "", "空值")

        from sms_provider import is_prefix_blocked  # noqa: PLC0415
        c.ok(is_prefix_blocked("+52 66 1234 5678", ["5266"]), "本地复核命中前缀")
        c.ok(not is_prefix_blocked("+52 81 1234", ["5266"]), "本地复核不命中")

        # ── 价格/库存归一化含 physicalCount ──
        from sms_provider import _norm_price_row  # noqa: PLC0415
        row = _norm_price_row("52", {"cost": 0.18, "count": 100, "physicalCount": 42, "minPrice": 0.1})
        c.eq((row["country"], row["price"], row["count"], row["physical_count"], row["min_price"]),
             ("52", 0.18, 100, 42, 0.1), "归一化读取 physicalCount / minPrice")

        # ── 择优 ──
        db.sms_price_upsert("smsbower", "dr", [
            {"country": "52", "price": 0.20, "count": 500, "physical_count": 300},
            {"country": "7", "price": 0.05, "count": 800, "physical_count": 700},
        ], ok=True)
        db.sms_price_upsert("herosms", "dr", [
            {"country": "52", "price": 0.18, "count": 900, "physical_count": 800},
        ], ok=True)
        for _ in range(10):
            db.sms_stats_bump("smsbower", "dr", "52", "attempt")
        db.sms_stats_bump("smsbower", "dr", "52", "ok")
        for _ in range(10):
            db.sms_stats_bump("herosms", "dr", "52", "attempt")
        for _ in range(9):
            db.sms_stats_bump("herosms", "dr", "52", "ok")

        best_price = sms_prices.compute_best()
        # 规则：**完全按配置排序**（价格权重打分 → 价格 → 库存），谁便宜用谁。
        # openai_safe 只作为信息返回，不参与排序、不覆盖配置。
        c.eq(best_price["country"], "7", "默认权重下最便宜的赢（不看白名单）")
        c.ok("openai_safe" in best_price, "最优结果里带了 openai_safe 信息（仅供参考）")
        # 冷启动平滑先验：拿没有统计的 7 号国家来验（与排序无关）
        ev_all = sms_prices.evaluate()
        cand7 = next((x for x in ev_all["candidates"] if x["country"] == "7"), None)
        c.eq(cand7["success_rate"] if cand7 else None, 0.5, "冷启动用平滑先验 0.5")

        # 新增：「排除虚拟号」必须按平台判断，不能把不给 physicalCount 的平台全灭
        ev_p = sms_prices.evaluate()
        c.ok(not ev_p["physical_only_ignored"] or ev_p["physical_only_effective"] is False,
             "physical_only 的退化状态自洽")
        c.ok(len(ev_p["candidates"]) > 0 or ev_p["platforms_without_physical"],
             "平台不给 physicalCount 时不会把候选清空（按平台退化）")

        db.save_sms_config({"sms_price_weight": "0.0"})
        best_rate = sms_prices.compute_best()
        c.eq(best_rate["country"], "52", "权重=0 → 成功率最高的赢")
        c.eq(best_rate["platform"], "herosms", "成功率最高的是 HeroSMS/52")

        # ── 过滤 ──
        db.save_sms_config({"sms_price_weight": "0.5", "sms_excluded_countries": "7"})
        ev = sms_prices.evaluate()
        c.ok(all(x["country"] != "7" for x in ev["candidates"]), "排除国家生效")
        c.ok(any(x["reason"] == "在排除国家名单里" for x in ev["rejected"]), "给出被拒原因")
        db.save_sms_config({"sms_excluded_countries": ""})

        db.save_sms_config({"sms_max_price": "0.1"})
        ev2 = sms_prices.evaluate()
        c.ok(all(x["price"] <= 0.1 for x in ev2["candidates"]), "最高价过滤生效")
        db.save_sms_config({"sms_max_price": ""})

        db.save_sms_config({"sms_min_price": "0.19"})
        ev3 = sms_prices.evaluate()
        c.ok(all(x["price"] >= 0.19 for x in ev3["candidates"]), "最低价过滤生效")
        db.save_sms_config({"sms_min_price": ""})

        db.save_sms_config({"sms_physical_only": "1", "sms_auto_min_stock": "400"})
        ev4 = sms_prices.evaluate()
        c.ok(all(x["stock"] == x["physical_count"] for x in ev4["candidates"]),
             "排除虚拟号：按 physicalCount 计库存")
        c.ok(ev4["physical_count_available"], "自检标记 physicalCount 可用")
        db.save_sms_config({"sms_physical_only": "0", "sms_auto_min_stock": "20"})

        # 平台模式固定
        db.save_sms_config({"sms_platform_mode": "herosms"})
        eff = sms_prices.effective_params()
        c.eq(eff["platform_candidates"], ["herosms"], "固定平台模式")
        db.save_sms_config({"sms_platform_mode": "auto"})

        # ── effective_params：最优结果驱动实际取号 ──
        db.save_sms_config({
            "sms_use_monitor_best": "1", "sms_price_weight": "1.0",
            "sms_blocked_prefixes": "7:91", "sms_min_price": "0.01",
            "sms_exclude_providers": "9,10", "sms_price_margin": "0.02",
        })
        eff2 = sms_prices.effective_params()
        c.eq(eff2["source"], "monitor", "来源=monitor（任务最优生效）")
        c.eq(eff2["country_candidates"][0], "7", "首选国家=最优国家（按配置最便宜）")
        c.eq(eff2["max_price"], round(0.05 + 0.02, 4), "maxPrice=最优价+容差")
        c.ok("openai_safe" in eff2, "effective_params 里带白名单信息（仅提示）")
        c.eq(eff2["phone_exception"], "791", "phoneException 透传")
        c.eq(eff2["except_provider_ids"], "9,10", "供货商排除透传")
        c.eq(eff2["min_price"], 0.01, "最低价透传")

        # 关掉自动择优 → 回退手动配置
        db.save_sms_config({"sms_use_monitor_best": "0", "sms_country": "62", "sms_max_price": "0.3"})
        eff3 = sms_prices.effective_params()
        c.eq(eff3["source"], "manual", "关闭自动择优 → manual")
        # 新规则：设了默认国家先试它，其余可用国家跟在后面（不能只用它一个 ——
        # 那正是"平台回 NO_NUMBERS、看起来像坏了"的老毛病）；设了「允许国家」才只从里面挑。
        c.eq(eff3["country_candidates"][0], "62", "默认国家排第一位")
        c.ok(len(eff3["country_candidates"]) > 1, "后面还跟着全部可用国家（不是只剩一个）")
        db.save_sms_config({"sms_allowed_countries": "62,7"})
        eff4 = sms_prices.effective_params()
        c.eq(eff4["country_candidates"], ["62", "7"], "设了「允许国家」→ 只用这些")
        db.save_sms_config({"sms_allowed_countries": ""})
        c.eq(eff3["max_price"], 0.3, "用手动最高价")

        # ── 成功率统计 ──
        stats = {(r["platform"], r["country"]): r for r in db.sms_stats_list()}
        s = stats[("smsbower", "52")]
        c.eq((s["attempts"], s["ok"], s["fail"]), (10, 1, 0), "attempts/ok 累计正确")

    return c

"""接码价格任务 + 自动择优（每 5s 刷新已配置的平台）。

关键点（主人定的）：

  1. **独立平台 Key**：SmsBower / HeroSMS / SMSPool 各配一个 API Key，每 5s 各拉一次
     价格/库存，落 `sms_prices`；单平台失败只记 `last_error` 并保留上次快照。
  2. **任务算出的最优就是实际取号参数**：命中 SMS 时调 `effective_params()`，
     平台 / 国家 / `maxPrice`（最优价 + 容差）/ `minPrice` / `phoneException`
     / 供货商 全部用它 —— 所以「配置好之后自动选最优」是真的生效，
     而不是页面上的摆设。
  3. **按官方 API 对齐的配置**：排除指定国家、排除指定国家的号码前缀
     （`phoneException`）、排除虚拟号（`physicalCount`）、最高/最低价、库存、
     供货商包含/排除。参数名全部来自平台文档，见 README 的对照表。
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_provider import (  # noqa: E402
    OPENAI_SMS_COUNTRIES,
    SMS_COUNTRY_NAMES_CN,
    SMS_DEFAULT_SERVICE,
    create_sms_provider,
    normalize_blocked_prefixes,
)

from . import db  # noqa: E402

logger = logging.getLogger("sms_prices")

PLATFORMS = ("smsbower", "herosms", "smspool")
PLATFORM_LABELS = {"smsbower": "SmsBower", "herosms": "HeroSMS", "smspool": "SMSPool"}


# ──────────────────────── 小工具 ────────────────────────


def _int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _float(v, default: float) -> float:
    try:
        s = str(v).strip()
        return float(s) if s else default
    except Exception:
        return default


def split_csv(raw) -> list[str]:
    return [c.strip() for c in re.split(r"[,;\s]+", str(raw or "")) if c.strip()]


def platform_key_configured(cfg: dict, pk: str) -> bool:
    return bool(str(cfg.get(f"sms_api_key_{pk}") or "").strip())


def platform_mode(cfg: dict) -> str:
    mode = str(cfg.get("sms_platform_mode") or "auto").strip().lower()
    if mode != "auto":
        # Existing configuration wire stays a string: a single platform or a
        # comma-separated selection. Preserve invalid selections as empty scope.
        selected = list(dict.fromkeys(split_csv(mode)))
        return ",".join(selected)
    # 新增平台独立配置时，自动模式也能在价格快照尚未就绪时使用它。
    if (mode == "auto" and platform_key_configured(cfg, "smspool")
            and not any(platform_key_configured(cfg, pk) for pk in ("smsbower", "herosms"))):
        return "smspool"
    return mode


def selected_platforms(cfg: dict) -> tuple[str, ...]:
    mode = platform_mode(cfg)
    if mode == "auto":
        return PLATFORMS
    return tuple(pk for pk in split_csv(mode) if pk in PLATFORMS)


def probe_platforms(cfg: Optional[dict] = None) -> dict:
    """并行只读检查三平台余额及报价/库存；不更新缓存、不创建或改变订单。"""
    from smsinvit.invitation.safety import redact
    cfg = cfg if cfg is not None else db.get_sms_internal_config()
    service = str(cfg.get("sms_service") or SMS_DEFAULT_SERVICE)
    secrets = [str(cfg.get(f"sms_api_key_{pk}") or "") for pk in PLATFORMS]

    def safe_error(exc):
        message = redact(str(exc))
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return f"{type(exc).__name__}: {message[:250]}"

    def probe(pk):
        started = time.monotonic()
        result = {"configured": platform_key_configured(cfg, pk), "balance_ok": False,
                  "prices_ok": False, "price_rows": 0, "in_stock_rows": 0, "stock": 0}
        if not result["configured"]:
            result["error"] = "未配置 API Key"
            result["elapsed_ms"] = 0
            return pk, result
        try:
            provider = create_sms_provider(pk, cfg)
            original = provider._request

            def readonly_request(params, **kwargs):
                if params.get("action") not in {"getBalance", "getPrices"}:
                    raise ValueError("平台诊断禁止租号、发码及修改订单")
                kwargs["timeout"] = 10
                return original(params, **kwargs)

            provider._request = readonly_request
        except Exception as exc:
            result["error"] = safe_error(exc)
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            return pk, result
        try:
            result["balance"] = provider.get_balance()
            result["balance_ok"] = True
        except Exception as exc:
            result["balance_error"] = safe_error(exc)
        try:
            rows = provider.get_top_countries(service)
            result.update(prices_ok=bool(rows), price_rows=len(rows),
                          in_stock_rows=sum(row["count"] > 0 for row in rows),
                          stock=sum(max(0, row["count"]) for row in rows))
            if not rows:
                result["prices_error"] = "当前服务未返回可解析报价"
        except Exception as exc:
            result["prices_error"] = safe_error(exc)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return pk, result

    with ThreadPoolExecutor(max_workers=3) as pool:
        platforms = dict(pool.map(probe, PLATFORMS))
    return {"ok": all(row["balance_ok"] and row["prices_ok"] for row in platforms.values()),
            "platforms": platforms}


# ──────────────────────── 打分 / 优选 ────────────────────────


def evaluate(cfg: Optional[dict] = None) -> dict:
    """按**当前配置**过滤 + 打分，返回候选与被拒原因（页面要能解释为什么）。"""
    cfg = cfg or db.get_sms_internal_config()
    service = str(cfg.get("sms_service") or SMS_DEFAULT_SERVICE).strip() or SMS_DEFAULT_SERVICE

    allowed = split_csv(cfg.get("sms_allowed_countries"))
    excluded = set(split_csv(cfg.get("sms_excluded_countries")))
    strict = bool(cfg.get("sms_strict_whitelist"))
    mode = platform_mode(cfg)
    scope = set(selected_platforms(cfg))
    min_stock = max(1, _int(cfg.get("sms_auto_min_stock"), 20))
    min_price = _float(cfg.get("sms_min_price"), 0.0)
    ceilings = [v for v in (_float(cfg.get("sms_auto_max_price"), 0.0), _float(cfg.get("sms_max_price"), 0.0)) if v > 0]
    max_price = min(ceilings) if ceilings else 0.0
    min_rate = _float(cfg.get("sms_min_success_rate"), 0.0)
    weight = min(1.0, max(0.0, _float(cfg.get("sms_price_weight"), 0.5)))
    physical_only = bool(cfg.get("sms_physical_only"))

    stats = {(r["platform"], str(r["country"])): r for r in db.sms_stats_list()}
    rows = db.sms_price_list(service=service)
    states = {row["platform"]: row for row in db.sms_price_state_list()}
    max_age = max(30, 3 * _int(cfg.get("sms_price_poll_seconds"), 5))
    now = time.time()

    candidates: list[dict] = []
    rejected: list[dict] = []
    # ⚠️ 「排除虚拟号」靠 physicalCount。但 smsbower / herosms 两个接口**从来不给这个字段**
    #    （实测 120/120 行全是 NULL）。如果照字面执行，每一行都会被"接口没给实体卡数量"拒掉
    #    → 候选永远 0 → 价格任务永远没有最优结果，主人只看到"代码坏了"。
    #    所以：整个平台都没有这个字段时，必须**优雅退化**（不排除），并把这件事说出来。
    # 必须**按平台**判断：herosms 偶尔给 physicalCount，smsbower 从来不给 ——
    # 用跨平台的 any() 会让 smsbower 的每一行仍被拒，候选照样是 0。
    platform_has_physical = {
        str(r.get("platform")) for r in rows if r.get("physical_count") is not None
    }
    physical_seen = bool(platform_has_physical)
    physical_only_effective = bool(physical_only and physical_seen)

    for r in rows:
        pk = str(r.get("platform") or "")
        cid = str(r.get("country") or "")
        if pk not in scope:
            continue
        price = _float(r.get("price"), 0.0)

        def _rej(reason: str) -> None:
            rejected.append({"platform": pk, "country": cid, "price": price, "reason": reason})

        state = states.get(pk) or {}
        if not platform_key_configured(cfg, pk):
            _rej("未配置 API Key")
            continue
        if state.get("last_error"):
            _rej("平台最近报价刷新失败")
            continue
        if now - _float(r.get("updated_at"), 0) > max_age:
            _rej("报价已过期")
            continue
        if cid in excluded:
            _rej("在排除国家名单里")
            continue
        if allowed and cid not in allowed:
            _rej("不在允许国家名单里")
            continue
        if strict and cid not in OPENAI_SMS_COUNTRIES:
            _rej("不在项目经验名单内")
            continue
        if price <= 0:
            _rej("价格无效")
            continue
        if min_price > 0 and price < min_price:
            _rej(f"低于最低价 {min_price}")
            continue
        if max_price > 0 and price > max_price:
            _rej(f"高于最高价 {max_price}")
            continue

        physical = r.get("physical_count")
        # 只有**这个平台确实会给** physicalCount 时，"排除虚拟号"才对该行生效；
        # 平台压根不给（smsbower），就不该拿一个它没有的字段去否决它全部国家。
        row_physical_effective = bool(physical_only and pk in platform_has_physical)
        if row_physical_effective and physical is None:
            _rej("接口没给实体卡数量（physicalCount），已按虚拟号排除")
            continue
        stock = int(physical if (row_physical_effective and physical is not None) else (r.get("count") or 0))
        if stock < min_stock:
            _rej(f"库存不足 {min_stock}")
            continue

        st = stats.get((pk, cid)) or {}
        attempts = int(st.get("attempts") or 0)
        ok = int(st.get("ok") or 0)
        fail = int(st.get("fail") or 0)
        # 平滑成功率：冷启动 ≈ 0.5，样本少时不至于被误杀
        eff = (ok + 2) / (attempts + 4)

        candidates.append({
            "platform": pk,
            "platform_label": PLATFORM_LABELS.get(pk, pk),
            "country": cid,
            "country_name": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
            "price": price,
            "min_price": _float(r.get("min_price"), 0.0),
            "count": int(r.get("count") or 0),
            "physical_count": physical,
            "stock": stock,
            "attempts": attempts,
            "ok": ok,
            "fail": fail,
            "success_rate": round(eff, 4),
            "score": 0.0,
            "openai_safe": cid in OPENAI_SMS_COUNTRIES,
        })

    # 实际租号与显示最优共享硬过滤，不在候选为空时放宽库存。
    picked = list(candidates)
    if min_rate > 0:
        picked = [c for c in picked if c["success_rate"] >= min_rate]

    if picked:
        maxp = max(c["price"] for c in picked) or 1.0
        for c in picked:
            np = c["price"] / maxp
            c["score"] = round(weight * np + (1.0 - weight) * (1.0 - c["success_rate"]), 6)
        # 排序**完全按配置**：打分（价格权重 × 平滑成功率）→ 价格 → 库存。
        # `openai_safe` 只作为信息随结果一起返回 / 页面上提示，**不参与排序、不覆盖配置**。
        picked.sort(key=lambda c: (c["score"], c["price"], -c["stock"]))

    safe_n = sum(1 for c in picked if c.get("openai_safe"))
    return {
        "service": service,
        "candidates": picked,
        "openai_safe_candidates": safe_n,
        "openai_safe_picked": bool(picked and picked[0].get("openai_safe")),
        "rejected": rejected[:200],
        "weight": weight,
        "min_stock": min_stock,
        "physical_only": physical_only,
        "physical_only_effective": physical_only_effective,
        "physical_only_ignored": bool(physical_only and not physical_seen),
        "platforms_without_physical": sorted(set(PLATFORMS) - platform_has_physical),
        "physical_count_available": physical_seen,
        "min_success_rate": min_rate,
        "platform_mode": mode,
    }


def compute_best(cfg: Optional[dict] = None) -> Optional[dict]:
    """当前配置下的最优「平台 + 国家」。找不到返回 None（调用方回退手动配置）。"""
    ev = evaluate(cfg)
    cands = ev["candidates"]
    if not cands:
        return None
    best = dict(cands[0])
    best["alternatives"] = [dict(c) for c in cands[1:4]]
    best["evaluated_at"] = time.time()
    try:
        db.sms_price_state_set(
            best["platform"],
            best_country=best["country"],
            best_price=best["price"],
            best_score=best["score"],
            best_success_rate=best["success_rate"],
            best_at=best["evaluated_at"],
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("写 best 状态失败: %s", e)
    return best


def effective_params(cfg: Optional[dict] = None) -> dict:
    """显示与实际租号共享同一批新鲜候选，所有价格均受用户硬上限约束。"""
    cfg = cfg or db.get_sms_internal_config()
    service = str(cfg.get("sms_service") or SMS_DEFAULT_SERVICE).strip()
    exception, blocked = normalize_blocked_prefixes(cfg.get("sms_blocked_prefixes"))
    ev = evaluate(cfg)
    candidates = list(ev["candidates"])
    ceilings = [value for value in (_float(cfg.get("sms_max_price"), 0), _float(cfg.get("sms_auto_max_price"), 0)) if value > 0]
    hard_cap = min(ceilings) if ceilings else 0
    margin = max(0, _float(cfg.get("sms_price_margin"), 0))
    rentals = []
    for row in candidates:
        price = round(row["price"] + margin, 4)
        if hard_cap:
            price = min(price, hard_cap)
        rentals.append({"platform": row["platform"], "country": row["country"], "max_price": price})
    platforms = list(dict.fromkeys(row["platform"] for row in candidates))
    countries = list(dict.fromkeys(row["country"] for row in candidates))
    by_platform = {pk: [row["country"] for row in candidates if row["platform"] == pk] for pk in platforms}
    best = dict(candidates[0]) if candidates else None
    if best:
        best["alternatives"] = [dict(row) for row in candidates[1:4]]
    return {
        "source": "monitor", "service": service,
        "platform_candidates": platforms, "country_candidates": countries,
        "rental_candidates": rentals, "country_candidates_by_platform": by_platform,
        "max_price_by_country": {f"{row['platform']}:{row['country']}": row["max_price"] for row in rentals},
        "max_price": rentals[0]["max_price"] if rentals else hard_cap,
        "min_price": _float(cfg.get("sms_min_price"), 0),
        "phone_exception": exception, "blocked_prefixes": blocked,
        "provider_ids": ",".join(split_csv(cfg.get("sms_include_providers"))),
        "except_provider_ids": ",".join(split_csv(cfg.get("sms_exclude_providers"))),
        "allowed_countries": split_csv(cfg.get("sms_allowed_countries")),
        "pool_countries": countries, "best": best,
        "openai_safe": bool(best and best.get("openai_safe")),
    }


# ──────────────────────── 后台轮询任务 ────────────────────────


class SmsPriceMonitor:
    """每 N 秒（默认 5s）刷新已配置平台的价格/库存。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rounds = 0
        self._last_error = ""
        self._started_at = 0.0
        self._balance_counter = 0
        self._refresh_future: Optional[Future] = None
        self._refresh_config: Optional[dict] = None

    # ── 生命周期 ──
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="sms-price-monitor"
            )
            self._thread.start()
            logger.info("[sms-prices] 价格任务已启动")

    def stop(self) -> None:
        self._stop.set()

    def interval(self) -> int:
        return max(2, _int(db.get_setting("sms_price_poll_seconds", "5"), 5))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
                # Balance checks are independent reads and never delay a caller
                # waiting for fresh prices (especially the first phone request).
                if self._balance_counter % 12 == 0:
                    self.refresh_balances()
                self._balance_counter += 1
                self._last_error = ""
            except Exception as e:  # noqa: BLE001
                self._last_error = str(e)
                logger.warning("[sms-prices] 本轮刷新异常: %s", e)
            self._rounds += 1
            self._stop.wait(self.interval())

    # ── 单轮刷新 ──
    def refresh_once(self, cfg: Optional[dict] = None) -> dict:
        cfg = dict(cfg if cfg is not None else db.get_sms_internal_config())
        while True:
            with self._lock:
                future = self._refresh_future
                if future is None:
                    future = Future()
                    self._refresh_future = future
                    self._refresh_config = cfg
                    owner = True
                else:
                    owner = False
                same_config = self._refresh_config == cfg
            if owner:
                break
            result = future.result()
            if same_config:
                return result
        try:
            result = self._refresh_prices(cfg)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._refresh_future is future:
                    self._refresh_future = None
                    self._refresh_config = None

    @staticmethod
    def _readonly_provider(pk: str, cfg: dict, action: str):
        provider = create_sms_provider(pk, cfg)
        request = provider._request

        def bounded_request(params, **kwargs):
            if params.get("action") != action:
                raise RuntimeError("报价任务只允许指定的只读接口")
            kwargs["timeout"] = min(10, kwargs.get("timeout", 10))
            return request(params, **kwargs)

        provider._request = bounded_request
        return provider

    def _refresh_prices(self, cfg: dict) -> dict:
        service = str(cfg.get("sms_service") or SMS_DEFAULT_SERVICE).strip() or SMS_DEFAULT_SERVICE

        def refresh(pk):
            if not platform_key_configured(cfg, pk):
                db.sms_price_upsert(pk, service, [], ok=False, error="未配置 API Key")
                return pk, {"ok": False, "rows": 0, "error": "未配置 API Key"}
            try:
                provider = self._readonly_provider(pk, cfg, "getPrices")
                # Keep the full snapshot: hard filters run before any display
                # limit, otherwise cheap empty countries can hide usable ones.
                rows = provider.get_top_countries(service=service)
                norm = [
                    {
                        "country": str(r.get("country") or ""),
                        "price": r.get("price"),
                        "count": r.get("count"),
                        "physical_count": r.get("physical_count"),
                        "min_price": r.get("min_price"),
                        "raw": json.dumps(r, ensure_ascii=False),
                    }
                    for r in rows
                    if str(r.get("country") or "")
                ]
                db.sms_price_upsert(pk, service, norm, ok=True)
                return pk, {"ok": True, "rows": len(norm)}
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:300]
                db.sms_price_upsert(pk, service, [], ok=False, error=msg)
                logger.warning("[sms-prices] %s 刷新失败: %s", pk, msg)
                return pk, {"ok": False, "rows": 0, "error": msg}

        with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as pool:
            # cfg is captured before dispatch. Never copy the caller's entire
            # context: it may own a database transaction or OAuth credentials.
            futures = [pool.submit(refresh, pk) for pk in PLATFORMS]
            return dict(future.result() for future in futures)

    def refresh_balances(self, cfg: Optional[dict] = None) -> dict:
        cfg = dict(cfg if cfg is not None else db.get_sms_internal_config())

        def refresh(pk):
            if not platform_key_configured(cfg, pk):
                return pk, False
            try:
                balance = self._readonly_provider(pk, cfg, "getBalance").get_balance()
                db.sms_price_state_set(pk, balance=balance)
                return pk, True
            except Exception as exc:
                logger.debug("[sms-prices] %s 余额查询失败: %s", pk, type(exc).__name__)
                return pk, False

        with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as pool:
            futures = [pool.submit(refresh, pk) for pk in PLATFORMS]
            return dict(future.result() for future in futures)

    # ── 快照（给前端 + 给取号）──
    # ── 便捷委托：调用方拿到 MONITOR 后能直接用（别再调 MONITOR.effective_params 报
    #    AttributeError —— 那会把整个接码 controller 创建搞挂，而且只记一条 warning）。
    #    方法体内的 `effective_params(...)` 走的是**模块级函数**（类命名空间不参与
    #    方法体内的名字解析），不会递归。
    def effective_params(self, cfg: Optional[dict] = None) -> dict:
        """本次实际生效的取号参数（平台/国家/价格/前缀规则）。"""
        return effective_params(cfg)

    def compute_best(self, cfg: Optional[dict] = None) -> Optional[dict]:
        """当前最优平台+国家（无候选返回 None）。"""
        return compute_best(cfg)

    def snapshot(self) -> dict:
        cfg = db.get_sms_internal_config()
        ev = evaluate(cfg)
        best = compute_best(cfg)
        eff = effective_params(cfg)

        states = {r["platform"]: dict(r) for r in db.sms_price_state_list()}
        platforms = {}
        for pk in PLATFORMS:
            st = states.get(pk) or {}
            platforms[pk] = {
                "label": PLATFORM_LABELS.get(pk, pk),
                "configured": platform_key_configured(cfg, pk),
                "last_poll_at": st.get("last_poll_at"),
                "last_ok_at": st.get("last_ok_at"),
                "last_error": st.get("last_error"),
                "balance": st.get("balance"),
            }

        stats = {(r["platform"], str(r["country"])): r for r in db.sms_stats_list()}
        rows = []
        for r in db.sms_price_list():
            st = stats.get((r["platform"], str(r["country"]))) or {}
            attempts = int(st.get("attempts") or 0)
            ok = int(st.get("ok") or 0)
            rows.append({
                **r,
                "platform_label": PLATFORM_LABELS.get(r["platform"], r["platform"]),
                "country_name": SMS_COUNTRY_NAMES_CN.get(str(r["country"]), ""),
                "openai_safe": str(r["country"]) in OPENAI_SMS_COUNTRIES,
                "attempts": attempts,
                "ok": ok,
                "fail": int(st.get("fail") or 0),
                "success_rate": round((ok + 2) / (attempts + 4), 4),
                "score": next(
                    (c["score"] for c in ev["candidates"]
                     if c["platform"] == r["platform"] and c["country"] == str(r["country"])),
                    None,
                ),
                "rejected_reason": next(
                    (x["reason"] for x in ev["rejected"]
                     if x["platform"] == r["platform"] and x["country"] == str(r["country"])),
                    "",
                ),
            })

        return {
            "ok": True,
            "poll_seconds": self.interval(),
            "rounds": self._rounds,
            "started_at": self._started_at,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "last_error": self._last_error,
            "platforms": platforms,
            "rows": rows,
            "best": best,
            "effective": eff,
            "evaluate": {
                "weight": ev["weight"],
                "min_stock": ev["min_stock"],
                "physical_only": ev["physical_only"],
                "physical_only_effective": ev["physical_only_effective"],
                "physical_only_ignored": ev["physical_only_ignored"],
                "platforms_without_physical": ev["platforms_without_physical"],
                "physical_count_available": ev["physical_count_available"],
                "min_success_rate": ev["min_success_rate"],
                "platform_mode": ev["platform_mode"],
                "candidates": ev["candidates"][:30],
                "rejected": ev["rejected"][:60],
            },
            "refunds": (lambda: __import__("webui.sms_refund", fromlist=["WORKER"]).WORKER.snapshot())(),
            "generated_at": time.time(),
        }

    # ── 自检：确认平台真实支持哪些字段 ──
    def selftest(self) -> dict:
        cfg = db.get_sms_internal_config()
        service = str(cfg.get("sms_service") or SMS_DEFAULT_SERVICE).strip() or SMS_DEFAULT_SERVICE
        out: dict = {"service": service, "platforms": {}}
        for pk in PLATFORMS:
            entry: dict = {"configured": platform_key_configured(cfg, pk), "probes": []}
            if not entry["configured"]:
                entry["error"] = "未配置 API Key"
                out["platforms"][pk] = entry
                continue
            try:
                provider = create_sms_provider(pk, cfg)
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
                out["platforms"][pk] = entry
                continue
            probes = (
                ("getBalance", {}),
                ("getPrices", {"service": service}),
                ("getPricesV2", {"service": service}),
                ("getPricesV3", {"service": service}),
                ("getTopCountriesByServiceRank", {"service": service}),
            )
            if pk == "smspool":
                probes = probes[:2]
            for action, params in probes:
                try:
                    res = provider.raw_call(action, **params)
                    entry["probes"].append({
                        "action": action,
                        "status": res.get("status"),
                        "elapsed_ms": res.get("elapsed_ms"),
                        "fields": res.get("fields") or [],
                        "text": (res.get("text") or "")[:200],
                    })
                except Exception as e:  # noqa: BLE001
                    entry["probes"].append({"action": action, "error": str(e)[:200]})
            entry["physical_count_available"] = any(
                any("physicalCount" in f or "physical_count" in f for f in (p.get("fields") or []))
                for p in entry["probes"]
            )
            out["platforms"][pk] = entry
        out["ok"] = True
        return out


MONITOR = SmsPriceMonitor()

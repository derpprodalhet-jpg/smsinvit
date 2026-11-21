"""SMSPool 接入离线验证：独立配置 / 工厂 / 价格任务 / 接码回调。

只使用临时数据库与假 HTTP 响应；不查询真实余额、租号或修改号码缓存。
"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

from tests.util import Check, fresh_db


POOL_KEY = "smspool-test-key-0123456789abcdef"
BOWER_KEY = "smsbower-test-key-0123456789abcdef"
HERO_KEY = "herosms-test-key-0123456789abcdef"


class FakeResp:
    def __init__(self, text="", payload=None, status=200):
        self.text = text
        self.status_code = status
        self.payload = payload

    def json(self):
        return self.payload if self.payload is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Clock:
    """轮询只推进虚拟时间，不让测试真正等待。"""
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _provider_contract(c: Check, sp) -> None:
    # SMSPool 兼容接口只支持 V1 租号；每次租号都创建独立订单，不读写旧平台缓存。
    pool = sp.SmsPoolProvider(api_key=POOL_KEY, default_service="dr", default_country="52",
                              max_price=0.25, reuse_phone_to_max=True)
    calls = []

    def http(url, params=None, **kwargs):
        p = dict(params or {})
        calls.append((url, p))
        if p["action"] == "getBalance":
            return FakeResp("ACCESS_BALANCE:4.75")
        if p["action"] == "getPrices":
            return FakeResp(payload={
                "52": {"dr": {"cost": 0.2, "count": 30}},
                "7": {"dr": {"cost": 0.1, "count": 10}},
            })
        if p["action"] == "getNumber":
            return FakeResp(f"ACCESS_NUMBER:order-{len(calls)}:15551234567")
        raise AssertionError(f"不支持的 SMSPool action: {p['action']}")

    with patch.object(sp.requests, "get", side_effect=http), \
            patch.object(pool, "_load_cache", side_effect=AssertionError("不应读取共用缓存")), \
            patch.object(pool, "_save_cache", side_effect=AssertionError("不应写共用缓存")):
        c.eq(pool.get_balance(), 4.75, "SMSPool 兼容接口解析余额")
        c.eq([row["country"] for row in pool.get_top_countries("dr")], ["7", "52"],
             "SMSPool 直接从兼容价格接口解析并排序价格库存")
        act = pool.get_number(service="dr", country="52", country_candidates=["52"], extra={
            "maxPriceByCountry": {"52": 0.3}, "minPrice": 0.1, "phoneException": "5266",
            "providerIds": "1", "exceptProviderIds": "2", "pool": "3",
        })
        second = pool.get_number(service="dr", country="0", country_candidates=["0"])
    c.eq(act.phone_number, "+15551234567", "SMSPool 租号文本归一化")
    c.ok(act.activation_id != second.activation_id, "SMSPool 不复用旧订单")
    actions = [params["action"] for _, params in calls]
    c.eq(actions, ["getBalance", "getPrices", "getNumber", "getNumber"],
         "仅使用 SMSPool 支持的查询和 V1 租号动作")
    c.ok(all(url == pool.DEFAULT_BASE_URL and params.get("api_key") == POOL_KEY
             for url, params in calls), "SMSPool HTTP 请求使用官方地址和独立 Key")
    number = next(params for _, params in calls if params["action"] == "getNumber")
    c.eq((number.get("service"), number.get("country"), number.get("maxPrice"), number.get("pool")),
         ("dr", "52", 0.3, "3"), "SMSPool 保留服务 / 国家 / 各国价格上限与显式 pool")
    c.ok(not any(key in number for key in ("minPrice", "phoneException", "providerIds", "exceptProviderIds")),
         "SMSPool 不发送未支持的旧平台参数")
    c.eq(calls[-1][1].get("country"), "0", "SMSPool 保留有效的国家编号 0")

    # 轮询只用 getStatus；业务拒绝验证码后按本订单去重，不发付费 resend。
    clock = Clock()
    status = ["STATUS_WAIT_CODE", "STATUS_OK:123456", "STATUS_OK:123456", "STATUS_OK:654321"]
    status_calls = []

    def poll(params, **kwargs):
        status_calls.append(dict(params))
        c.eq(params.get("action"), "getStatus", "SMSPool 等码仅查询 getStatus")
        return FakeResp(status.pop(0) if status else "STATUS_WAIT_CODE")

    business_resend = Mock()
    pool.set_resend_callback(business_resend)
    with patch.object(pool, "_request", side_effect=poll), \
            patch.object(sp.time, "time", clock.time), patch.object(sp.time, "sleep", clock.sleep), \
            patch.object(pool, "_save_cache", side_effect=AssertionError("不应写共用缓存")):
        c.eq(pool.get_code(act.activation_id, timeout=10), "123456", "SMSPool 等待后取得验证码")
        pool.mark_code_failed(act.activation_id, "业务侧拒绝验证码")
        c.eq(pool.get_code(act.activation_id, timeout=10), "654321", "SMSPool 忽略本订单已拒绝的验证码")
        c.eq(pool.request_resend_sms(act.activation_id), False, "SMSPool 不触发付费平台重发")
        request_count = len(status_calls)
        pool.mark_send_succeeded(act.activation_id)
        c.eq(len(status_calls), request_count, "业务已发码时 SMSPool 不发送未支持的平台动作")
        c.eq(pool.report_success(act.activation_id), True, "SMSPool 收到验证码后可报告完成")
        c.eq(len(status_calls), request_count, "报告 SMSPool 成功不发送额外平台动作")
    c.eq(business_resend.call_count, 1, "拒绝验证码只请求业务侧重发")

    # 未收码时维持现有业务重发节奏，但不会调用 SMSPool 的付费重发接口。
    waiting_pool = sp.SmsPoolProvider(api_key=POOL_KEY)
    resend = Mock()
    waiting_pool.set_resend_callback(resend)
    clock = Clock()
    with patch.object(waiting_pool, "_request", return_value=FakeResp("STATUS_WAIT_CODE")) as request, \
            patch.object(sp.time, "time", clock.time), patch.object(sp.time, "sleep", clock.sleep):
        c.eq(waiting_pool.get_code("waiting-order", timeout=80), "", "SMSPool 等码窗口无验证码时返回空串")
    c.eq(resend.call_count, 3, "SMSPool 沿用最多三次业务侧重发")
    c.ok(all(call.args[0].get("action") == "getStatus" for call in request.call_args_list),
         "无验证码时也不发送 SMSPool 付费重发请求")

    # HTTP 200 本身不能证明退号成功；必须有 ACCESS_CANCEL 或状态确认。
    with patch.object(pool, "_request", return_value=FakeResp("ACCESS_CANCEL")) as request:
        c.eq(pool.cancel(act.activation_id), True, "SMSPool ACCESS_CANCEL 确认退号")
    c.eq(request.call_args.args[0], {"action": "setStatus", "id": act.activation_id, "status": 8},
         "SMSPool 只用官方 setStatus=8 退号")
    with patch.object(pool, "_request", side_effect=[
        FakeResp("BAD_STATUS"), FakeResp("BAD_STATUS"), FakeResp("STATUS_CANCEL"),
    ]) as request, patch.object(sp.time, "sleep"):
        c.eq(pool.cancel("confirmed-order"), True, "SMSPool 退号响应不确定时查询状态确认")
    c.eq([call.args[0]["action"] for call in request.call_args_list],
         ["setStatus", "setStatus", "getStatus"], "SMSPool 退号重试后使用 getStatus 复核")
    with patch.object(pool, "_request", side_effect=[
        FakeResp("BAD_STATUS"), FakeResp("BAD_STATUS"), FakeResp("STATUS_WAIT_CODE"),
    ]), patch.object(sp.time, "sleep"):
        c.eq(pool.cancel("unconfirmed-order"), False, "SMSPool 仅 HTTP 200 未确认退号时返回失败")
    with patch.object(pool, "cancel", return_value=False) as cancel:
        c.eq(pool.mark_send_failed("rejected-order", "号码被拒绝"), False, "SMSPool 发码失败沿用退号结果")
    c.eq(cancel.call_args.args[0], "rejected-order", "发码失败退掉当前 SMSPool 订单")


def run() -> Check:
    fresh_db()
    import sms_provider as sp
    from fastapi import HTTPException
    from webui import db, registrar, sms_prices, sms_refund

    c = Check("SMSPool 接入（临时库 / 离线打桩）")
    with c, patch.object(sp.requests, "get", side_effect=AssertionError("测试禁止真实 HTTP")):
        # app 导入时会启动后台任务：测试里不启动任何任务或网络轮询。
        with patch.object(sms_prices.SmsPriceMonitor, "start"), \
                patch.object(sms_refund.WORKER, "start"):
            from webui import app

        c.eq(db.get_sms_config().get("sms_api_key_smspool"), "", "SMSPool Key 默认为空")
        db.save_sms_config({
            "sms_api_key_smsbower": BOWER_KEY,
            "sms_api_key_herosms": HERO_KEY,
            "sms_api_key_smspool": POOL_KEY,
        })
        public = db.get_sms_config()
        c.eq(public.get("sms_api_key_smspool"), "***", "SMSPool Key 对外脱敏")
        c.ok(POOL_KEY not in str(public), "公开配置不泄漏 SMSPool Key")
        c.eq(db.get_sms_internal_config().get("sms_api_key_smspool"), POOL_KEY,
             "内部配置取得 SMSPool 独立 Key")
        app.api_save_sms_config(app.SaveSmsConfigReq(
            sms_api_key_smspool="***", sms_platform_mode="smspool",
        ))
        c.eq(db.get_sms_internal_config().get("sms_api_key_smspool"), POOL_KEY,
             "脱敏占位符回传保留 SMSPool Key")
        c.eq(db.get_sms_internal_config()["sms_api_key_smsbower"], BOWER_KEY,
             "修改 SMSPool 配置不影响 SmsBower Key")
        c.eq(db.get_sms_internal_config()["sms_api_key_herosms"], HERO_KEY,
             "修改 SMSPool 配置不影响 HeroSMS Key")
        try:
            app.api_save_sms_config(app.SaveSmsConfigReq(sms_api_key_smspool="bad-key"))
        except HTTPException as exc:
            c.eq(exc.status_code, 400, "短 SMSPool Key 被接口拒绝")
        else:
            c.ok(False, "短 SMSPool Key 必须被拒绝")
        c.eq(db.get_sms_internal_config().get("sms_api_key_smspool"), POOL_KEY,
             "无效 Key 不覆盖已有 SMSPool Key")
        app.api_save_sms_config(app.SaveSmsConfigReq(sms_api_key_smspool=""))
        c.eq(db.get_sms_internal_config().get("sms_api_key_smspool"), "", "允许显式清空 SMSPool Key")
        app.api_save_sms_config(app.SaveSmsConfigReq(sms_api_key_smspool=POOL_KEY))

        # 工厂只取所选平台的独立 Key，新增平台不改变原有平台地址或参数。
        cfg = db.get_sms_internal_config()
        cfg.update({"sms_country": "52", "sms_service": "dr", "sms_max_price": "0.5"})
        pool = sp.create_sms_provider("smspool", cfg)
        c.ok(isinstance(pool, sp.SmsPoolProvider), "工厂创建 SMSPool provider")
        c.eq(pool.api_key, POOL_KEY, "工厂使用 SMSPool 独立 Key")
        c.eq(pool.base_url, "https://api.smspool.net/stubs/handler_api", "使用 SMSPool 官方兼容接口")
        c.eq((pool.default_service, pool.default_country, pool.max_price), ("dr", "52", 0.5),
             "SMSPool 保留现有服务 / 国家 / 价格参数")
        c.eq(sp.create_sms_provider("smsbower", cfg).base_url,
             "https://smsbower.page/stubs/handler_api.php", "SmsBower 地址保持不变")
        c.eq(sp.create_sms_provider("herosms", cfg).base_url,
             "https://hero-sms.com/stubs/handler_api.php", "HeroSMS 地址保持不变")
        c.eq(sp.create_sms_provider("smsbower", cfg).api_key, BOWER_KEY, "SmsBower 仍使用自己的 Key")
        c.eq(sp.create_sms_provider("herosms", cfg).api_key, HERO_KEY, "HeroSMS 仍使用自己的 Key")
        for bad_cfg, label in (
            ({}, "未配 Key"),
            ({"sms_api_key_smspool": "   "}, "空白 Key"),
            ({"sms_api_key_smsbower": BOWER_KEY, "sms_api_key_herosms": HERO_KEY,
              "sms_api_key": "legacy-key"}, "只有其他平台或旧通用 Key"),
        ):
            try:
                sp.create_sms_provider("smspool", bad_cfg)
            except RuntimeError as exc:
                c.ok("API Key" in str(exc), f"SMSPool {label}时不串用其他平台 Key")
            else:
                c.ok(False, f"SMSPool {label}时必须拒绝创建")

        # 只配置 SMSPool，价格任务仍登记三个平台；仅为已配置平台调用工厂。
        db.save_sms_config({
            "sms_enabled": "1", "sms_api_key_smsbower": "", "sms_api_key_herosms": "",
            "sms_api_key_smspool": POOL_KEY, "sms_provider": "smsbower",
            "sms_platform_mode": "smspool", "sms_service": "dr", "sms_country": "52",
        })
        fake_provider = Mock()
        fake_provider.get_balance.return_value = 3.25
        fake_provider.get_top_countries.return_value = [
            {"country": "52", "price": 0.2, "count": 30, "physical_count": None},
        ]
        monitor = sms_prices.SmsPriceMonitor()
        with patch.object(sms_prices, "create_sms_provider", return_value=fake_provider) as factory:
            refreshed = monitor.refresh_once()
        c.eq(set(refreshed), {"smsbower", "herosms", "smspool"}, "价格任务登记三个平台")
        c.eq([call.args[0] for call in factory.call_args_list], ["smspool"],
             "仅为配置了独立 Key 的 SMSPool 拉取价格")
        c.eq(refreshed["smspool"], {"ok": True, "rows": 1}, "SMSPool 报价落入现有价格任务")
        c.eq(set(monitor.snapshot()["platforms"]), {"smsbower", "herosms", "smspool"},
             "价格快照含 SMSPool")
        c.eq(monitor.snapshot()["platforms"]["smspool"]["balance"], 3.25, "SMSPool 余额写入快照")

        # 固定 SMSPool 模式沿用原有择优规则，不会选中其他平台的便宜报价。
        db.sms_price_upsert("herosms", "dr", [{"country": "7", "price": 0.01, "count": 100}], ok=True)
        c.eq(sms_prices.compute_best()["platform"], "smspool", "固定 SMSPool 只选择本平台报价")
        db.save_sms_config({"sms_use_monitor_best": "0"})
        eff = sms_prices.effective_params()
        c.eq(eff["platform_candidates"], ["smspool"], "固定 SMSPool 手动回退仍只使用 SMSPool")
        c.eq(eff["country_candidates"][0], "52", "SMSPool 保留现有手动国家顺序")
        ctrl = registrar._build_sms_callback("run-smspool-offline")
        c.ok(ctrl is not None, "仅配置 SMSPool Key 时可以创建接码回调")
        if ctrl is not None:
            c.eq(ctrl.provider_key, "smspool", "回调使用固定 SMSPool 平台")
            c.ok(callable(ctrl.effective_params_fn), "SMSPool 回调接入现有生效参数")
            c.ok(isinstance(ctrl._provider(), sp.SmsPoolProvider), "回调可构造 SMSPool provider")
        with patch.object(sp, "create_sms_provider", return_value=fake_provider) as factory:
            result = app.api_test_sms()
        c.eq(result.get("provider"), "smspool", "连接测试按固定 SMSPool 模式查询")
        c.eq(factory.call_args.args[0], "smspool", "连接测试把请求派发给 SMSPool")
        with patch.object(sp, "create_sms_provider", return_value=fake_provider) as factory:
            top = app.api_sms_top_countries()
        c.eq(factory.call_args.args[0], "smspool", "国家排名接口按固定 SMSPool 模式查询")
        c.eq(top["countries"][0]["country"], "52", "SMSPool 排名接口返回当前平台的国家")
        with patch.object(sp, "create_sms_provider", return_value=fake_provider) as factory:
            countries = app.api_sms_all_countries("smspool")
        c.eq(countries.get("source"), "live", "仅配 SMSPool 独立 Key 时动态查询国家列表")
        c.eq(factory.call_args.args[0], "smspool", "动态国家查询使用 SMSPool 独立 Key")

        # 新用户仅填 SMSPool Key、保留默认 auto / smsbower，也应直接可用。
        # 上面保留了更便宜的 HeroSMS 快照，不能让失去 Key 的旧平台抢占结果。
        db.save_sms_config({"sms_platform_mode": "auto", "sms_use_monitor_best": "1"})
        only_pool = db.get_sms_internal_config()
        c.eq(sms_prices.platform_mode(only_pool), "smspool", "auto 且只有 SMSPool Key 时使用 SMSPool")
        c.eq(sms_prices.platform_mode({**only_pool, "sms_platform_mode": "smsbower"}), "smsbower",
             "SMSPool 兼容处理保留显式固定平台模式")
        c.eq(sms_prices.platform_mode({**only_pool, "sms_api_key_herosms": HERO_KEY}), "auto",
             "多个平台有 Key 时保留 auto 模式")
        c.eq(sms_prices.platform_mode({"sms_platform_mode": "auto", "sms_api_key_smsbower": BOWER_KEY}),
             "auto", "原有平台配置保持原有 auto 模式")
        c.eq(sms_prices.compute_best(only_pool)["platform"], "smspool",
             "只有 SMSPool Key 时忽略更便宜的旧平台快照")
        with patch.object(sms_prices, "compute_best", return_value=None):
            before_best = sms_prices.effective_params(only_pool)
            ctrl = registrar._build_sms_callback("run-smspool-before-price-best", only_pool)
        c.eq(before_best["source"], "manual", "价格任务尚无最优结果时走手动回退")
        c.eq(before_best["platform_candidates"], ["smspool"],
             "只有 SMSPool Key 时手动回退也选择 SMSPool")
        c.ok(ctrl is not None, "默认 auto 且价格任务尚无最优结果时仍可创建 SMSPool 回调")
        if ctrl is not None:
            c.eq(ctrl.provider_key, "smspool", "无最优结果的回调使用唯一配置的 SMSPool")
            c.ok(isinstance(ctrl._provider(), sp.SmsPoolProvider), "无最优结果也可构造 SMSPool provider")
        with patch.object(sp, "create_sms_provider", return_value=fake_provider) as factory:
            auto_test = app.api_test_sms()
        c.eq(auto_test.get("provider"), "smspool", "默认 auto 模式连接测试选择唯一配置的 SMSPool")
        c.eq(factory.call_args.args[0], "smspool", "auto 模式连接测试派发给 SMSPool")

        shared_cache = {"activation_id": "other-platform-order", "used_codes": {"123456", "654321"}}
        with patch.object(sp, "_SMS_CACHE", shared_cache):
            _provider_contract(c, sp)
        c.eq(shared_cache, {"activation_id": "other-platform-order", "used_codes": {"123456", "654321"}},
             "SMSPool 不修改原有平台共用缓存")

    return c

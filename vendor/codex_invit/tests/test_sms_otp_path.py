#!/usr/bin/env python
"""接码（SMS）全链路离线测试 —— **一个真实请求都不发，不花钱**。

覆盖：① 状态文本解析 ② get_code 轮询 ③ 租号参数（含按国家 maxPrice）
④ controller 端到端 ⑤ 前缀过滤重租 ⑥ effective_params 异常回退
⑦ 接码 controller 必须真能创建 ⑧ 退号语义（先退成功再换号）
⑨ hero-sms 退号失败(409) → 主链路不中断 + 后台每 60s 重试，最多 3 次
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.util import Check, fresh_db  # noqa: E402


class FakeResp:
    def __init__(self, text="", status=200, payload=None):
        self.text = text
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class ScriptedSms:
    """按 action 编排的假 SMS 服务器，记录收到的每个请求参数。"""

    def __init__(self, status_script=None, number_script=None, v2_payload=None):
        self.status_script = list(status_script or [])
        self.number_script = list(number_script or [])
        self.v2_payload = v2_payload
        self.calls = []

    def __call__(self, params, **kwargs):
        p = dict(params)
        self.calls.append(p)
        action = p.get("action", "")
        if action == "getNumberV2":
            if self.v2_payload is not None:
                return FakeResp(payload=self.v2_payload)
            return FakeResp(text="NO_NUMBERS")
        if action == "getNumber":
            if self.number_script:
                item = self.number_script.pop(0)
                if isinstance(item, str):
                    return FakeResp(text=item)
                return FakeResp(payload=item)
            return FakeResp(text="NO_NUMBERS")
        if action in ("getStatus", "getStatusV2"):
            if self.status_script:
                return FakeResp(text=self.status_script.pop(0))
            return FakeResp(text="STATUS_WAIT_CODE")
        return FakeResp(text="OK")

    def actions(self):
        return [c.get("action", "") for c in self.calls]

    def payload_for(self, action):
        for c in self.calls:
            if c.get("action") == action:
                return c
        return {}


_REAL_REQUESTS_GET = None


def _provider(script, **cfg):
    """造一个真 provider，但把 requests.get 换掉（一个真实请求都不发）。"""
    import sms_provider as sp
    from sms_provider import SmsBowerProvider

    global _REAL_REQUESTS_GET
    if _REAL_REQUESTS_GET is None:
        _REAL_REQUESTS_GET = sp.requests.get
    sp.requests.get = lambda url, params=None, timeout=None, proxies=None: script(dict(params or {}))
    with sp._SMS_CACHE_LOCK:
        sp._SMS_CACHE = {}

    kwargs = {"default_service": "dr", "default_country": "52"}
    kwargs.update({k: v for k, v in cfg.items() if k != "max_price"})
    if "max_price" in cfg:
        kwargs["max_price"] = cfg["max_price"]
    return SmsBowerProvider(api_key="k", **kwargs)


def _act(aid="aid-x", phone="+10000000000", country="52"):
    class _A:
        activation_id = aid
        phone_number = phone
        metadata = {}
    _A.country = country
    return _A()


class _CancelProvider:
    def __init__(self, ok):
        self.ok = ok
        self.last_cancel_raw = "HTTP 200 BAD_STATUS" if not ok else "HTTP 200 ACCESS_CANCEL"
        self.cancelled = []

    def cancel(self, aid):
        self.cancelled.append(aid)
        return self.ok

    def mark_send_failed(self, aid, reason=""):
        return self.cancel(aid)


class _FakeAct:
    def __init__(self, aid, phone, country="52"):
        self.activation_id = aid
        self.phone_number = phone
        self.country = country
        self.metadata = {}


class _FakeProvider:
    def __init__(self):
        self.rented = []
        self.cancelled = []
        self.success = False
        self.codes = []

    def get_number(self, *, service, country, country_candidates=None, extra=None):
        self.rented.append({"service": service, "country": country,
                            "candidates": list(country_candidates or []), "extra": dict(extra or {})})
        if len(self.rented) == 1:
            return _FakeAct("act-bad", "+52 66 1234 5678")
        return _FakeAct("act-good", "+528155550000")

    def cancel(self, aid):
        self.cancelled.append(aid)
        return True

    def get_code(self, aid, timeout=180):
        self.codes.append(aid)
        return "246810"

    def mark_code_failed(self, aid, reason=""):
        self.marked_fail = (aid, reason)

    def report_success(self, aid):
        self.success = aid


def run():
    c = Check("接码全链路（离线打桩，不花钱）")

    # ★ 全局工厂桩：任何路径（含 _provider 因配置变化而重建）都拿到假 provider，
    #   绝不允许测试里出现真实网络请求。
    import sms_provider as _spmod
    _real_factory = _spmod.create_sms_provider
    _factory_fakes = {}

    def _fake_factory(pk, cfg):
        f = _factory_fakes.get(pk)
        if f is None:
            f = _FakeProvider()
            _factory_fakes[pk] = f
        return f

    _spmod.create_sms_provider = _fake_factory
    from sms_provider import (PhoneCallbackController, _parse_sms_status_text,
                              is_prefix_blocked, normalize_blocked_prefixes)

    # ① 状态文本解析
    c.eq(_parse_sms_status_text("STATUS_WAIT_CODE").get("status"), "wait_code", "WAIT_CODE = 等待取码")
    r = _parse_sms_status_text("STATUS_WAIT_RETRY:1234")
    c.eq(r.get("status"), "wait_retry", "WAIT_RETRY = 等待重试")
    c.ok("1234" in str(r.get("raw")), "WAIT_RETRY 原文保留")
    c.eq(_parse_sms_status_text("STATUS_WAIT_RESEND").get("status"), "wait_resend", "WAIT_RESEND = 等待重发")
    r = _parse_sms_status_text("STATUS_OK:5678")
    c.eq(r.get("status"), "ok", "STATUS_OK = 收到")
    c.eq(r.get("code"), "5678", "STATUS_OK 的码正确")
    c.eq(_parse_sms_status_text("STATUS_CANCEL").get("status"), "cancel", "STATUS_CANCEL = 已取消")
    c.eq(_parse_sms_status_text("看不懂").get("status"), "unknown", "垃圾返回 = unknown")

    # ② get_code 轮询
    script = ScriptedSms(status_script=["STATUS_WAIT_CODE", "STATUS_WAIT_CODE", "STATUS_OK:4321"])
    prov = _provider(script)
    c.eq(prov.get_code("aid-1", timeout=5), "4321", "轮询到 STATUS_OK 后返回验证码")
    c.ok(script.actions().count("getStatusV2") >= 1, "优先用 getStatusV2")
    c.ok(script.actions().count("getStatus") >= 1, "v2 无码时兜底 getStatus(v1)")
    c.eq(script.payload_for("getStatusV2").get("id"), "aid-1", "查询带上了 activation id")
    c.ok("api_key" in script.payload_for("getStatusV2"), "每个请求都带 api_key")

    script = ScriptedSms()
    prov = _provider(script)
    prov.get_status_v2 = lambda aid: {"status": "ok", "code": "9999", "sms_key": "v2"}
    c.eq(prov.get_code("aid-2", timeout=3), "9999", "v2(JSON) 拿到码也能返回")

    script = ScriptedSms(status_script=["STATUS_WAIT_CODE"] * 50)
    prov = _provider(script)
    t0 = time.time()
    c.eq(prov.get_code("aid-3", timeout=1), "", "一直没码 → 返回空串")
    c.ok(time.time() - t0 < 8, "超时后立即返回，不能卡死")

    script = ScriptedSms(status_script=["STATUS_CANCEL"] * 5)
    prov = _provider(script)
    t0 = time.time()
    c.eq(prov.get_code("aid-4", timeout=5), "", "STATUS_CANCEL → 放弃")
    c.ok(time.time() - t0 < 5, "取消后不再死等")

    import sms_provider as _sp
    script = ScriptedSms(status_script=["STATUS_OK:1111"] * 5)
    prov = _provider(script)
    with _sp._SMS_CACHE_LOCK:
        _sp._SMS_CACHE = {"used_codes": ["1111"]}
    try:
        c.eq(prov.get_code("aid-5", timeout=1), "", "已在 used_codes 里的码不重复返回")
    finally:
        with _sp._SMS_CACHE_LOCK:
            _sp._SMS_CACHE = {}

    # ③ 租号参数
    script = ScriptedSms(number_script=["ACCESS_NUMBER:a1:5215550001"])
    prov = _provider(script, max_price=0.5)
    prov.get_number(service="dr", country="52", extra={
        "maxPrice": 0.4, "minPrice": 0.05, "phoneException": "5266,5281",
        "providerIds": "12", "exceptProviderIds": "77"})
    sent = script.payload_for("getNumber")
    c.ok(bool(sent), "V2 失败后确实回退到 getNumber(V1)")
    c.ok("getNumberV2" in script.actions(), "先试了 getNumberV2")
    c.eq(str(sent.get("service")), "dr", "service 传对")
    c.eq(str(sent.get("country")), "52", "country 传对")
    c.eq(str(sent.get("maxPrice")), "0.5", "maxPrice 用 provider 自己的配置")
    c.eq(str(sent.get("minPrice")), "0.05", "minPrice 透传")
    c.eq(str(sent.get("phoneException")), "5266,5281", "phoneException 透传")
    c.eq(str(sent.get("providerIds")), "12", "providerIds 透传")
    c.eq(str(sent.get("exceptProviderIds")), "77", "exceptProviderIds 透传")
    c.ok("api_key" in sent, "带上了 api_key")

    script = ScriptedSms(number_script=["ACCESS_NUMBER:a9:5215550009"])
    prov = _provider(script, max_price=0.1)
    prov.get_number(service="dr", country="8", extra={"maxPriceByCountry": {"8": 0.36}})
    sent = script.payload_for("getNumber")
    c.eq(str(sent.get("maxPrice")), "0.36", "按国家抬到该国实际价（不再被 WRONG_MAX_PRICE 拒）")
    c.ok("maxPriceByCountry" not in sent, "maxPriceByCountry 本身不发出去")

    script = ScriptedSms(number_script=["ACCESS_NUMBER:a2:5215550002"])
    prov = _provider(script)
    prov.get_number(service="dr", country="52", extra={})
    sent = script.calls[-1]
    for k in ("phoneException", "providerIds", "exceptProviderIds"):
        c.ok(k not in sent, f"空值时不发 {k}")

    # ④⑤⑥ controller 端到端
    from webui import db as _db0  # noqa: PLC0415
    _cur_cfg = _db0.get_sms_internal_config()
    fake = _FakeProvider()
    stats = []
    ctrl = PhoneCallbackController("smsbower", _cur_cfg, service="dr", country="52",
                                   blocked_prefixes=["5266"], prefix_max_retry=3,
                                   stats_fn=lambda pk, svc, ctry, ev: stats.append((pk, svc, ctry, ev)))
    ctrl.provider = fake
    ctrl.provider_key = "smsbower"
    c.eq(ctrl.get_phone(), "+528155550000", "命中排除前缀的号被丢弃，拿到干净号")
    c.eq(fake.cancelled, ["act-bad"], "被排除的号已 cancel")
    c.eq(len(fake.rented), 2, "重租了一次")
    _ex = fake.rented[0]["extra"]
    c.ok(not _ex.get("phoneException") and not _ex.get("providerIds") and not _ex.get("exceptProviderIds"),
         "无 effective_params 时不透传额外取号参数")
    c.ok(("smsbower", "dr", "52", "attempt") in stats, "租到号记了一次 attempt")
    c.eq(ctrl.get_code(timeout=5), "246810", "get_code 通过 controller 取到码")
    c.eq(fake.codes, ["act-good"], "取码用的是租到号的那个 activation")
    c.eq(fake.success, "act-good", "auto_report_success_on_code 生效")
    c.eq(stats.count(("smsbower", "dr", "52", "ok")), 1, "ok 只记一次（去重）")

    fake2 = _FakeProvider()
    ctrl2 = PhoneCallbackController("smsbower", {}, service="dr", country="52",
        effective_params_fn=lambda: {
            "platform_candidates": ["herosms", "smsbower"], "country_candidates": ["6", "52"],
            "max_price": 0.3, "min_price": 0.02, "phone_exception": "61",
            "provider_ids": "5", "except_provider_ids": "9", "source": "monitor"})
    import sms_provider as _sp2
    _real_create = _sp2.create_sms_provider
    _sp2.create_sms_provider = lambda pk, cfg: fake2
    try:
        ctrl2.provider = fake2
        ctrl2.provider_key = "herosms"
        ctrl2.get_phone()
        c.ok(ctrl2.provider_key in ("herosms", "smsbower"), "多平台候选被应用")
        c.eq(fake2.rented[0]["extra"].get("phoneException"), "61", "最优任务 phoneException 落到租号参数")
        c.eq(fake2.rented[0]["extra"].get("maxPrice"), 0.3, "最优任务价格落到租号参数")
        c.eq(fake2.rented[0]["candidates"], ["6", "52"], "最优任务国家候选被应用")
    finally:
        _sp2.create_sms_provider = _real_create

    def _boom():
        raise RuntimeError("监控线程炸了")

    fake3 = _FakeProvider()
    ctrl3 = PhoneCallbackController("smsbower", {}, service="dr", country="52", effective_params_fn=_boom)
    ctrl3.provider = fake3
    ctrl3.provider_key = "smsbower"
    try:
        ctrl3.get_phone()
        c.ok(True, "effective_params 异常时回退手动配置，不抛错")
    except Exception as e:
        c.ok(False, f"effective_params 异常时应回退而不是崩: {e}")

    s_, lst = normalize_blocked_prefixes("52:66, 52:81, +5266")
    c.eq(s_, "5266,5281", "归一化成平台要的 phoneException")
    c.ok(is_prefix_blocked("+52 66 1234 5678", lst), "本地复核能命中被排除前缀")
    c.ok(not is_prefix_blocked("+52 55 1234 5678", lst), "不在前缀里的号放行")

    # ⑦ 接码 controller 必须真的能创建出来
    from webui import db as _db, registrar as _reg
    fresh_db()
    _db.save_sms_config({"sms_enabled": "1", "sms_api_key_smsbower": "k",
                         "sms_service": "dr", "sms_country": "52", "sms_provider": "smsbower"})
    try:
        ctl = _reg._build_sms_callback("run-sms-test")
        c.ok(ctl is not None, "启用了接码且配了 Key → controller 必须能创建")
        c.ok(ctl is not None and callable(ctl.effective_params_fn), "controller 拿到了 effective_params_fn")
        c.ok(isinstance(ctl.effective_params_fn(), dict), "effective_params_fn 可调用且返回 dict")
    except Exception as e:
        c.ok(False, f"创建接码 controller 抛异常: {type(e).__name__}: {e}")

    # fresh_db() 会换掉 db 模块 → registrar 必须重新 import，否则它还读旧库
    fresh_db()
    from webui import db as _db6, registrar as _reg6
    _db6.save_sms_config({"sms_enabled": "1", "sms_country": "52"})
    try:
        c.ok(_reg6._build_sms_callback("run-sms-test2") is None, "没配 Key → 返回 None 不崩")
    except Exception as e:
        c.ok(False, f"没配 Key 时抛异常: {type(e).__name__}: {e}")

    # ⑧ 退号语义：先退成功再换号
    ctrl_ok = PhoneCallbackController("smsbower", {}, service="dr", country="52")
    ctrl_ok.provider = _CancelProvider(True)
    ctrl_ok.activation = _act()
    c.eq(ctrl_ok.cleanup(), True, "退号成功 → cleanup 返回 True（可以换号）")
    c.eq(ctrl_ok.provider.cancelled, ["aid-x"], "确实调用了退号")

    ctrl_bad = PhoneCallbackController("smsbower", {}, service="dr", country="52")
    ctrl_bad.provider = _CancelProvider(False)
    ctrl_bad.activation = _act()
    c.eq(ctrl_bad.cleanup(), False, "smsbower 退号失败 → cleanup 返回 False（停止换号）")

    ctrl_bad2 = PhoneCallbackController("smsbower", {}, service="dr", country="52")
    ctrl_bad2.provider = _CancelProvider(False)
    ctrl_bad2.activation = _act()
    c.eq(ctrl_bad2.mark_send_failed("被拒"), False, "mark_send_failed 退号失败 → 返回 False")

    script2 = ScriptedSms(status_script=["STATUS_WAIT_CODE"])
    prov_c = _provider(script2)
    c.eq(prov_c.cancel("aid-c"), False, "没回 ACCESS_CANCEL、状态也不是 CANCEL → 判失败")

    script3 = ScriptedSms()

    def _resp(params, **kw):
        p = dict(params)
        script3.calls.append(p)
        if p.get("action") in ("cancelActivation", "setStatus"):
            return FakeResp(text="ACCESS_CANCEL")
        return FakeResp(text="STATUS_WAIT_CODE")

    prov_c2 = _provider(script3)
    prov_c2._request = _resp
    c.eq(prov_c2.cancel("aid-d"), True, "平台回 ACCESS_CANCEL → 退号判成功")

    # ⑨ hero-sms 退号失败：主链路不中断 + 后台重试
    fresh_db()
    from webui import db as _db5, sms_refund

    class _HeroFailProvider:
        last_cancel_raw = "cancelActivation#1 异常: HTTPError: 409 Conflict"

        def cancel(self, aid):
            return False

    ctrl_h = PhoneCallbackController("herosms", {}, service="dr", country="52")
    ctrl_h.provider = _HeroFailProvider()
    ctrl_h.activation = _act()
    c.eq(ctrl_h.cleanup(), True, "hero-sms 退号失败 → 仍返回 True（主链路不中断，继续取号）")
    tasks = _db5.refund_task_list()
    c.eq(len(tasks), 1, "并挂了一个退号重试任务")
    c.eq(tasks[0]["status"], "pending", "任务初始为 pending")
    c.eq(int(tasks[0]["max_attempts"]), 3, "最多重试 3 次")
    c.ok(59 <= (float(tasks[0]["next_at"]) - float(tasks[0]["created_at"])) <= 61, "间隔 60s")

    tid = int(tasks[0]["id"])
    row = {}
    for _i in range(3):
        _db5.refund_task_claim(tid)
        row = _db5.refund_task_finish(tid, False, "409 Conflict")
    c.eq(row["status"], "gave_up", "3 次仍失败 → gave_up（不再继续退）")
    c.eq(int(row["attempts"]), 3, "记录了 3 次尝试")

    tid2 = _db5.refund_task_add("herosms", "aid-ok", "409")
    _db5.refund_task_claim(tid2)
    c.eq(_db5.refund_task_finish(tid2, True, "")["status"], "done", "退成功 → done")

    _db5.refund_task_add("herosms", "aid-worker", "409", delay_seconds=0)
    _real_provider_for = sms_refund._provider_for
    sms_refund._provider_for = lambda pk: _CancelProvider(True)
    try:
        stat = sms_refund.run_once()
        c.ok(stat["tried"] >= 1 and stat["ok"] >= 1, f"worker 认领并退号成功: {stat}")
    finally:
        sms_refund._provider_for = _real_provider_for

    ctrl_s = PhoneCallbackController("smsbower", {}, service="dr", country="52")
    ctrl_s.provider = _HeroFailProvider()
    ctrl_s.activation = _act()
    c.eq(ctrl_s.cleanup(), False, "smsbower 退号失败 → 仍返回 False（按老规矩停）")

    if _REAL_REQUESTS_GET is not None:
        import sms_provider as _sp3
        _sp3.requests.get = _REAL_REQUESTS_GET

    # ── ⑩ 接码配置实时生效（改完下一轮取号就用新值）──
    fresh_db()
    from webui import db as _db7, sms_prices as _sp7  # noqa: PLC0415
    _db7.save_sms_config({"sms_enabled": "1", "sms_api_key_smsbower": "k", "sms_service": "dr",
                          "sms_country": "52", "sms_allowed_countries": "52",
                          "sms_blocked_prefixes": "", "sms_max_price": "0.1",
                          "sms_use_monitor_best": "0"})
    seen_cfg = []
    fake_rt = _FakeProvider()
    _real_factory2 = _spmod.create_sms_provider

    def _factory_rt(pk, cfg):
        seen_cfg.append(dict(cfg))
        return fake_rt

    _spmod.create_sms_provider = _factory_rt
    try:
        ctrl_rt = PhoneCallbackController("smsbower", _db7.get_sms_internal_config(),
                                          service="dr", country="52",
                                          effective_params_fn=lambda: _sp7.effective_params())
        ctrl_rt.provider = fake_rt
        ctrl_rt.provider_key = "smsbower"
        ctrl_rt.get_phone()
        c.eq(fake_rt.rented[-1]["candidates"], ["52"], "改配置前：候选=配置里的 52")

        _db7.save_sms_config({"sms_allowed_countries": "7,16",
                              "sms_blocked_prefixes": "52:66,52:81",
                              "sms_max_price": "0.25"})
        ctrl_rt.get_phone()
        c.eq(fake_rt.rented[-1]["candidates"], ["7", "16"],
             "★ 同一个 controller：改完「允许国家」下一轮取号立刻生效")
        c.eq(fake_rt.rented[-1]["extra"].get("phoneException"), "5266,5281",
             "★ 改完「号码前缀排除」立刻生效")
        c.ok(seen_cfg and str(seen_cfg[-1].get("sms_max_price")) == "0.25",
             "★ 改完「最高价」后 provider 用新配置重建")
    finally:
        _spmod.create_sms_provider = _real_factory2

    _spmod.create_sms_provider = _real_factory

    return c

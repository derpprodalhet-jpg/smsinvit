"""SMS 接码 provider 抽象 + SmsBower / HeroSMS / SMSPool 实现。

设计参考：asz798838958/GeniusFKoai 的 core/base_sms.py，但裁剪掉浏览器回调相关代码、
仅保留纯协议注册需要的两段流程：
    1) rent number    → provider.get_number(service=..., country=...)
    2) wait sms code  → provider.get_code(activation_id, timeout=...)
    3) 成功/失败       → provider.report_success / cancel / mark_code_failed

项目经验名单只表示历史观察，不能保证 OpenAI 的验证方式或短信送达。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

from http_client import normalize_proxy
from smsinvit.invitation.safety import guarded_requests as requests
from smsinvit.invitation.safety import redact as _redact_text
from smsinvit.invitation.sms_state import SmsBudgetExhausted, RentalUncertainError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


@dataclass
class SmsActivation:
    """一次手机号租用的句柄。"""
    activation_id: str
    phone_number: str          # E.164 格式，带 + 前缀
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """接码 provider 抽象基类。"""

    auto_report_success_on_code = False  # 由业务侧确认，平台结算单独记录

    @abstractmethod
    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None,
                    extra: Optional[dict] = None) -> SmsActivation:
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        ...

    def get_balance(self) -> float:
        """查询余额（货币随平台）。"""
        raise NotImplementedError

    def report_success(self, activation_id: str) -> bool:
        """查询或确认供应商完成状态。"""
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """记录业务失败；同号不会重发或再验证。"""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> bool:
        """返回供应商是否明确取消；待退款持久化由 controller 负责。"""
        return True

    def mark_send_succeeded(self, activation_id: str) -> None:
        """业务侧已成功触发短信发送（add-phone/send 200）。"""
        return None

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """兼容旧调用；单号模式不注册重发。"""
        return None


# ---------------------------------------------------------------------------
# 国家 ID → 中文名映射（sms-activate.org 协议系，SmsBower 共用）
# ---------------------------------------------------------------------------

SMS_COUNTRY_NAMES_CN: dict[str, str] = {
    "0": "俄罗斯", "1": "乌克兰", "2": "哈萨克斯坦", "3": "中国", "4": "菲律宾",
    "5": "缅甸", "6": "印度尼西亚", "7": "马来西亚", "8": "肯尼亚", "9": "坦桑尼亚",
    "10": "越南", "11": "吉尔吉斯斯坦", "12": "美国(虚拟)", "13": "以色列", "14": "香港",
    "15": "波兰", "16": "英国", "17": "马达加斯加", "18": "刚果(布)", "19": "尼日利亚",
    "20": "澳门", "21": "埃及", "22": "印度", "23": "爱尔兰", "24": "柬埔寨",
    "25": "老挝", "26": "海地", "27": "科特迪瓦", "28": "冈比亚", "29": "塞尔维亚",
    "30": "也门", "31": "南非", "32": "罗马尼亚", "33": "哥伦比亚", "34": "爱沙尼亚",
    "35": "阿塞拜疆", "36": "加拿大", "37": "摩洛哥", "38": "加纳", "39": "阿根廷",
    "40": "乌兹别克斯坦", "41": "喀麦隆", "42": "乍得", "43": "德国", "44": "立陶宛",
    "45": "克罗地亚", "46": "瑞典", "47": "伊拉克", "48": "荷兰", "49": "拉脱维亚",
    "50": "奥地利", "51": "白俄罗斯", "52": "泰国", "53": "沙特阿拉伯", "54": "墨西哥",
    "55": "台湾", "56": "西班牙", "57": "伊朗", "58": "阿尔及利亚", "59": "斯洛文尼亚",
    "60": "孟加拉国", "61": "塞内加尔", "62": "土耳其", "63": "捷克", "64": "斯里兰卡",
    "65": "秘鲁", "66": "巴基斯坦", "67": "新西兰", "68": "几内亚", "69": "马里",
    "70": "委内瑞拉", "71": "埃塞俄比亚", "72": "蒙古", "73": "巴西", "74": "阿富汗",
    "75": "乌干达", "76": "安哥拉", "77": "塞浦路斯", "78": "法国", "79": "巴布亚新几内亚",
    "80": "莫桑比克", "81": "尼泊尔", "82": "比利时", "83": "保加利亚", "84": "匈牙利",
    "85": "摩尔多瓦", "86": "意大利", "87": "巴拉圭", "88": "洪都拉斯", "89": "突尼斯",
    "90": "尼加拉瓜", "91": "东帝汶", "92": "玻利维亚", "93": "哥斯达黎加", "94": "危地马拉",
    "95": "阿联酋", "96": "津巴布韦", "97": "波多黎各", "98": "苏丹", "99": "多哥",
    "100": "科威特", "101": "萨尔瓦多", "102": "利比亚", "103": "牙买加", "104": "特立尼达和多巴哥",
    "105": "厄瓜多尔", "106": "斯威士兰", "107": "阿曼", "108": "波黑", "109": "多米尼加",
    "110": "叙利亚", "111": "卡塔尔", "112": "巴拿马", "113": "古巴", "114": "毛里塔尼亚",
    "115": "塞拉利昂", "116": "约旦", "117": "葡萄牙", "118": "巴巴多斯", "119": "布隆迪",
    "120": "贝宁", "121": "文莱", "122": "巴哈马", "123": "博茨瓦纳", "124": "伯利兹",
    "125": "中非", "126": "多米尼克", "127": "格林纳达", "128": "格鲁吉亚", "129": "希腊",
    "130": "几内亚比绍", "131": "圭亚那", "132": "冰岛", "133": "科摩罗", "134": "利比里亚",
    "135": "莱索托", "136": "马拉维", "137": "纳米比亚", "138": "尼日尔", "139": "卢旺达",
    "140": "斯洛伐克", "141": "苏里南", "142": "塔吉克斯坦", "143": "摩纳哥", "144": "巴林",
    "145": "留尼汪岛", "146": "赞比亚", "147": "亚美尼亚", "148": "索马里", "149": "刚果(金)",
    "150": "智利", "151": "布基纳法索", "152": "黎巴嫩", "153": "加蓬", "154": "阿尔巴尼亚",
    "155": "乌拉圭", "156": "毛里求斯", "157": "不丹", "158": "马尔代夫", "159": "瓜德罗普岛",
    "160": "土库曼斯坦", "161": "法属圭亚那", "162": "芬兰", "163": "圣卢西亚", "164": "卢森堡",
    "165": "圣文森特", "166": "赤道几内亚", "167": "吉布提", "168": "安提瓜和巴布达", "169": "开曼群岛",
    "170": "黑山", "171": "丹麦", "172": "瑞士", "173": "挪威", "174": "澳大利亚",
    "175": "厄立特里亚", "176": "南苏丹", "177": "圣多美", "178": "阿鲁巴岛", "179": "蒙特塞拉特",
    "180": "安圭拉岛", "181": "北马其顿", "182": "塞舌尔", "183": "新喀里多尼亚", "184": "佛得角",
    "185": "美国(实体)", "186": "巴勒斯坦", "187": "美国", "188": "中国", "189": "韩国",
    "190": "科特迪瓦", "191": "日本",
}


def _otp_logging_enabled() -> bool:
    """是否把验证码打进日志。读任务冻结设置（sms_log_otp），默认开。

    延迟 import：runtime 会拉起 mysql/webui，模块级 import 容易成环。
    """
    try:
        from smsinvit.invitation.runtime import TASK_SETTINGS

        settings = TASK_SETTINGS.get() or {}
        return str(settings.get("sms_log_otp", "1")).strip().lower() not in (
            "0", "false", "no", "off", "",
        )
    except Exception:  # noqa: BLE001
        return True


def country_label(country_id) -> str:
    """返回 '52 泰国' 这样的展示标签。"""
    cid = str(country_id or "").strip()
    name = SMS_COUNTRY_NAMES_CN.get(cid, "")
    return f"{cid} {name}".strip()


# ---------------------------------------------------------------------------
# SmsBower / SMSBower —— 共享 API 协议
# ---------------------------------------------------------------------------

SMS_DEFAULT_SERVICE = "dr"
SMS_DEFAULT_COUNTRY = "52"  # Thailand，旧配置默认值
# 项目经验名单来自历史观察，不是 OpenAI 官方国家许可列表。
OPENAI_SMS_COUNTRIES = {"52"}
# 本引擎的报价（sms_max_price / 行情价 / activationCost）按既有约定记 USD：
# 应用侧 sms_sessions.activation_cost 同样按 USD 统计，跨币种不求和。
SMS_COST_CURRENCY = "USD"


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _parse_sms_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1].strip().strip("\"\'")}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _make_sms_candidate(activation_id: str, source: str, code) -> Optional[dict]:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": hashlib.sha256(
            f"{activation_id}:{code}".encode("utf-8")
        ).hexdigest(),
    }


# ── 价格/库存归一化 + 号码前缀过滤（codex_invit）──────────────


def _to_float(v):
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(v) if v is not None and str(v).strip() != "" else 0
    except (TypeError, ValueError):
        return 0


def _safe_cost(value) -> Optional[float]:
    """供应商金额必须是有限非负数；拿不到就是 None，绝不猜一个 0。

    getNumberV2 的 activationCost 可能缺失、为空串、NaN/inf，或被写成
    dict/list（协议变化）。这些都不该进入成本统计。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 4)


def _quoted_cost(extra, country) -> Optional[float]:
    """getNumber（ACCESS_NUMBER:aid:phone）没有金额，用下单前的行情价回退。

    报价优先取该国家的上限（maxPriceByCountry），其次全局 maxPrice，最后
    minPrice：这三者都是下单时已经在用的价格上限。全都拿不到时返回 None
    （成本未知），而不是把未知记成 0。
    """
    extra = extra if isinstance(extra, dict) else {}
    per_country = extra.get("maxPriceByCountry")
    per_country = per_country if isinstance(per_country, dict) else {}
    for candidate in (per_country.get(str(country)), extra.get("maxPrice"), extra.get("minPrice")):
        cost = _safe_cost(candidate)
        if cost is not None:
            return cost
    return None


def _amount_log(cost, currency) -> str:
    """日志里的金额后缀；金额未知时不写任何字。"""
    if cost is None:
        return ""
    text = ("%.4f" % float(cost)).rstrip("0").rstrip(".")
    return "，金额=%s%s" % (text, (" " + currency) if currency else "")


def _stage_amount(cost, currency) -> dict:
    """阶段事件里携带的金额字段；金额未知时不带字段。"""
    if cost is None:
        return {}
    return {"amount": float(cost), "currency": str(currency or "") or None}


def _activation_cost(activation) -> tuple[Optional[float], str]:
    """取回租号时确认的成本与币种（存在句柄 metadata 里）。"""
    metadata = getattr(activation, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return _safe_cost(metadata.get("activationCost")), str(metadata.get("currency") or SMS_COST_CURRENCY)


def _norm_price_row(country_id, value) -> Optional[dict]:
    """把平台返回的一条国家记录归一化成统一行。

    `physical_count` 是**实体卡（真卡）数量** —— 「排除虚拟号」就靠它。
    接口/套餐没有这个字段时置 None，调用方退回 `count` 并显式告警，
    绝不假装拿到了实体卡数据。
    """
    if not isinstance(value, dict):
        return None
    price = value.get("price") or value.get("cost") or value.get("retail_price")
    p = _to_float(price)
    if p is None:
        return None
    count = value.get("count") or value.get("qty") or value.get("available") or 0
    physical = next((value[key] for key in ("physicalCount", "physical_count", "physical", "physicalQty")
                     if key in value and value[key] is not None), None)
    min_price = value.get("minPrice") or value.get("min_price")
    return {
        "country": str(country_id),
        "price": p,
        "count": _to_int(count),
        "physical_count": _to_int(physical) if physical is not None else None,
        "min_price": _to_float(min_price),
    }


def digits_only(phone: str) -> str:
    """号码 → 纯数字（去掉 +、空格、括号、横线）。"""
    return re.sub(r"\D", "", str(phone or ""))


def normalize_blocked_prefixes(raw: str) -> tuple[str, list[str]]:
    """把用户填的「排除前缀」归一化成平台要的 phoneException。

    接受两种写法（混着写也行）：
        52:66,52:81     国家:前缀
        +5266, 5281     直接写完整前缀（国家码 + 3~6 位掩码）
    官方格式是「country code and 3 to 6 digits of the mask (e.g. 7918,7900111)」，
    即 66 + 66 → 6666 这样拼起来、逗号分隔、不带 +。

    返回 (phoneException 字符串, 前缀列表)。
    """
    items: list[str] = []
    for chunk in re.split(r"[,;\s]+", str(raw or "")):
        s = chunk.strip()
        if not s:
            continue
        if ":" in s:
            cc, rest = s.split(":", 1)
            s = f"{cc.strip()}{rest.strip()}"
        s = digits_only(s)
        if s:
            items.append(s)
    uniq = list(dict.fromkeys(items))
    return ",".join(uniq), uniq


def is_prefix_blocked(phone: str, prefixes: Optional[list[str]]) -> bool:
    """取号后的本地复核：号码是否命中被排除的前缀。

    平台侧已经传了 phoneException，但不同平台/供货商对它的支持程度不一，
    所以拿到号之后再本地核一遍，命中就 cancel 退款重租 —— 双保险。
    """
    d = digits_only(phone)
    if not d:
        return False
    return any(d.startswith(p) for p in (prefixes or []) if p)


def collect_json_fields(data, prefix: str = "", out: Optional[list[str]] = None) -> list[str]:
    """自检用：把响应里的字段路径收集出来（只看名字，不看值）。"""
    if out is None:
        out = []
    if isinstance(data, dict):
        for k, v in list(data.items())[:60]:
            path = f"{prefix}.{k}" if prefix else str(k)
            if path not in out:
                out.append(path)
            if isinstance(v, (dict, list)):
                collect_json_fields(v, path, out)
    elif isinstance(data, list):
        for item in data[:3]:
            collect_json_fields(item, f"{prefix}[]", out)
    return out


class SmsBowerProvider(BaseSmsProvider):
    """SMS-Activate 协议适配器。每个成功订单只交给一个业务尝试。"""

    DEFAULT_BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    auto_report_success_on_code = False
    NO_ORDER_ERRORS = frozenset({"NO_NUMBERS", "NO_BALANCE", "BAD_ACTION", "BAD_SERVICE",
        "BAD_COUNTRY", "BAD_KEY", "NO_KEY", "BANNED_KEY", "WRONG_EXCEPTION_PHONE", "WRONG_MAX_PRICE"})

    def __init__(self, api_key: str, *, base_url: str = "", default_service: str = SMS_DEFAULT_SERVICE,
                 default_country: str = SMS_DEFAULT_COUNTRY, max_price: float = -1,
                 proxy: Optional[str] = None, state=None, **_obsolete):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").strip() or self.DEFAULT_BASE_URL
        self.default_service = str(default_service or SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or "").strip()
        self.max_price = _safe_float(max_price, 0)
        self._proxy = normalize_proxy(proxy)
        self._proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        if state is None:
            from smsinvit.invitation import sms_state
            state = sms_state
        self.state = state
        self.platform = "herosms" if "hero-sms" in self.base_url else "smsbower"
        self.last_code_result = None
        self.last_cancel_raw = ""
        self.current_activation = None
        self._delivered = set()
        self.last_cancel_auto_completed = False

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30):
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        response = requests.get(self.base_url, params=payload, timeout=timeout, proxies=self._proxies)
        response.raise_for_status()
        return response

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"{self.platform} getBalance 失败: {text[:100]}")

    def get_prices(self, service: Optional[str] = None, country=None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = str(country)
        response = self._request(params)
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.platform} getPrices 返回非报价响应") from exc
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError(f"{self.platform} getPrices 返回结构异常")
        return data

    def get_top_countries(self, service: Optional[str] = None) -> list[dict]:
        code = str(service or self.default_service or SMS_DEFAULT_SERVICE)
        rows = []
        for cid, services in self.get_prices(code).items():
            if isinstance(services, dict):
                row = _norm_price_row(cid, services.get(code))
                if row is not None:
                    rows.append(row)
        rows.sort(key=lambda row: (row["price"], -row["count"]))
        return rows

    @staticmethod
    def _parse_top_countries(data) -> list[dict]:
        items = data.get("data", data) if isinstance(data, dict) else data
        iterable = items.items() if isinstance(items, dict) else ((r.get("country"), r) for r in (items or []) if isinstance(r, dict))
        return [row for cid, value in iterable if cid is not None and (row := _norm_price_row(cid, value)) is not None]

    def get_best_country(self, service=None, *, min_stock=20, max_price=0,
                         strict_whitelist=False, allowed_countries=None):
        allowed = set(str(c) for c in (allowed_countries or []))
        for row in self.get_top_countries(service):
            cid = row["country"]
            if allowed and cid not in allowed:
                continue
            if strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                continue
            if row["count"] < min_stock or (max_price > 0 and row["price"] > max_price):
                continue
            return cid
        return None

    @staticmethod
    def _no_order_error(text):
        return str(text or "").strip().split(":", 1)[0] in SmsBowerProvider.NO_ORDER_ERRORS

    def _unknown_purchase(self, rental, reason, cause=None):
        try:
            self.state.rental_unknown(rental, reason)
        except Exception as exc:
            raise RentalUncertainError("租号结果不明且登记失败，停止自动购买，请核对供应商订单") from exc
        raise RentalUncertainError(reason + "，停止自动购买，请核对供应商订单") from cause

    def _request_number_single_action(self, action, service, country, extra=None):
        params = {"action": action, "service": service}
        if country not in (None, ""):
            params["country"] = str(country)
        extra = extra or {}
        ceiling = _safe_float(extra.get("maxPrice"), 0)
        per_country = _safe_float((extra.get("maxPriceByCountry") or {}).get(str(country)), 0)
        caps = [v for v in (self.max_price, ceiling, per_country) if v > 0]
        if caps:
            params["maxPrice"] = round(min(caps), 4)
        supported = ("pool",) if self.platform == "smspool" else ("minPrice", "phoneException", "providerIds", "exceptProviderIds")
        for key in supported:
            value = extra.get(key)
            if value not in (None, "", 0, "0"):
                params[key] = value
        rental = self.state.reserve_rental(self.platform, action)
        if rental is None:
            raise SmsBudgetExhausted("本次运行已达到 3 单租号预算")
        try:
            response = self._request(params)
        except Exception as exc:
            text = str(getattr(getattr(exc, "response", None), "text", "") or "")
            if self._no_order_error(text):
                self.state.rental_no_order(rental)
                raise RuntimeError(text[:100]) from exc
            self._unknown_purchase(rental, "租号请求结果不明", exc)
        text = response.text.strip()
        if self._no_order_error(text):
            self.state.rental_no_order(rental)
            raise RuntimeError(text[:100])
        data = None
        if action == "getNumberV2":
            try:
                data = response.json()
            except ValueError:
                pass
        elif text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":", 2)
            if len(parts) == 3:
                data = {"activationId": parts[1], "phoneNumber": parts[2]}
        if not isinstance(data, dict) or not data.get("activationId") or not digits_only(str(data.get("phoneNumber") or "")):
            self._unknown_purchase(rental, "租号响应无法确认订单与号码")
        aid = str(data["activationId"])
        phone = self._format_phone(data)
        # getNumberV2 的 JSON 带供应商真实金额 activationCost；getNumber 只有
        # "ACCESS_NUMBER:aid:phone"，回退到下单前的行情价（见 _quoted_cost）。
        cost = _safe_cost(data.get("activationCost")) if action == "getNumberV2" else None
        if cost is None:
            cost = _quoted_cost(extra, country)
        currency = SMS_COST_CURRENCY
        try:
            self.state.rental_created(rental, aid, phone, activation_cost=cost, currency=currency)
        except Exception as exc:
            raise RentalUncertainError("供应商已创建订单，但本地登记失败，停止自动购买") from exc
        return data, cost, currency

    @staticmethod
    def _format_phone(info):
        raw = digits_only(str(info.get("phoneNumber") or ""))
        cc = digits_only(str(info.get("countryPhoneCode") or ""))
        if cc and not raw.startswith(cc):
            raw = cc + raw
        return "+" + raw

    def get_number(self, *, service, country="", country_candidates=None, extra=None):
        service = str(service or self.default_service or SMS_DEFAULT_SERVICE)
        countries = country_candidates or [str(country or self.default_country)]
        actions = ("getNumberV2", "getNumber") if self.platform == "smsbower" else ("getNumber",)
        failures = []
        for cid in countries:
            for action in actions:
                try:
                    info, cost, currency = self._request_number_single_action(action, service, str(cid), extra)
                except (SmsBudgetExhausted, RentalUncertainError):
                    raise
                except RuntimeError as exc:
                    failures.append(str(exc))
                    continue
                act = SmsActivation(str(info["activationId"]), self._format_phone(info), str(cid),
                                    metadata={"activationCost": cost, "currency": currency})
                self.current_activation = act
                self.last_code_result = None
                return act
        raise RuntimeError(f"{self.platform} 没有可购买的候选号码: {' | '.join(failures[-3:])}")

    def get_status(self, activation_id, *, timeout=30):
        return _parse_sms_status_text(self._request({"action": "getStatus", "id": activation_id}, timeout=timeout).text)

    def get_status_v2(self, activation_id):
        response = self._request({"action": "getStatusV2", "id": activation_id})
        try:
            data = response.json()
        except ValueError:
            return _parse_sms_status_text(response.text)
        if isinstance(data, str):
            return _parse_sms_status_text(data)
        if isinstance(data, dict):
            if isinstance(data.get("status"), str):
                parsed = _parse_sms_status_text(data["status"])
                if parsed["status"] != "unknown":
                    return parsed
            for channel in ("sms", "call"):
                value = data.get(channel)
                if isinstance(value, dict):
                    candidate = _make_sms_candidate(activation_id, channel, value.get("code"))
                    if candidate:
                        return candidate
        return {"status": "wait_code"}

    def wait_for_code(self, activation_id, *, timeout=80, poll=3, **_obsolete):
        deadline = time.monotonic() + min(80, max(0, timeout))
        while time.monotonic() < deadline:
            # 三个平台都支持 V1。避免探测额外动作，也不重发同号短信。
            try:
                result = self.get_status(activation_id, timeout=max(.1, min(30, deadline-time.monotonic())))
                if result["status"] == "cancel":
                    return None
                if result["status"] == "ok" and result.get("code"):
                    return result
            except Exception as exc:
                logger.debug("%s 收码查询失败: %s: %s", self.platform, type(exc).__name__,
                             _redact_text(str(exc))[:200])
            time.sleep(min(poll, max(0, deadline - time.monotonic())))
        return None

    def get_code(self, activation_id, *, timeout=80):
        if activation_id in self._delivered:
            return ""
        result = self.wait_for_code(activation_id, timeout=timeout)
        self.last_code_result = result
        if result:
            self._delivered.add(activation_id)
        return str((result or {}).get("code") or "")

    def request_resend_sms(self, activation_id):
        return False

    def set_resend_callback(self, callback):
        return None

    def mark_code_failed(self, activation_id, reason=""):
        return None

    def mark_send_succeeded(self, activation_id):
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 1})
        except Exception:
            pass  # 此操作可选；业务发码已成功，不能再次发码。

    def report_success(self, activation_id):
        try:
            response = self._request({"action": "setStatus", "id": activation_id, "status": 6})
            return response.text.strip() == "ACCESS_ACTIVATION"
        except Exception:
            return False

    def cancel(self, activation_id):
        evidence = []
        self.last_cancel_auto_completed = False
        try:
            response = self._request({"action": "setStatus", "id": activation_id, "status": 8})
            text = response.text.strip()
            evidence.append(text[:100])
            if text == "ACCESS_CANCEL":
                self.last_cancel_raw = " | ".join(evidence)
                return True
        except Exception as exc:
            evidence.append(type(exc).__name__)
        try:
            status = self.get_status(activation_id)["status"]
            evidence.append("getStatus=" + status)
            self.last_cancel_auto_completed = self.platform == "smspool" and status == "ok"
            confirmed = status == "cancel"
        except Exception as exc:
            evidence.append(type(exc).__name__)
            confirmed = False
        self.last_cancel_raw = " | ".join(evidence)
        return confirmed

    def mark_send_failed(self, activation_id, reason=""):
        return self.cancel(activation_id)

    def raw_call(self, action, **params):
        if action not in {"getBalance", "getPrices", "getPricesV2", "getPricesV3", "getTopCountriesByServiceRank", "getTopCountriesByService"}:
            raise ValueError("接口自检仅允许只读动作")
        started = time.monotonic()
        response = self._request({"action": action, **params})
        try:
            data = response.json()
        except ValueError:
            data = None
        return {"action": action, "status": response.status_code,
                "elapsed_ms": round((time.monotonic()-started)*1000), "json": data,
                "text": response.text[:20000] if data is None else "",
                "fields": collect_json_fields(data) if isinstance(data, (dict, list)) else []}


class SmsPoolProvider(SmsBowerProvider):
    DEFAULT_BASE_URL = "https://api.smspool.net/stubs/handler_api"

    def __init__(self, api_key, **kwargs):
        super().__init__(api_key, **kwargs)
        self.platform = "smspool"

    def mark_send_succeeded(self, activation_id):
        return None

    def report_success(self, activation_id):
        # 兼容接口在收码后自动完成。后台确认也必须重新看到已收码状态。
        try:
            return self.get_status(activation_id)["status"] == "ok"
        except Exception:
            return False


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """从配置创建 provider 实例。

    provider_key: smsbower / herosms / smspool
    config 字段：sms_api_key_{provider_key}（各平台独立 Key）/
                sms_country / sms_service / sms_max_price
    """
    pk = (provider_key or "").lower().strip()
    api_key = str(
        config.get(f"sms_api_key_{pk}") or ""
    ).strip()
    if not api_key:
        raise RuntimeError(f"{pk} 未配置 API Key")
    country = str(config.get("sms_country") or "").strip()
    service = str(config.get("sms_service") or "").strip() or "dr"
    # 接码 API 请求走的代理：复用全局 proxy（registrar 注入注册流程的代理），
    # 也允许调用方显式传 sms_proxy 覆盖（保留扩展点，目前 WebUI 不暴露）。
    proxy = (str(config.get("sms_proxy") or config.get("proxy") or "")).strip() or None
    max_price = _safe_float(config.get("sms_max_price"), -1)

    if pk in ("smsbower", "sms_bower"):
        return SmsBowerProvider(api_key=api_key,
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                proxy=proxy,
                                state=config.get("_sms_state"))
    if pk in ("herosms", "hero_sms"):
        return SmsBowerProvider(api_key=api_key,
                                base_url="https://hero-sms.com/stubs/handler_api.php",
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                proxy=proxy,
                                state=config.get("_sms_state"))
    if pk == "smspool":
        return SmsPoolProvider(api_key=api_key,
                               default_service=service,
                               default_country=country or SMS_DEFAULT_COUNTRY,
                               max_price=max_price,
                               proxy=proxy, state=config.get("_sms_state"))
    raise RuntimeError(f"未知接码服务: {provider_key}")


class PhoneCallbackController:
    """租号、单次发码/验证与平台结算分离；所有购买共享运行预算。"""

    def __init__(self, provider_key, config, *, service="dr", country="", log_fn=None,
                 auto_select_country=False, platform_candidates=None, country_candidates=None,
                 effective_params_fn=None, stats_fn=None, blocked_prefixes=None, **_obsolete):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.platform_candidates = platform_candidates or []
        self.injected_countries = country_candidates or []
        self.effective_params_fn = effective_params_fn
        self.stats_fn = stats_fn
        self.blocked_prefixes = blocked_prefixes or []
        self.provider = None
        self.activation = None
        self.completed = False
        self._stats_reported = False
        self._code_delivered = False
        self._local_rentals = 0
        self._refreshed_quotes = False

    def _stats(self, event):
        if self.stats_fn and self.activation:
            try:
                self.stats_fn(self.provider_key, self.service, self.activation.country, event)
            except Exception:
                pass

    def _effective(self):
        return self.effective_params_fn() if self.effective_params_fn else {}

    def _provider(self, platform=""):
        pk = platform or self.provider_key
        if self.provider is None or self.provider_key != pk:
            self.provider = create_sms_provider(pk, self.config)
            self.provider_key = pk
        return self.provider

    def _refresh_config(self):
        if self.config.get("_sms_state") is not None:
            return
        from webui import db
        fresh = db.get_sms_internal_config()
        self.config = dict(fresh)
        self.service = str(fresh.get("sms_service") or "dr")
        self.country = str(fresh.get("sms_country") or "")
        self.blocked_prefixes = normalize_blocked_prefixes(fresh.get("sms_blocked_prefixes"))[1]
        self.provider = None

    def get_phone(self):
        if self.activation is not None:
            raise RentalUncertainError("上个号码仍未清理，停止购买新号")
        self._refresh_config()
        params = self._effective() or {}
        if self.effective_params_fn and not params.get("rental_candidates") and not self._refreshed_quotes:
            self._refreshed_quotes = True
            try:
                from webui.sms_prices import MONITOR
                MONITOR.refresh_once()
                params = self._effective() or {}
            except Exception as exc:
                logger.warning("接码报价刷新失败: %s: %s", type(exc).__name__,
                               _redact_text(str(exc))[:200])
        if self.effective_params_fn and not params.get("rental_candidates"):
            raise RuntimeError("当前没有新鲜且符合配置的接码报价")
        candidates = params.get("rental_candidates") or [
            {"platform": pk, "country": country, "max_price": params.get("max_price", 0)}
            for pk in (self.platform_candidates or [self.provider_key])
            for country in (self.injected_countries or [self.country])]
        from webui.sms_prices import selected_platforms, platform_key_configured
        scope = {pk for pk in selected_platforms(self.config) if platform_key_configured(self.config, pk)}
        candidates = [row for row in candidates if row.get("platform") in scope]
        if not candidates:
            raise RuntimeError("当前所选接码平台没有可购买的候选号码")
        extra = {"minPrice": params.get("min_price", 0),
                 "phoneException": params.get("phone_exception", ""),
                 "providerIds": params.get("provider_ids", ""),
                 "exceptProviderIds": params.get("except_provider_ids", "")}
        self.blocked_prefixes = params.get("blocked_prefixes", self.blocked_prefixes)
        last_error = None
        for candidate in candidates:
            pk, cid = candidate["platform"], str(candidate["country"])
            while self._local_rentals < 3:
                try:
                    provider = self._provider(pk)
                    act = provider.get_number(service=self.service, country=cid, country_candidates=[cid],
                        extra={**extra, "maxPrice": candidate.get("max_price", 0)})
                except (SmsBudgetExhausted, RentalUncertainError):
                    raise
                except Exception as exc:
                    last_error = exc
                    break
                self._local_rentals += 1
                self.activation = act
                self.completed = False
                self._stats_reported = False
                self._code_delivered = False
                self._stats("attempt")
                try:
                    claimed = provider.state.claim_phone(act.phone_number)
                except Exception as exc:
                    raise RentalUncertainError("号码去重登记失败，停止发码") from exc
                if claimed and not is_prefix_blocked(act.phone_number, self.blocked_prefixes):
                    cost, currency = _activation_cost(act)
                    self.log(f"已租到新号码，平台={pk} 国家={country_label(cid)} "
                             f"号码={act.phone_number}{_amount_log(cost, currency)}")
                    try:
                        from smsinvit.invitation.runtime import emit_task_stage

                        emit_task_stage("sms_phone", **_stage_amount(cost, currency))
                    except Exception:  # noqa: BLE001
                        pass
                    return act.phone_number
                self.log("号码已使用或命中排除前缀，取消本单并按剩余预算换新号")
                if not self.cleanup():
                    raise RentalUncertainError("待取消订单登记失败，停止购买新号")
            if self._local_rentals >= 3:
                raise SmsBudgetExhausted("本次运行已达到 3 单租号预算")
        raise RuntimeError(f"没有可购买的候选号码: {last_error or '无候选'}")

    def get_code(self, timeout=80):
        if not self.activation:
            raise RuntimeError("未先租号")
        if self._code_delivered:
            return ""
        self._code_delivered = True
        code = self.provider.get_code(self.activation.activation_id, timeout=min(80, timeout))
        # 主人要能在日志里直接看到"哪个号码、收到什么验证码"，否则出问题只能靠猜。
        # 可用「接码 → 打印验证码」关掉（sms_log_otp=0）。
        if code:
            phone = str(getattr(self.activation, "phone_number", "") or "")
            cost, currency = _activation_cost(self.activation)
            if _otp_logging_enabled():
                # 金额跟在号码后面：同一单租号的成本和验证码能对上。
                self.log(f"已收到验证码：{code}（号码={phone}{_amount_log(cost, currency)}）")
            try:
                from smsinvit.invitation.runtime import emit_task_stage

                emit_task_stage("sms_code", **_stage_amount(cost, currency))
            except Exception:  # noqa: BLE001
                pass
        return code

    def report_success(self):
        if not self.activation or not self.provider or self.completed:
            return
        # 先持久化结算待确认，再尝试供应商完成；不会把业务成功改成失败。
        self.completed = True
        aid = self.activation.activation_id
        try:
            self.provider.state.order_state(self.provider_key, aid, "finish_pending")
        except Exception:
            self.log("业务验证成功，平台结算登记失败，需要人工核对已创建订单")
            if not self._stats_reported:
                self._stats("ok")
                self._stats_reported = True
            return
        try:
            confirmed = bool(self.provider.report_success(aid))
            if confirmed:
                self.provider.state.order_state(self.provider_key, aid, "completed")
        except Exception:
            confirmed = False
        if not self._stats_reported:
            self._stats("ok")
            self._stats_reported = True
        self.log("业务验证成功，平台结算已确认" if confirmed else "业务验证成功，平台结算待确认")
        if confirmed:
            try:
                from smsinvit.invitation.runtime import emit_task_stage

                emit_task_stage("sms_settled")
            except Exception:  # noqa: BLE001
                pass

    def mark_code_failed(self, reason=""):
        if not self._stats_reported:
            self._stats("fail")
            self._stats_reported = True

    def mark_send_succeeded(self):
        if self.activation and self.provider:
            self.provider.mark_send_succeeded(self.activation.activation_id)

    def mark_send_failed(self, reason=""):
        return self.cleanup()

    def set_resend_callback(self, callback):
        return None

    def cleanup(self):
        if not self.activation:
            self._clear()
            return True
        if self.completed:
            self._clear()
            return True
        provider, aid, pk = self.provider, self.activation.activation_id, self.provider_key
        try:
            confirmed = bool(provider.cancel(aid))
            if confirmed:
                provider.state.order_state(pk, aid, "cancelled")
                self.log("号码取消已确认")
            elif getattr(provider, "last_cancel_auto_completed", False):
                provider.state.order_state(pk, aid, "completed")
                self.log("本次业务失败，SMSPool 已在收码后结算，退款未成立")
            else:
                from webui import sms_refund
                reason = provider.last_cancel_raw or "供应商未确认取消"
                provider.state.order_state(pk, aid, "cancel_pending", reason)
                if not sms_refund.enqueue(pk, aid, reason):
                    return False
                self.log("退款待确认，已登记后台处理；可按剩余预算换新号")
        except Exception as exc:
            logger.warning("待取消订单登记失败: %s: %s", type(exc).__name__,
                           _redact_text(str(exc))[:200])
            return False
        if not self._stats_reported:
            self._stats("fail")
        self._clear()
        return True

    def _clear(self):
        self.activation = None
        self.provider = None
        self.completed = False
        self._stats_reported = False
        self._code_delivered = False

    def _release_lock(self):
        return None

"""Bounded, read-only proxy checks through the normal outbound guard."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import socket
import time
from urllib.parse import urlsplit

from http_client import create_http_session, normalize_proxy


DEFAULT_TARGET = "https://api.ipify.org?format=json"


def _alternate(proxy):
    scheme, authority = proxy.split("://", 1)
    if scheme in ("http", "https"):
        return ["socks5h://" + authority, ("https" if scheme == "http" else "http") + "://" + authority]
    if scheme in ("socks5", "socks5h"):
        return ["http://" + authority, "https://" + authority]
    return []


def _failure(category, message, *, reachable=False, http_status=None):
    return {"ok": False, "status": "reachable" if reachable else "failed",
            "reachable": reachable, "category": category, "error": message,
            "hint": "", "http_status": http_status, "ip": ""}


def _exception(exc):
    # The raw transport exception may contain proxy credentials and addresses.
    # It is used only for classification, never included in the result.
    text = str(exc).lower()
    if isinstance(exc, ValueError):
        return _failure("policy", "检测配置或出站安全检查未通过，请检查服务端配置。")
    if isinstance(exc, socket.gaierror) or any(s in text for s in (
            "could not resolve", "name or service not known", "name resolution", "curl: (5)", "curl: (6)")):
        return _failure("dns", "服务端域名解析失败，尚不能判断代理是否可用。")
    if "407" in text or "authentication" in text or "auth failed" in text:
        return _failure("proxy_auth", "代理认证未通过，请检查认证信息或代理商白名单。")
    if "wrong version number" in text:
        return _failure("protocol", "代理协议握手不匹配，正在核对其他协议。")
    if any(s in text for s in ("ssl", "tls", "certificate", "curl: (35)", "curl: (60)")):
        return _failure("tls", "安全连接验证未通过，请检查证书或代理链路。")
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text or "curl: (28)" in text:
        return _failure("timeout", "本次检测超时，请稍后重试；尚不能判断代理是否可用。")
    if any(s in text for s in ("handshake", "unsupported protocol", "socks", "empty reply", "curl: (52)", "curl: (97)")):
        return _failure("protocol", "代理隧道或协议握手未建立，请核对协议；尚不能判断目标服务可用性。")
    return _failure("connect", "代理隧道未建立，尚不能判断目标服务可用性。")


def _probe(proxy, target, timeout):
    session = None
    try:
        session = create_http_session(proxy=proxy)
        # request() is passed through by the TLS wrapper, without its get()
        # retries. Disable requests adapter retries in the fallback as well.
        for adapter in (getattr(session, "adapters", {}) or {}).values():
            adapter.max_retries = 0
        response = session.request("GET", target, timeout=timeout, allow_redirects=False)
        status = response.status_code
        if status == 407:
            return _failure("proxy_auth", "代理认证未通过，请检查认证信息或代理商白名单。", http_status=status)
        if status != 200:
            return _failure("target_http", f"已连接检测目标，但目标返回 HTTP {status}；代理可达，检测尚未通过。",
                            reachable=True, http_status=status)
        ip = ""
        if urlsplit(target).hostname == "api.ipify.org":
            try:
                data = response.json()
                ip = str(ipaddress.ip_address(data["ip"])) if isinstance(data, dict) else ""
                if not ip:
                    raise ValueError("missing IP")
            except (ValueError, TypeError, KeyError):
                return _failure("invalid_response", "已连接检测目标，但未收到有效检测结果，请稍后重试。",
                                reachable=True, http_status=status)
        return {"ok": True, "status": "success", "reachable": True, "category": "none",
                "error": "", "hint": "", "http_status": status, "ip": ip}
    except Exception as exc:
        return _exception(exc)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def test_proxy(proxy, *, target=DEFAULT_TARGET, timeout=12):
    start = time.monotonic()
    budget = max(1, min(float(timeout), 60))
    deadline = start + budget
    normalized = normalize_proxy(proxy) or ""
    attempts = 0
    tried = []
    try:
        alternate = _alternate(normalized)
    except (ValueError, AttributeError):
        alternate = []
    candidates = [normalized] + alternate
    result = _failure("timeout", "本次检测超时，请稍后重试；尚不能判断代理是否可用。")
    original_result = None
    for candidate in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Leave part of the same budget for an alternate protocol if needed.
        attempt_timeout = min(remaining, budget * .5) if attempts == 0 and alternate else remaining / (len(candidates) - attempts)
        tried.append(candidate)
        attempts += 1
        current = _probe(candidate, target, attempt_timeout)
        result = current
        if original_result is None:
            original_result = dict(current)
        if current["ok"]:
            if candidate != normalized:
                result["alt_scheme"] = candidate
                result["hint"] = "备用协议检测通过，请先确认并改用该协议再运行任务。"
            break
        if current["category"] not in ("connect", "protocol", "timeout"):
            break
    if not result["ok"] and not result["reachable"] and len(tried) > 1:
        result = original_result
        result["hint"] = "已核对 " + "、".join(urlsplit(item).scheme.upper() for item in tried) + "，本次均未完成检测；未修改代理池状态。"
    result.update(attempts=attempts, latency_ms=int((time.monotonic() - start) * 1000))
    if len(tried) > 1:
        result["tried"] = tried
    return result


def test_proxies(proxies, *, target=DEFAULT_TARGET, timeout=12):
    with ThreadPoolExecutor(max_workers=min(20, len(proxies))) as pool:
        results = pool.map(lambda proxy: test_proxy(proxy, target=target, timeout=timeout), proxies)
        return {"ok": True, "results": dict(zip(proxies, results))}

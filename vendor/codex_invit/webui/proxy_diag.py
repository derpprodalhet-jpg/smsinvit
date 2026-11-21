"""代理路径诊断：直连 / 本机系统代理 / 池内代理 各打一次，比出口 IP。

为什么要它：代理池测试失败时，光看一句 "CONNECT aborted" 分不清
「代理本身坏了」还是「本机（Clash/TUN/防火墙）把去代理的那一跳改道了」。
把三条路径放在一起比，结论就唯一了：

  · 直连/系统代理通、池内代理不通 → 问题在池内那一跳（账号/白名单/端口）
  · 三条都不通                      → 问题在本机出网
  · 池内代理通                      → 没问题

⚠️ 必须在**你自己的终端**里跑，结果才代表你的网络：
    .venv/bin/python tests/diagnose_proxy.py
（管理后台「代理池」页的按钮走的是服务进程的网络，如果服务是别人起的，
  结果代表的是那个进程的出口。）
"""
from __future__ import annotations

import re
import time
from typing import Optional
from urllib.request import getproxies

from . import db

TARGET = "https://api.ipify.org?format=json"


def mask(url: str) -> str:
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", str(url or ""))


_HINTS = (
    ("proxy connect aborted", "代理拒绝了 CONNECT：账号过期 / 出口 IP 未加白名单 / 端口失效"),
    ("connection to proxy closed", "代理接了 TCP 立刻断开：认证失败，或要求先把本机 IP 加白名单"),
    ("empty reply from server", "代理返回空响应：服务异常，或该端口不是 HTTP 代理（试 socks5h）"),
    ("proxy handshake", "代理握手失败：协议不对（http ↔ socks5 互换再试）"),
    ("could not resolve proxy", "代理域名解析失败：地址写错"),
    ("connection refused", "代理端口拒绝连接：端口关了或地址写错"),
    ("failed to connect", "连不上代理端口：网络不通 / 被防火墙拦"),
    ("timed out", "连接超时：网络不通，或被墙（本机可能需要走前置代理）"),
    ("503", "隧道被拒（HTTP 503）：代理或本机 Clash 规则拒绝这条隧道"),
)


def hint_of(err: str) -> str:
    low = str(err or "").lower()
    for key, h in _HINTS:
        if key in low:
            return h
    return ""


def tcp_hijack_check(host: str = "1.1.1.1", ports=(1, 9, 54321), timeout: float = 5.0) -> dict:
    """本机是否在本地接管了 TCP（Clash TUN / 透明代理）。

    做法：连一个**不可能开放**的端口。正常网络会 refused/timeout；
    若这里也能"连上"，说明连接根本没出去，是被本机代理在本地接下的 ——
    那么任何"代理连不上"的结论都不可信。

    这个坑真出现过：池内代理两个端口都报 CONNECT aborted，
    结果连 `43.165.126.74:1` 都能"连上"，全是本机 TUN 造成的假象。
    """
    import socket

    open_ports: list = []
    for port in ports:
        try:
            sk = socket.create_connection((host, port), timeout=timeout)
            sk.close()
            open_ports.append(port)
        except Exception:  # noqa: BLE001
            pass
    hijacked = bool(open_ports)
    return {
        "hijacked": hijacked,
        "open_ports": open_ports,
        "probed": list(ports),
        "host": host,
        "hint": (f"本机在本地接管了 TCP（连 {host}:{open_ports} 这种不可能开放的端口都能连上）——"
                 f"说明有 Clash TUN / 透明代理在中间，**池内代理的连通性结论不可信**。"
                 f"请先关掉 Clash 的 TUN/系统代理，或给代理商的 IP 加一条 DIRECT 规则，再测。"
                 if hijacked else ""),
    }


def probe(label: str, proxy: Optional[str], target: str = TARGET, timeout: int = 12) -> dict:
    out = {"label": label, "proxy": mask(proxy) if proxy else "（直连）", "ok": False,
           "latency_ms": None, "ip": "", "error": "", "hint": ""}
    try:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from http_client import create_http_session
    except Exception as e:  # noqa: BLE001
        out["error"] = f"加载 http_client 失败: {e}"
        return out
    t0 = time.perf_counter()
    try:
        sess = create_http_session(proxy=proxy)
        resp = sess.get(target, timeout=timeout)
        out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
            out["hint"] = "通了但目标返回非 200（可能是目标站点限制）"
            return out
        out["ok"] = True
        try:
            out["ip"] = (resp.json() or {}).get("ip", "")
        except Exception:  # noqa: BLE001
            out["ip"] = (resp.text or "").strip()[:64]
    except Exception as e:  # noqa: BLE001
        out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        out["error"] = str(e)[:200]
        out["hint"] = hint_of(out["error"])
    return out


def diagnose(target: str = TARGET, timeout: int = 12, max_pool: int = 5) -> dict:
    checks: list[dict] = []

    # ⓪ 先确认本机有没有在本地接管 TCP —— 有的话后面所有代理结论都不可信
    pool_rows_pre = db.proxy_list_with_state()
    hijack_host = "1.1.1.1"
    if pool_rows_pre:
        try:
            from urllib.parse import urlparse
            host = urlparse(pool_rows_pre[0]["url"]).hostname or ""
            # 用池内代理所在主机探测更贴切（同一台机器的"不可能端口"）
            if host:
                hijack_host = host
        except Exception:  # noqa: BLE001
            pass
    hj = tcp_hijack_check(host=hijack_host)
    checks.append({
        "label": "⓪ 本机是否接管了 TCP",
        "proxy": f"探测 {hj['host']}:{hj['probed']}（不可能开放的端口）",
        "ok": (not hj["hijacked"]),
        "latency_ms": None,
        "ip": "",
        "error": "" if not hj["hijacked"] else f"这些端口也'连上'了: {hj['open_ports']}",
        "hint": hj["hint"] if hj["hijacked"] else "正常：不可能开放的端口连不上，说明连接真的出去了",
    })

    # ① 直连
    checks.append(probe("① 直连（不走任何代理）", None, target, timeout))

    # ② 本机系统代理（macOS 的系统代理只有 urllib 读得到）
    try:
        proxies = getproxies() or {}
    except Exception:  # noqa: BLE001
        proxies = {}
    sys_proxy = proxies.get("https") or proxies.get("http") or ""
    if sys_proxy:
        checks.append(probe(f"② 本机系统代理 {mask(sys_proxy)}", sys_proxy, target, timeout))
    else:
        checks.append({"label": "② 本机系统代理", "proxy": "（未设置）", "ok": None,
                       "latency_ms": None, "ip": "", "error": "", "hint": "系统里没配代理"})

    # ③ 池内代理
    pool_rows = db.proxy_list_with_state()[:max_pool]
    if not pool_rows:
        checks.append({"label": "③ 池内代理", "proxy": "（池子为空）", "ok": None,
                       "latency_ms": None, "ip": "", "error": "", "hint": "先去导代理"})
    for i, row in enumerate(pool_rows, 1):
        checks.append(probe(f"③ 池内代理 #{row['id']}（{row['status']}）",
                            row["url"], target, timeout))

    pool_checks = [c for c in checks if c["label"].startswith("③") and c["ok"] is not None]
    base_checks = [c for c in checks if c["label"].startswith(("①", "②")) and c["ok"] is not None]
    pool_ok = any(c["ok"] for c in pool_checks)
    base_ok = any(c["ok"] for c in base_checks)

    if pool_ok:
        # ⚠️ 顺序很重要：代理通了就以「通」为准。
        #    本机若开着 Clash TUN / 透明代理，会出现「连上即断」的假象
        #    （连不可能开放的端口都能"连上"），别把这种瞬时抖动当成代理商的问题。
        verdict = "池内代理可用 ✅"
        if hj["hijacked"]:
            verdict += ("（注意：本机有透明代理/TUN 接管 TCP，偶发「连上即断」的假象 —— "
                        "之前几轮全失败就是这种抖动，不是代理商的问题。若再遇到，重测一次即可。）")
    elif hj["hijacked"]:
        verdict = ("本机在本地接管了 TCP（不可能开放的端口也能'连上'），这一轮池内代理全部失败 —— "
                   "可能是瞬时抖动，也可能是去代理那一跳被改道。先**重测一次**；"
                   "仍失败就把 Clash 的 TUN 关掉，或给代理商 IP 加 "
                   "`IP-CIDR,<代理商IP>/32,DIRECT`（放在 MATCH 规则之前）。")
    elif base_ok:
        verdict = ("本机出网正常（直连/系统代理能连通），但本轮池内代理全部失败。"
                   "这类代理常有抖动，先**重测一次**；若持续失败，再查代理商的白名单/余额/端口。")
    else:
        verdict = ("本机出网本身就不通（直连和系统代理都失败）—— 先解决本机网络/Clash 再谈池内代理。")

    return {"ok": True, "target": target, "checks": checks,
            "pool_ok": pool_ok, "base_ok": base_ok, "verdict": verdict}

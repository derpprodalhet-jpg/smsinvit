#!/usr/bin/env python
"""端到端 API 验收：先按 README 起好两个进程，再跑这个脚本。

    .venv/bin/python start_admin.py &     # 5093
    .venv/bin/python start_user.py  &     # 5094
    .venv/bin/python tests/e2e_api.py

它只做「接口层」的验收：
  · 两个端口的隔离（用户端口不该有管理接口，反之亦然）
  · 用户侧响应不泄漏代理/凭证
  · 兑换 / 邮箱不足整批拒绝 / 重复兑换拒绝
  · 未确认邀请信不能开始；入队 / 重复点开始 / 脱敏阶段
  · 管理端使用记录、代理池 API、接码价格、邀请设置、依赖自检、概览、卡密导出

真实注册与 Codex 授权需要真代理/真邮箱/真邀请信，不在本脚本范围内。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ADMIN = "http://127.0.0.1:5093"
USER = "http://127.0.0.1:5094"

_ok = _fail = 0


def check(cond, label, extra=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ✓ {label}")
    else:
        _fail += 1
        print(f"  ✗ {label} {extra}")


def call(method, url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body)
        except Exception:  # noqa: BLE001
            pass
        return e.code, body


def main() -> int:
    print("=== 0. 健康检查 ===")
    st, _ = call("GET", ADMIN + "/api/health")
    check(st == 200, "管理后台 /api/health")
    st, _ = call("GET", USER + "/api/u/health")
    check(st == 200, "用户前台 /api/u/health")
    if _fail:
        print("\n两个进程没起好，先跑 start_admin.py / start_user.py")
        return 1

    print("=== 1. 端口隔离 ===")
    for p in ("/api/registered", "/api/settings/sms", "/api/cdk/list", "/api/proxy/pool", "/api/overview"):
        st, _ = call("GET", USER + p)
        check(st == 404, f"用户端口 404: {p}", st)
    for p in ("/api/u/meta", "/api/u/status?codes=X", "/api/u/redeem"):
        st, _ = call("GET", ADMIN + p)
        check(st in (404, 405), f"管理端口 404: {p}", st)

    print("=== 2. 播种 ===")
    stamp = str(int(time.time()))[-6:]
    mail = f"e2e{stamp}@outlook.com"
    st, r = call("POST", ADMIN + "/api/import", {
        "kind": "outlook",
        "text": f"{mail}----Pw1!----cid-e2e----refresh-token-abcdefghijklmnop",
    })
    check(st == 200 and r.get("inserted") == 1, "导入邮箱")
    st, r = call("POST", ADMIN + "/api/proxy/pool/add",
                 {"text": "http://9.9.9.9:8080\nsocks5://8.8.8.8:1080"})
    check(st == 200 and r.get("added") >= 1, "导入代理")
    st, r = call("POST", ADMIN + "/api/cdk/generate",
                 {"count": 3, "batch": f"E2E{stamp}", "prefix": "CDK-", "length": 10})
    codes = r.get("codes") or []
    check(st == 200 and len(codes) == 3, "生成 3 张卡密")

    print("=== 3. 兑换 ===")
    st, meta = call("GET", USER + "/api/u/meta")
    check("pool_available" in meta and "ready" in meta, f"meta: {meta}")
    check("proxy" not in json.dumps(meta).lower(), "meta 不含任何代理信息")
    if meta.get("pool_available") == 0:
        print("  ! 号池已空，跳过兑换相关断言")
        return _report()

    available = meta.get("pool_available")
    if isinstance(available, int) and available >= 0 and available < 3:
        st, r = call("POST", USER + "/api/u/redeem", {"codes": codes})
        check(st == 400, "邮箱不足 → 整批拒绝", r)

    st, r = call("POST", USER + "/api/u/redeem", {"codes": [codes[0]]})
    check(st == 200 and r["items"], "兑换 1 张成功")
    blob = json.dumps(r)
    check(not any(k in blob.lower() for k in ("password", "totp", "token", "refresh", "cookie")),
          "兑换响应无凭证字段")
    st, r = call("POST", USER + "/api/u/redeem", {"codes": [codes[0]]})
    check(r.get("ok") is False, "重复兑换被拒")

    print("=== 4. 开始执行 ===")
    st, r = call("POST", USER + "/api/u/start", {"code": codes[0]})
    check(st == 400, "未确认邀请信 → 拒绝")
    st, r = call("POST", USER + "/api/u/invite_sent", {"code": codes[0]})
    check(st == 200, "确认已发送邀请信")
    st, r = call("POST", USER + "/api/u/start", {"code": codes[0]})
    if r.get("reason") == "busy":
        check(True, "无可用代理 → 返回 busy（不启动、不算失败）")
    else:
        check(r.get("ok") is True, "入队成功")
        st, r2 = call("POST", USER + "/api/u/start", {"code": codes[0]})
        check(r2.get("already") is True, "重复点开始 → 返回现有作业")

    print("=== 5. 用户侧状态脱敏 ===")
    allowed = {"正在排队", "正在等待邀请信", "正在接受邀请", "正在发送消息",
               "正在生成凭证", "完成", "失败", "待开始", "未兑换", "未知卡密", "已作废"}
    labels, statuses = set(), set()
    for _ in range(6):
        st, r = call("GET", USER + f"/api/u/status?codes={codes[0]}")
        item = r["items"][0]
        statuses.add(item["status"])
        labels.add(item["stage_label"])
        low = json.dumps(r).lower()
        leaked = [k for k in ("proxy", "password", "totp", "token", "access", "refresh", "cookie")
                  if k in low]
        if leaked:
            check(False, "用户状态泄漏", leaked)
            break
        if item["status"] in ("running", "failed", "done"):
            break
        time.sleep(1.5)
    check(labels <= allowed, f"阶段文案全部脱敏: {labels}")

    print("=== 6. 管理端使用记录 ===")
    st, r = call("GET", ADMIN + f"/api/cdk/records?code={codes[0]}")
    check(st == 200 and len(r.get("jobs", [])) >= 1, "作业历史存在")
    check(len(r.get("events", [])) >= 2, "事件流水存在")
    # 代理是「选中代理之后」才写进作业行的；等邀请信阶段本来就没有代理
    if r["jobs"] and r["jobs"][0].get("stage") not in ("queued", "waiting_invite"):
        check(bool(r["jobs"][0].get("proxy")), "过了邀请信阶段后作业行记下了代理")
    else:
        check(r["jobs"][0].get("proxy") in (None, ""),
              "等邀请信阶段不占用代理（符合设计）")

    print("=== 7. 代理池 API ===")
    st, r = call("GET", ADMIN + "/api/proxy/pool")
    check(st == 200 and r.get("items"), "代理池列表")
    st, r = call("POST", ADMIN + "/api/proxy/pool/reset", {"id": r["items"][0]["id"]})
    check(st == 200, "重置代理")
    st, r = call("GET", ADMIN + "/api/proxy/pool/events?limit=50")
    check(st == 200 and r.get("items"), "代理事件流水")

    print("=== 8. 接码价格 / 邀请设置 ===")
    st, r = call("GET", ADMIN + "/api/sms/prices")
    check(st == 200 and r.get("poll_seconds") == 5, "价格任务 5s")
    check(set(r.get("platforms", {}).keys()) == {"smsbower", "herosms", "smspool"}, "三个平台都在状态里")
    check("effective" in r and "best" in r, "快照含最优与生效参数")
    st, r = call("POST", ADMIN + "/api/sms/selftest", {}, timeout=90)
    check(st == 200 and "platforms" in r, "接码接口自检")
    st, r = call("GET", ADMIN + "/api/settings/invite")
    check(r["config"]["invite_wait_timeout"] == "150" and r["config"]["cdk_max_concurrency"] == "3",
          "邀请设置默认值")
    st, r = call("GET", ADMIN + "/api/settings/runtime_check", timeout=60)
    check(st == 200 and "codex" in r, f"依赖自检 node={bool(r.get('node'))} codex={bool(r.get('codex'))}")

    print("=== 9. 概览 / 卡密导出 ===")
    st, r = call("GET", ADMIN + "/api/overview")
    check(st == 200 and all(k in r for k in ("stats", "cdk", "proxies", "sms")), "overview 字段齐全")
    st, r = call("GET", ADMIN + "/api/cdk/export/formats")
    check(st == 200 and len(r["formats"]) >= 4, "卡密导出格式")
    st, r = call("POST", ADMIN + "/api/cdk/export", {"format": "codes_txt", "search": "CDK-"})
    check(st == 200 and r["count"] >= 1 and codes[0] in r["text"], "导出卡密 txt")

    return _report()


def _report() -> int:
    print(f"\n通过 {_ok}，失败 {_fail}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""在你自己的终端里跑，判断代理失败到底出在哪一跳。

    cd /srv/workspace/codex_invit
    .venv/bin/python tests/diagnose_proxy.py

它会用**同一套 HTTP 代码**依次走：
    ① 直连           ② 本机系统代理        ③ 池内每个代理
并打出每次的出口 IP。结论看最后那行 verdict。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webui import proxy_diag  # noqa: E402


def main() -> int:
    print("目标:", proxy_diag.TARGET)
    print("-" * 96)
    r = proxy_diag.diagnose()
    for c in r["checks"]:
        mark = "✅" if c["ok"] else ("－" if c["ok"] is None else "❌")
        lat = f"{c['latency_ms']}ms" if c.get("latency_ms") is not None else "-"
        print(f"{mark} {c['label']:<34} {c['proxy']}")
        print(f"      出口IP={c.get('ip') or '-':<22} 耗时={lat:<7} {c.get('error') or ''}")
        if c.get("hint"):
            print(f"      → {c['hint']}")
    print("-" * 96)
    print("结论:", r["verdict"])
    return 0 if r["pool_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

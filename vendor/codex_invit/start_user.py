#!/usr/bin/env python3
"""codex_invit 用户前台一键启动（默认 127.0.0.1:5094）。

**和管理后台分开跑、分开端口**：这个进程只暴露用户接口（兑换 / 我已发送邀请信 /
开始执行 / 进度），并且是**唯一**跑 CDK worker 的进程，所以「同时只跑 3 个」
在全局成立。

用法：
    python start_user.py
    python start_user.py --host 0.0.0.0 --port 5094     # 要给外部用户访问时
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if sys.platform.startswith("win"):
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认 127.0.0.1；给外部用户访问才写 0.0.0.0）")
    ap.add_argument("--port", type=int, default=5094, help="监听端口（默认 5094）")
    ap.add_argument("--reload", action="store_true", help="开发模式（改动自动重启）")
    args = ap.parse_args()

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("[!] 缺少依赖，正在安装 fastapi / uvicorn ...")
        import subprocess

        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "fastapi", "uvicorn[standard]", "pydantic>=2",
        ])

    sys.path.insert(0, str(ROOT))
    import uvicorn

    shown = args.host if args.host != "0.0.0.0" else "127.0.0.1"
    print("\n🔔 codex_invit 用户前台启动中...")
    print(f"   访问: http://{shown}:{args.port}/")
    if args.host == "0.0.0.0":
        print("   ⚠️ 已监听 0.0.0.0：外部用户可直接访问兑换页")
    print()
    uvicorn.run("webui.user_app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()

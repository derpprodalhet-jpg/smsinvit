#!/usr/bin/env python
"""跑全部单元测试：`.venv/bin/python tests/run_all.py`

不依赖 pytest（requirements 里没有它），失败以退出码体现。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import (  # noqa: E402
    test_codex_flow,
    test_db_migration,
    test_mail_robust,
    test_invite_and_cli,
    test_proxy_pool,
    test_redeem_queue,
    test_sms,
    test_smspool,
    test_sms_otp_path,
)

MODULES = [
    test_proxy_pool,
    test_mail_robust,
    test_db_migration,
    test_sms,
    test_smspool,
    test_invite_and_cli,
    test_codex_flow,
    test_redeem_queue,
    test_sms_otp_path,
]


def main() -> int:
    total_passed = 0
    all_failed: list[tuple[str, list[str]]] = []
    for mod in MODULES:
        name = mod.__name__.split(".")[-1]
        try:
            chk = mod.run()
        except Exception:  # noqa: BLE001
            print(f"\n!!! {name} 崩了")
            traceback.print_exc()
            all_failed.append((name, ["模块异常"]))
            continue
        total_passed += chk.passed
        if chk.failed:
            all_failed.append((name, chk.failed))

    print("\n" + "=" * 60)
    print(f"通过 {total_passed} 项")
    if all_failed:
        print("失败：")
        for name, items in all_failed:
            for it in items:
                print(f"  ✗ [{name}] {it}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())

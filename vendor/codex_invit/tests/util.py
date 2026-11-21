"""测试公共设施：临时库 + 打桩工具。

不依赖 pytest —— 用虚拟环境的 python 直接跑 `tests/run_all.py` 即可。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = None


def fresh_db() -> Path:
    """把库指到一个全新的临时文件，然后重新初始化 schema。"""
    global _TMP
    if _TMP is None:
        _TMP = Path(tempfile.mkdtemp(prefix="codex_invit_test_"))
    db_file = _TMP / "test.db"
    for suffix in ("", "-shm", "-wal"):
        p = Path(str(db_file) + suffix)
        if p.exists():
            p.unlink()
    os.environ["CODEX_INVIT_DB"] = str(db_file)

    # 让 webui.db 用新路径重新加载
    for mod in [m for m in list(sys.modules) if m.startswith("webui")]:
        del sys.modules[mod]
    from webui import db  # noqa: PLC0415
    db.DB_PATH = db_file
    db.init_db()
    return db_file


def cleanup() -> None:
    global _TMP
    if _TMP and _TMP.exists():
        shutil.rmtree(_TMP, ignore_errors=True)
    _TMP = None


class Check:
    """极简断言计数器（避免引入 pytest 依赖）。"""

    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed: list[str] = []

    def __enter__(self):
        print(f"\n=== {self.name} ===")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.failed.append(f"异常: {exc_type.__name__}: {exc}")
            print(f"  ✗ 异常 {exc_type.__name__}: {exc}")
            return True
        print(f"  → {self.passed} 项通过，{len(self.failed)} 项失败")
        return False

    def ok(self, cond, label: str):
        if cond:
            self.passed += 1
        else:
            self.failed.append(label)
            print(f"  ✗ {label}")
        return bool(cond)

    def eq(self, got, want, label: str):
        return self.ok(got == want, f"{label}（got={got!r} want={want!r}）")


def seed_proxy(url: str = "http://10.0.0.1:8080") -> None:
    from webui import db  # noqa: PLC0415
    db.proxy_add_many([url])


def seed_mailbox(email: str = "invite1@outlook.com", kind: str = "outlook") -> None:
    from webui import db  # noqa: PLC0415
    db.import_accounts(
        f"{email}----Pass1!----client-1----refresh-token-abcdefghijklmnop",
        kind=kind,
    )


def make_cdk(code: str = "CDK-TEST-0001") -> None:
    from webui import db  # noqa: PLC0415
    db.create_cdk_codes([code], batch="T")

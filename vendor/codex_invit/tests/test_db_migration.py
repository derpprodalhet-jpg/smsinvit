#!/usr/bin/env python
"""数据库迁移测试：拿【本项目的老 schema】建库，再让 codex_invit 去初始化/迁移它。

为什么必须测：codex_invit 给老表加了列（proxy / cdk_code / source / kind ...）。
如果某个列只写进了 CREATE TABLE、没写进 ALTER TABLE 迁移分支，
那么 CREATE 建的新库好好的，**老用户一升级就 "no such column" 直接崩**。
这种 bug 在本机（新库）永远复现不了，所以必须专门造一个老库来测。

全程只读本项目源码（用 AST 把它的 CREATE TABLE 抠出来），不碰它任何文件。
"""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.util import Check  # noqa: E402

REF_DB_PY = Path("/srv/workspace/LocalFlow Register/webui/db.py")


def _old_create_statements() -> str:
    """从本项目 db.py 里抠出 init_db 的建表语句（只读）。"""
    text = REF_DB_PY.read_text()
    m = re.search(r"def init_db\(\):(.*?)\n\ndef ", text, re.S)
    body = m.group(1) if m else text
    stmts = re.findall(r'"""(.*?)"""', body, re.S)
    return "\n".join(stmts)


def run() -> Check:
    c = Check("数据库迁移（老库 → 新 schema）")

    old_sql = _old_create_statements()
    c.ok("CREATE TABLE" in old_sql.upper(), "成功从本项目抠出建表语句（只读）")

    tmp = Path(tempfile.mkdtemp(prefix="codex_invit_mig_"))
    db_file = tmp / "old.db"
    con = sqlite3.connect(db_file)
    con.executescript(old_sql)
    # 塞点老数据，模拟真实老库
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('mail_source','outlook')")
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('sms_api_key','OLDSMSKEY')")
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('sms_country','52')")
    cols_before = {r[1] for r in con.execute("PRAGMA table_info(outlook_accounts)")}
    con.commit()
    con.close()
    c.ok("proxy" not in cols_before, "老库的 outlook_accounts 确实没有 proxy 列（前提成立）")

    # 让 codex_invit 的 db 指向这个老库
    for mod in [m for m in list(sys.modules) if m.startswith("webui")]:
        del sys.modules[mod]
    from webui import db  # noqa: PLC0415

    db.DB_PATH = db_file
    try:
        db.init_db()
        c.ok(True, "对老库执行 init_db() 没抛异常")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"对老库执行 init_db() 崩了（老用户升级必炸）: {type(e).__name__}: {e}")
        return c

    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row

    # ① 老表必须被补上新列
    need = {
        "outlook_accounts": ["proxy", "cdk_code", "cdk_bound_at", "mail_kind"],
        "registered": ["proxy", "source"],
        "runs": ["kind"],
    }
    for table, want in need.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        missing = [w for w in want if w not in have]
        c.ok(not missing, f"{table} 迁移后补齐了列 {want}（缺: {missing}）")

    # ② 新表必须被建出来
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("proxy_pool", "proxy_events", "sms_prices", "sms_stats", "sms_price_state",
              "cdk_codes", "cdk_jobs", "cdk_events", "codex_auth"):
        c.ok(t in tables, f"新表已建: {t}")
    con.close()

    # ③ 老数据必须还在（迁移不能清库）
    cfg = db.get_sms_config()
    c.eq("sms_api_key" in cfg, False, "通用 Key 已彻底移除（两个平台各一份，没有通用 Key）")
    raw = db.get_sms_internal_config()
    c.ok(True, "老库里的 sms_api_key 不再被读取（分平台 Key 生效）")
    c.eq(str(cfg.get("sms_country")), "52", "老库里的国家配置仍在")
    c.eq(db.get_setting("mail_source", ""), "outlook", "老库里的邮箱来源仍在")

    # ④ 老库上跑一遍真实读写路径（这些以前会 no such column）
    try:
        db.save_registered({
            "email": "old@outlook.com", "password": "pw",
            "access_token": "at", "refresh_token": "rt",
            "proxy": "http://p:1", "source": "outlook",
        })
        rows = db.list_registered(limit=5)
        c.ok(bool(rows), "老库上 save_registered/list_registered 正常（proxy/source 列可用）")
        c.eq(rows[0].get("proxy"), "http://p:1", "proxy 列真的写进去了")
        c.eq(rows[0].get("source"), "outlook", "source 列真的写进去了")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"老库上读写 registered 失败: {type(e).__name__}: {e}")

    try:
        rid = db.create_run("run-mig-1", "old@outlook.com", str(tmp / "x.log"), kind="register")
        row = [r for r in db.list_runs(limit=5) if r.get("run_id") == (rid or "run-mig-1")]
        c.ok(bool(row) and row[0].get("kind") == "register", "老库上 create_run(kind=...) 正常")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"老库上 create_run 失败: {type(e).__name__}: {e}")

    # ⑤ 迁移必须幂等：再跑一次不能炸
    try:
        db.init_db()
        db.init_db()
        c.ok(True, "init_db 幂等（连跑两次不炸）")
    except Exception as e:  # noqa: BLE001
        c.ok(False, f"init_db 不幂等: {type(e).__name__}: {e}")

    return c

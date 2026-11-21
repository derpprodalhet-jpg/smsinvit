"""SQLite 号池 + 注册结果存储。

表结构：
  outlook_accounts: 接码号池（多种邮箱混放，kind 列区分 + 状态机）
  registered:       注册成功结果（凭证 JSON）

关于 outlook_accounts 这个表名：
    它现在装的不止 outlook（还有 gmail / icloud / qq ...），名字已经不准，
    但改表名要动迁移和一堆 SQL，收益只是好看一点，风险不值。
    真正区分类型的是 kind 列。

凭证字段用「并集列」而不是 extra_json：
    outlook/gmail 用 password+client_id+refresh_token，
    icloud 这类中转只用 relay_url，各自把不用的列留空。
    几种邮箱的规模下，并集列比 JSON 好 —— 能建索引、能加约束、
    SQL 里直接看得见。加新邮箱时如果要新字段，就再 ALTER 加一列。
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# CODEX_INVIT_DB 允许把库指到别处（测试用临时库；生产留空即 webui/webui.db）
DB_PATH = Path(
    os.environ.get("CODEX_INVIT_DB") or (Path(__file__).resolve().parent / "webui.db")
)

_lock = threading.RLock()  # SQLite 写入串行化


def _conn():
    from smsinvit.invitation.mysql import connection
    return connection()



def init_db():
    """Schema installation is explicit through the migration CLI."""
    return None


def _extra_dict(value) -> dict:
    """Malformed or absent legacy metadata must not hide the account row."""
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _phone_details(row: dict, extra: dict) -> dict:
    for field in ("used_proxy", "phone_bound_ok"):
        value = row.get(field)
        if value not in (0, 1):
            value = extra.get(field, -1)
        row[field] = int(value) if value in (0, 1) else -1
    row["phone_bound_at"] = row.get("phone_bound_at") or extra.get("phone_bound_at")
    if row["used_proxy"] == 0:
        row["proxy"] = ""
    for field in ("phone_number", "phone_code"):
        row[field] = str(extra.get(field) or "").strip()
    return row


_REGISTERED_POOL_COLUMNS = (
    "oa.proxy AS _account_proxy, oa.used_proxy AS _account_used_proxy, "
    "oa.phone_bound_ok AS _account_phone_bound_ok, "
    "oa.phone_bound_at AS _account_phone_bound_at"
)
_REGISTERED_POOL_JOIN = (
    " LEFT JOIN outlook_accounts oa ON lower(oa.email)=lower(r.email) "
)


def _registered_details(row: dict, *, full: bool = False) -> dict:
    """Project the same current registration evidence in both admin lists."""
    extra = _extra_dict(row.pop("extra_json", None))
    account_proxy = row.pop("_account_proxy", None)
    account_used_proxy = row.pop("_account_used_proxy", None)
    account_phone_ok = row.pop("_account_phone_bound_ok", None)
    account_phone_at = row.pop("_account_phone_bound_at", None)
    row["proxy"] = account_proxy or row.get("proxy") or ""
    # Known pool state is authoritative; older standalone registrations may
    # only have their metadata snapshot in registered.extra_json.
    row["used_proxy"] = (account_used_proxy if account_used_proxy in (0, 1)
                         else extra.get("used_proxy", -1))
    row["phone_bound_ok"] = (account_phone_ok if account_phone_ok in (0, 1)
                            else extra.get("phone_bound_ok", -1))
    row["phone_bound_at"] = account_phone_at or extra.get("phone_bound_at")
    _phone_details(row, extra)
    if full:
        row["extra"] = extra
    else:
        row["plus_check"] = extra.get("plus_check")
    return row



# ──────────────────────── outlook 号池 ────────────────────────


def parse_lines(text: str, kind: str = "") -> list[dict]:
    """解析导入文本，委托给 mail_providers 注册表。

    kind 指定 → 用该 provider 的格式解析（推荐）
    kind 为空 → 按段数猜（段数唯一时才行，Outlook/Gmail 都是 4 段会猜不出）

    非法行抛 ImportValidationError（带行号和原因），**不再静默跳过**。
    以前这里是 `if len(parts) != 4: continue`，用户看到"导入成功"
    但号少了几个，完全没法排查。
    """
    from mail_providers import parse_import_text

    return parse_import_text(text or "", kind)


def import_accounts(text: str, kind: str = "") -> dict:
    """批量入库。已存在的 email 仅在凭证变化时更新。

    解析阶段全对才写：有一行非法就整批拒绝（抛 ImportValidationError），
    不会出现"写进去一半"对不上账的情况。
    """
    rows = parse_lines(text, kind)
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        for r in rows:
            row_kind = r.get("kind") or kind or "outlook"
            # 凭证并集：不同 provider 用不同子集，没有的留空字符串
            password = r.get("password", "") or ""
            client_id = r.get("client_id", "") or ""
            refresh = r.get("refresh_token", "") or ""
            relay = r.get("relay_url", "") or ""

            cur = con.execute(
                "SELECT refresh_token, relay_url, kind FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "relay_url, kind, status, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'available', ?)",
                    (r["email"], password, client_id, refresh, relay, row_kind, now),
                )
                inserted += 1
            elif (
                (existing["refresh_token"] or "") != refresh
                or (existing["relay_url"] or "") != relay
                or (existing["kind"] or "") != row_kind
            ):
                # 凭证或类型变了 → 覆盖并重置为可用
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "relay_url=?, kind=?, status='available', imported_at=?, fail_reason=NULL "
                    "WHERE email=?",
                    (refresh, password, client_id, relay, row_kind, now, r["email"]),
                )
                updated += 1
            else:
                skipped += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def count_accounts(status: str = "", kind: str = "") -> int:
    con = _conn()
    sql = "SELECT COUNT(*) FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    return con.execute(sql, args).fetchone()[0]


def list_accounts(
    status: str = "", limit: int = 50, offset: int = 0, kind: str = ""
) -> list[dict]:
    con = _conn()
    sql = ("SELECT oa.*, r.extra_json AS _registered_extra_json, r.proxy AS _registered_proxy "
           "FROM outlook_accounts oa LEFT JOIN registered r ON lower(r.email)=lower(oa.email)")
    where, args = [], []
    if status:
        where.append("oa.status=?")
        args.append(status)
    if kind:
        where.append("oa.kind=?")
        args.append(kind.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY oa.imported_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    rows = []
    for row in con.execute(sql, args).fetchall():
        item = dict(row)
        item["proxy"] = item.get("proxy") or item.pop("_registered_proxy", None) or ""
        item.pop("_registered_proxy", None)
        rows.append(_phone_details(item, _extra_dict(item.pop("_registered_extra_json", None))))
    return rows


def stats_by_kind() -> dict:
    """按邮箱类型分组统计，给 WebUI 顶部展示"每种邮箱各有多少号"。"""
    con = _conn()
    cur = con.execute(
        "SELECT kind, status, COUNT(*) AS n FROM outlook_accounts GROUP BY kind, status"
    )
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        k = r["kind"] or "outlook"
        slot = out.setdefault(
            k, {"available": 0, "registered": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
        )
        slot[r["status"]] = r["n"]
        slot["total"] += r["n"]
    return out


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT oa.*, r.extra_json AS _registered_extra_json, r.proxy AS _registered_proxy FROM outlook_accounts oa "
        "LEFT JOIN registered r ON lower(r.email)=lower(oa.email) WHERE oa.email=?",
        (email.lower(),),
    )
    row = cur.fetchone()
    if not row:
        return None
    item = dict(row)
    item["proxy"] = item.get("proxy") or item.pop("_registered_proxy", None) or ""
    item.pop("_registered_proxy", None)
    return _phone_details(item, _extra_dict(item.pop("_registered_extra_json", None)))


def claim_account(email: str) -> Optional[dict]:
    """原子 claim 指定邮箱（available / failed -> in_use）。

    failed 也允许重试 claim：之前 OpenAI 风控误判 / 网络抖动等导致 fail 的号
    应允许用户手动重试，已 done 的号才禁止重 claim（防误覆盖凭证）。

    按 email 指定时不过滤 kind —— 用户点名要这个号，它是什么类型
    由记录自己的 kind 列说了算，调用方读 account["kind"] 即可。
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        con = _conn()
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE email=? AND status IN ('available', 'failed')",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
            "WHERE email=? AND status IN ('available', 'failed')",
            (time.time(), email),
        )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def claim_next(kind: str = "") -> Optional[dict]:
    """原子 claim 任一 available 号。

    kind 指定 → 只从该类型里挑（"选了 gmail 就只跑 gmail 号"）
    kind 为空 → 全池子里挑最早导入的

    多类型混放的关键就在这里：号池里 outlook 和 gmail 并存，
    但当前配置选了哪种，就只 claim 哪种，不会串。
    """
    k = (kind or "").strip().lower()
    with _lock:
        con = _conn()
        for _ in range(50):  # 有限重试，避免并发抢号时无限递归爆栈
            if k:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' AND kind=? "
                    "ORDER BY imported_at ASC LIMIT 1",
                    (k,),
                )
            else:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' "
                    "ORDER BY imported_at ASC LIMIT 1"
                )
            row = cur.fetchone()
            if not row:
                return None
            rc = con.execute(
                "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
                "WHERE email=? AND status='available'",
                (time.time(), row["email"]),
            )
            con.commit()
            if rc.rowcount == 1:
                return dict(row)
            # 被别的线程抢走了，换下一个再试
        return None


def mark_done(email: str, proxy: str = "", used_proxy: Optional[bool] = None) -> None:
    """手动单次注册完成。proxy 同 mark_registered：记录本次实际用的代理。"""
    addr = (email or "").lower()
    with _lock:
        con = _conn()
        flag = "" if used_proxy is None else ", used_proxy=?"
        args = () if used_proxy is None else (1 if used_proxy else 0,)
        if (proxy or "").strip():
            con.execute(
                "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL, "
                "proxy=?" + flag + " WHERE email=?",
                (time.time(), proxy.strip()) + args + (addr,),
            )
        else:
            con.execute(
                "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL"
                + flag + " WHERE email=?",
                (time.time(),) + args + (addr,),
            )
        con.commit()


def mark_registered(email: str, proxy: str = "", used_proxy: Optional[bool] = None) -> None:
    """账号工厂完成：已注册到 2FA、凭证已落库、尚未被卡密占用。

    这是**唯一**可以给 CDK 兑换的状态（见 claim_cdk_mailbox）。注册完但没拿到
    2FA secret 的行不允许走这里 —— 无 2FA 的号不进池（调用方负责判定）。

    proxy：这次注册**实际用的**代理。以前不写，注册结果/邮箱管理里就显示成
    「直连」，看起来像没走代理 —— 实际是数据没落库。空值不覆盖已有值。
    """
    addr = (email or "").lower()
    with _lock:
        con = _conn()
        flag = "" if used_proxy is None else ", used_proxy=?"
        args = () if used_proxy is None else (1 if used_proxy else 0,)
        if (proxy or "").strip():
            con.execute(
                "UPDATE outlook_accounts SET status='registered', finished_at=?, fail_reason=NULL, "
                "proxy=?" + flag + " WHERE email=?",
                (time.time(), proxy.strip()) + args + (addr,),
            )
        else:
            con.execute(
                "UPDATE outlook_accounts SET status='registered', finished_at=?, fail_reason=NULL"
                + flag + " WHERE email=?",
                (time.time(),) + args + (addr,),
            )
        con.commit()


def ensure_pool_account(email: str, kind: str, proxy: str = "", status: str = "registered",
                        used_proxy: Optional[bool] = None) -> bool:
    """把**注册时现造出来的**地址登记成可兑换账号（非池化来源专用）。

    池化来源的号是导入进来的，注册完 mark_registered 就够；CF 临时邮箱这类
    来源的地址是注册那一刻才存在的，号池里本来没有这行 —— 不补登记，
    用户就永远兑换不到它。已绑定卡密、已完成或正在执行的行绝不覆盖。
    """
    addr = (email or "").strip().lower()
    if not addr:
        return False
    want = str(status or "registered").strip().lower() or "registered"
    now = time.time()
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT status, cdk_code FROM outlook_accounts WHERE email=?", (addr,)
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO outlook_accounts"
                "(email, password, client_id, refresh_token, relay_url, kind, status, "
                " imported_at, finished_at, proxy, used_proxy) "
                "VALUES (?, '', '', '', '', ?, ?, ?, ?, ?, ?)",
                (addr, (kind or "outlook").strip().lower(), want, now, now, (proxy or "").strip(),
                 -1 if used_proxy is None else (1 if used_proxy else 0)),
            )
        # in_use 也要允许升到 registered：号池自己现造的地址先占成 in_use，
        # 注册成功后必须能转正。已绑卡密（cdk_code 非空）或已完成的绝不动。
        elif not (row["cdk_code"] or "") and (row["status"] or "") != "done":
            con.execute(
                "UPDATE outlook_accounts SET status=?, finished_at=?, proxy=?, "
                "fail_reason=NULL, used_proxy=COALESCE(?, used_proxy) WHERE email=?",
                (want, now, (proxy or "").strip(),
                 None if used_proxy is None else (1 if used_proxy else 0), addr),
            )
        else:
            return False
        con.commit()
        return True


def mark_phone_bound(email: str, ok: bool, error: str = "", *,
                     phone_number: str = "", phone_code: str = "") -> None:
    """记录账号池的「是否接码」状态（-1 未尝试 / 1 已接码 / 0 接码失败）。

    注册阶段 best-effort 顺手验手机：成功 → 兑换时不再命中 add-phone；
    失败也不影响入池（绑上 2FA 就算 GPT 注册成功），只是兑换会走 add-phone。
    """
    addr = (email or "").strip().lower()
    if not addr:
        return
    with _lock:
        con = _conn()
        now = time.time()
        con.execute(
            "UPDATE outlook_accounts SET phone_bound_at=?, phone_bound_ok=?, "
            "fail_reason=CASE WHEN ?=0 THEN ? ELSE fail_reason END WHERE email=?",
            (now, 1 if ok else 0, 1 if ok else 0, (error or "接码失败")[:200], addr),
        )
        # Evidence belongs to the encrypted metadata column. A later status
        # update without evidence must preserve the last successful phone/code.
        evidence = {field: str(value or "").strip()
                    for field, value in (("phone_number", phone_number), ("phone_code", phone_code))
                    if str(value or "").strip()}
        registered = con.execute("SELECT extra_json FROM registered WHERE email=?", (addr,)).fetchone()
        if registered:
            extra = _extra_dict(registered["extra_json"])
            # Ordinary one-off registrations can have no pool row at all.
            # Keep their state snapshot alongside the same successful evidence.
            extra.update(phone_bound_ok=1 if ok else 0, phone_bound_at=now)
            if ok:
                extra.update(evidence)
            con.execute("UPDATE registered SET extra_json=? WHERE email=?",
                        (json.dumps(extra, ensure_ascii=False), addr))
        con.commit()


def count_phone_bound(kind: str = "", bound: bool = True) -> int:
    """号池里已接码 / 未接码（含失败）的账号数，供后台统计展示。"""
    con = _conn()
    sql = ("SELECT COUNT(*) AS n FROM outlook_accounts WHERE status IN ('registered','in_use') "
           "AND phone_bound_ok" + ("=1" if bound else "<=0"))
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(str(kind).strip().lower())
    return int(con.execute(sql, args).fetchone()["n"] or 0)


def retarget_run(run_id: str, email: str) -> None:
    """把运行记录的邮箱改成**真实的**地址。

    非池化来源注册时先给一个占位地址，真地址是 provider 现造的；不改写的话
    「运行记录」里挂着的是一串 placeholder，事后根本对不上是哪个号。
    """
    addr = (email or "").strip().lower()
    if not addr:
        return
    with _lock:
        con = _conn()
        con.execute("UPDATE runs SET email=? WHERE run_id=?", (addr, run_id))
        con.commit()


def mark_failed(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_unused(email: str) -> None:
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """手动重置单个号：done / failed → available，清空时间戳和失败原因。

    场景：注册成功但 refresh_token 没拿到，主人想重新跑一遍这个号。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            "finished_at=NULL, fail_reason=NULL "
            "WHERE lower(email)=lower(?)",
            (email,),
        )
        con.commit()
        return rc.rowcount > 0


def bulk_reset_to_available(emails: list[str]) -> int:
    """批量重置多个号。返回实际被改的行数。"""
    if not emails:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            f"UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            f"finished_at=NULL, fail_reason=NULL "
            f"WHERE lower(email) IN ({','.join(['lower(?)'] * len(emails))})",
            emails,
        )
        con.commit()
        return rc.rowcount


def reset_failed_to_available() -> int:
    """把所有 failed 号一次性重置为 available（清掉 fail_reason）。返回受影响行数。

    场景：代理短暂抽风导致一波号被冤枉标 failed，主人想给它们一次机会。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', fail_reason=NULL, "
            "finished_at=NULL WHERE status='failed'"
        )
        con.commit()
        return rc.rowcount


def release_stale_in_use(stale_seconds: float = 1800) -> int:
    """把 claimed_at 超过 N 秒还在 in_use 的号释放回 available。

    场景：上次 webui 强退/进程崩溃，号卡在 in_use 永远不释放。默认 30 分钟。
    """
    with _lock:
        con = _conn()
        cutoff = time.time() - stale_seconds
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE status='in_use' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
        con.commit()
        return rc.rowcount


def delete_account(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_accounts_by_status(status: str) -> int:
    """按状态批量删除。status 必须是 available/in_use/done/failed 之一；
    传 'all' 删全部。返回受影响行数。"""
    valid = {"available", "in_use", "done", "failed", "all"}
    s = (status or "").strip().lower()
    if s not in valid:
        return 0
    with _lock:
        con = _conn()
        if s == "all":
            rc = con.execute("DELETE FROM outlook_accounts")
        else:
            rc = con.execute("DELETE FROM outlook_accounts WHERE status=?", (s,))
        con.commit()
        return rc.rowcount


def delete_accounts_by_emails(emails: list[str]) -> int:
    """按 email 列表批量删除。返回受影响行数。"""
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM outlook_accounts WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def stats() -> dict:
    con = _conn()
    cur = con.execute(
        "SELECT status, COUNT(*) AS n FROM outlook_accounts GROUP BY status"
    )
    out = {"available": 0, "registered": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


# ──────────────────────── 注册结果存储 ────────────────────────


def authorization_counts() -> dict:
    """注册 / 接受邀请 / 完成 CLI 三个阶段各有多少号。

    以前概览只显示邮箱 `done` 一个数字，而 `done` 既有"单次注册成功"也有"Codex 授权
    完成"，于是"只注册没授权"被读成了"邀请已经做完"。
    """
    con = _conn()
    row = con.execute(
        "SELECT COUNT(*) AS tracked,"
        " SUM(CASE WHEN coalesce(invite_processed_at,0)>0 THEN 1 ELSE 0 END) AS inviteAccepted,"
        " SUM(CASE WHEN coalesce(cli_completed_at,0)>0 THEN 1 ELSE 0 END) AS cliCompleted "
        "FROM codex_auth").fetchone()
    tracked = int((row or {}).get("tracked") or 0)
    cli = int((row or {}).get("cliCompleted") or 0)
    return {
        "tracked": tracked,
        "inviteAccepted": int((row or {}).get("inviteAccepted") or 0),
        "cliCompleted": cli,
        # "已注册但还没跑完 CLI"就是 cdk 兑换后真正待办的量。
        "awaitingAuthorization": max(0, tracked - cli),
    }


def _stamp_new_jar(extra: dict) -> dict:
    """写罐就带时间：本轮**确实带了新罐**却没带时间戳时补 `cookie_jar_at`。

    为什么要补：历史导入与旧调用方只写 `cookie_jar` 不写 `cookie_jar_at`，罐年龄就成了
    "未知"。`codex_auth._sso_jar_is_fresh` 会把未知一律当过期（丢弃 auth.openai.com 的
    旧 cookie）—— 这是安全的方向，但一个本来新鲜的新罐也会因此多走一次协议登录。

    ⚠️ **只给本轮带上来的罐盖章**，绝不给出库里继承来的旧罐盖章：把真正的旧罐标成
    "刚抓的"会让它被当成新鲜罐回放 auth.openai.com 旧 cookie，正是"卡在 Codex 授权
    确认页、三次重试全废"那个坑（2026-10-09 实测）。
    """
    if extra.get("cookie_jar") and not extra.get("cookie_jar_at"):
        stamped = dict(extra)
        stamped["cookie_jar_at"] = time.time()
        return stamped
    return extra


def save_registered(d: dict) -> None:
    """保存注册成功（或部分成功）的凭证。覆盖同邮箱旧记录。

    凭证三件套（access_token / session_token / refresh_token）单独存列；
    其余字段（如 device_id / cookie_header / id_token / 自定义元数据）打包进 extra_json。
    """
    email = (d.get("email") or "").lower()
    if not email:
        return
    password = d.get("password", "") or ""
    extra = _stamp_new_jar({k: v for k, v in d.items() if k not in {
        "email", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
        "totp_secret", "totp_factor_id", "proxy", "source",
    }})
    with _lock:
        con = _conn()
        # ⚠️ extra_json 是**整列覆盖**：只传部分字段的调用（_persist_restored_session /
        #    _complete_cli 等）会把别的 extra 键冲掉 —— 例如 cookie_jar（会话罐）。
        #    与 password/totp 同样处理：本轮没传的键沿用库里的旧值。
        try:
            row = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,)).fetchone()
            existing_extra = json.loads(row["extra_json"]) if (row and row["extra_json"]) else {}
            if existing_extra:
                merged = dict(existing_extra)
                merged.update(extra)
                extra = merged
        except Exception:  # noqa: BLE001
            pass
        # ⚠️ INSERT OR REPLACE 是**整行替换**，不是按字段合并 —— 没写的列会被清空。
        #    重跑同一个邮箱时这会咬人：第一轮 register_password 设了密码但 OTP 超时，
        #    save_password_early 把密码存下了；第二轮 OpenAI 已经认识这个邮箱了，
        #    走 passwordless_login 分支根本不调 register_password，
        #    这一轮的 d["password"] 是空的 —— 直接 REPLACE 就把上一轮的密码冲没了。
        #    密码是 OpenAI 侧的**持久状态**，"这一轮没设" ≠ "这个号没有密码"，
        #    所以空值不覆盖非空旧值。
        #    token 三件套同样按"这一轮没传就别动"处理：save_registered 的调用方
        #    不只有完整注册（registrar._do_register），还有只回写个别字段的
        #    _persist_restored_session / _refresh_cookie_jar / 2FA 落库 ——
        #    它们只带 cookie 罐或 2FA，空 token 会把库里还能用的凭证冲成空。
        #    实测 2026-10-08/09：22 个池账号里 4 个注册时 at=1986/st=4115 齐全，
        #    被一次"只回写会话罐"的整行 REPLACE 清空，之后 Codex 授权只能重走登录。
        #    真需要主动清空某个 token 时，传 None 之外的显式空占位即可（见下）。
        totp_secret = (d.get("totp_secret") or "").strip()
        totp_factor_id = (d.get("totp_factor_id") or "").strip()
        proxy = (d.get("proxy") or "").strip()
        source = (d.get("source") or "").strip()
        tokens = {name: str(d.get(name) or "").strip()
                  for name in ("access_token", "session_token", "refresh_token", "id_token", "cookie_header")}
        if not password or not totp_secret or not proxy or not source or not all(tokens.values()):
            row = con.execute(
                "SELECT password, totp_secret, totp_factor_id, access_token, session_token, "
                "refresh_token, id_token, cookie_header, proxy, source FROM registered WHERE email=?",
                (email,),
            ).fetchone()
            if row:
                if not password and (row["password"] or "").strip():
                    password = row["password"]
                if not totp_secret and (row["totp_secret"] or "").strip():
                    totp_secret = row["totp_secret"]
                    # factor_id 跟着 secret 走：本轮没绑就沿用旧的
                    totp_factor_id = totp_factor_id or (row["totp_factor_id"] or "")
                # Session/2FA-only writes must not erase the recorded proxy or
                # source while retaining the rest of the registration.
                proxy = proxy or (row["proxy"] or "")
                source = source or (row["source"] or "")
                for name in tokens:
                    # 本轮没传 → 沿用库里的旧值；传了非空 → 用新值覆盖。
                    if not tokens[name] and (row[name] or "").strip():
                        tokens[name] = row[name]
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, proxy, source, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                password,
                tokens["access_token"],
                tokens["session_token"],
                tokens["refresh_token"],
                tokens["id_token"],
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                tokens["cookie_header"],
                totp_secret,
                totp_factor_id,
                proxy,
                source,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()


def refund_task_add(platform: str, activation_id: str, reason: str = "",
                    max_attempts: int = 3, delay_seconds: float = 60.0) -> int:
    """登记一个"退号重试"任务（同一 platform+id 只留一条）。返回任务 id。"""
    plat = str(platform or "").strip().lower()
    aid = str(activation_id or "").strip()
    if not plat or not aid:
        return 0
    now = time.time()
    with _lock:
        con = _conn()
        con.execute(
            "INSERT OR IGNORE INTO sms_refund_tasks"
            "(platform, activation_id, reason, attempts, max_attempts, status, next_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, 'pending', ?, ?, ?)",
            (plat, aid, str(reason or "")[:300], max(1, int(max_attempts)),
             now + max(0.0, float(delay_seconds)), now, now),
        )
        con.commit()
        row = con.execute(
            "SELECT id FROM sms_refund_tasks WHERE platform=? AND activation_id=?", (plat, aid)
        ).fetchone()
        return int(row["id"]) if row else 0


def refund_task_due(limit: int = 20) -> list[dict]:
    """取出到点该重试的任务（只取，不改状态；认领由 refund_task_claim 做）。"""
    now = time.time()
    con = _conn()
    cur = con.execute(
        "SELECT * FROM sms_refund_tasks WHERE status='pending' AND coalesce(next_at,0)<=? "
        "ORDER BY next_at ASC LIMIT ?", (now, int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def refund_task_claim(task_id: int) -> bool:
    """原子认领（多进程安全）：只有把它推到 60s 之后的那一个进程能拿到。"""
    now = time.time()
    with _lock:
        con = _conn()
        cur = con.execute(
            "UPDATE sms_refund_tasks SET next_at=?, updated_at=? "
            "WHERE id=? AND status='pending' AND coalesce(next_at,0)<=?",
            (now + 60.0, now, int(task_id), now),
        )
        con.commit()
        return cur.rowcount > 0


def refund_task_finish(task_id: int, ok: bool, error: str = "") -> dict:
    """记一次重试结果。ok=False 且次数用尽 → gave_up（不再继续退）。"""
    now = time.time()
    with _lock:
        con = _conn()
        row = con.execute("SELECT * FROM sms_refund_tasks WHERE id=?", (int(task_id),)).fetchone()
        if not row:
            return {}
        d = dict(row)
        attempts = int(d.get("attempts") or 0) + 1
        max_attempts = int(d.get("max_attempts") or 3)
        if ok:
            con.execute(
                "UPDATE sms_refund_tasks SET status='done', attempts=?, last_error='', updated_at=? WHERE id=?",
                (attempts, now, int(task_id)),
            )
        elif attempts >= max_attempts:
            con.execute(
                "UPDATE sms_refund_tasks SET status='gave_up', attempts=?, last_error=?, updated_at=? WHERE id=?",
                (attempts, str(error or "")[:300], now, int(task_id)),
            )
        else:
            con.execute(
                "UPDATE sms_refund_tasks SET attempts=?, last_error=?, next_at=?, updated_at=? WHERE id=?",
                (attempts, str(error or "")[:300], now + 60.0, now, int(task_id)),
            )
        con.commit()
        return dict(con.execute("SELECT * FROM sms_refund_tasks WHERE id=?", (int(task_id),)).fetchone())


def refund_task_list(limit: int = 50) -> list[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM sms_refund_tasks ORDER BY id DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in cur.fetchall()]


def refund_task_stats() -> dict:
    con = _conn()
    out = {"pending": 0, "done": 0, "gave_up": 0}
    for r in con.execute("SELECT status, COUNT(*) n FROM sms_refund_tasks GROUP BY status"):
        out[str(r["status"])] = int(r["n"])
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def _is_masked_secret(value) -> bool:
    """是不是前端回传的打码占位符（`***` / `•••` / `<masked>`）—— 这类值必须忽略。"""
    v = str(value if value is not None else "").strip()
    if not v:
        return False
    if all(ch in "*•·." for ch in v):
        return True
    return v.lower() in ("<masked>", "masked", "已设置", "******")


def set_registered_refresh_token(email: str, refresh_token: str) -> None:
    """把 Codex 拿到的 RT 回写进 registered.refresh_token。

    本项目（LocalFlow Register）就是这么存的：registrar.py 里
    `d["refresh_token"] = full.get("refresh_token","")` —— full 就是 flow.result，
    而 result.refresh_token 是 **Codex OAuth 换到的那个 RT**。
    所以它那页「注册结果 → refresh」列显示的就是 Codex RT。
    codex_invit 一开始只把它写进 codex_auth 表，页面 refresh 列因此是空的。
    这里只 UPDATE 一列，不碰 access_token / session_token / password / totp_secret。
    """
    mail = (email or "").strip().lower()
    rt = (refresh_token or "").strip()
    if not mail or not rt:
        return
    with _lock:
        con = _conn()
        con.execute("UPDATE registered SET refresh_token=? WHERE lower(email)=?", (rt, mail))
        con.commit()


def save_password_early(email: str, password: str) -> None:
    """密码一在 OpenAI 侧生效就落盘，不等整个注册流程跑完。

    由 AuthFlow 的 on_password 回调触发（register_password 里 POST 200 之后）。
    此刻账号+密码在 OpenAI 那边已经建好，但本地还要过发码/验证/建账户三关，
    挂在任何一关都走不到 save_registered ——
    密码只活在内存里，进程一退号就成了谁也登不进去的孤儿。

    只写 email + password；token 三件套留空，等流程跑通后 save_registered
    用同一个 email 主键覆盖同一行补上。extra_json 打 pending 标记，
    方便一眼认出"有密码没凭证"的半成品行（跑通后会被 save_registered 清掉）。

    ⚠️ 行已存在时**只 UPDATE password**，绝不动已有的 token：
       重跑一个之前跑通过的邮箱时，不能把人家的凭证清空。
    """
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        return
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, ?, '', '', '', '', '', '', '', ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET password=excluded.password",
            (
                email,
                password,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def normalize_totp_secret(raw: str) -> str:
    """把用户手填的 TOTP secret 规范化成可用的 base32，非法值抛 ValueError。

    登录侧（auth_flow._totp_now）拿到 secret 直接 b32decode，**不做任何校验** ——
    脏值存进去要等到真登录时才炸，那时只看到一句 base32 解码异常，
    根本看不出是手填填错了。所以校验必须挡在写库这一关。

    接受的输入：
      - 裸 base32:  JBSWY3DPEHPK3PXP / jbswy3dp ehpk 3pxp / JBSW-Y3DP-EHPK
      - otpauth URI: otpauth://totp/ChatGPT:a@b.com?secret=JBSWY3DP&issuer=...
        （从手机 App 导出/二维码解码出来的就是这个格式，直接粘进来很常见）
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # otpauth:// URI 抽 secret 参数
    if s.lower().startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(s).query)
            s = (qs.get("secret") or [""])[0]
        except Exception:
            raise ValueError("otpauth 链接解析失败，请直接填 secret")
        if not s:
            raise ValueError("otpauth 链接里没有 secret 参数")
    # 去掉分隔符（手机 App 展示时常带空格/连字符）并统一大写
    s = s.replace(" ", "").replace("-", "").replace("_", "").upper()
    # base32 只有 A-Z 和 2-7，先挡掉明显非法字符再解码，报错更好懂
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s):
        raise ValueError("TOTP secret 含非法字符（base32 只允许 A-Z 和 2-7）")
    try:
        # 补 padding 后试解，解得开才算合法。auth_flow 那边也是这么补的。
        decoded = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise ValueError("TOTP secret 不是合法的 base32")
    if len(decoded) < 10:
        raise ValueError(f"TOTP secret 太短（解出 {len(decoded)} 字节，通常应为 20 字节）")
    return s


def update_registered_manual(email: str, password: Optional[str] = None,
                             totp_secret: Optional[str] = None) -> bool:
    """手动修正某个已注册账号的密码 / TOTP secret。

    ⚠️ 只改**本地库**，不会同步到 OpenAI —— 这里改密码不等于改了账号密码。
       用途是把外部已知的凭证补进来，或修正记录错误。

    传 None = 该字段不动（不是清空）。用 None 而不是空串做"不修改"的标记，
    是为了留出"主人真想清空某字段"的余地（传空串即清空）。

    totp_secret 会先过 normalize_totp_secret 校验，非法直接抛 ValueError；
    宁可这里报错，也不能让脏值躺进库里等登录时才炸。

    返回 False 表示该邮箱不存在（不会凭空插入新行 —— 手填是"修正已有记录"，
    真要新增外部账号是另一件事，走单独的导入功能）。
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    sets, vals = [], []
    if password is not None:
        sets.append("password=?")
        vals.append(password)
    if totp_secret is not None:
        # 空串 = 主人主动清空；非空则必须过校验
        sets.append("totp_secret=?")
        vals.append(normalize_totp_secret(totp_secret) if totp_secret.strip() else "")
    if not sets:
        return False
    with _lock:
        con = _conn()
        row = con.execute("SELECT email FROM registered WHERE email=?", (email,)).fetchone()
        if not row:
            return False
        vals.append(email)
        con.execute(f"UPDATE registered SET {', '.join(sets)} WHERE email=?", vals)
        con.commit()
        return True


def update_plus_check(email: str, plus_info: dict) -> None:
    """把 Plus 检查结果写入 extra_json.plus_check。"""
    email = email.lower()
    con = _conn()
    cur = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return
    extra = {}
    if row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except Exception:
            extra = {}
    extra["plus_check"] = plus_info
    with _lock:
        con.execute(
            "UPDATE registered SET extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.commit()


def _registered_where(filt: str) -> str:
    if filt == "has_rt":
        return "WHERE length(r.refresh_token) > 0"
    if filt == "no_rt":
        return "WHERE coalesce(length(r.refresh_token),0) = 0"
    if filt == "unchecked":
        return "WHERE (r.extra_json IS NULL OR r.extra_json NOT LIKE '%\"plus_check\"%')"
    if filt == "free":
        return "WHERE r.extra_json LIKE '%\"free\"%'"
    if filt == "plus":
        return "WHERE (r.extra_json LIKE '%\"plus_eligible\"%' OR r.extra_json LIKE '%\"plus_active\"%')"
    if filt == "banned":
        return "WHERE r.extra_json LIKE '%\"banned\"%'"
    if filt == "token_invalid":
        # token_invalid 从 2026-08-10 起会写库，得能筛出来，否则等于埋了：
        # 它既不在 unchecked 里（已有结论），又不在 free/plus/banned 里。
        return "WHERE r.extra_json LIKE '%\"token_invalid\"%'"
    return ""


def count_registered(filter_rt: str = "all") -> int:
    con = _conn()
    cur = con.execute(f"SELECT COUNT(*) FROM registered r {_registered_where(filter_rt)}")
    return cur.fetchone()[0]


def list_registered(limit: int = 20, offset: int = 0, filter_rt: str = "all") -> list[dict]:
    con = _conn()
    where = _registered_where(filter_rt)
    # ⚠️ 注册结果页的 refresh 列读的是 registered.refresh_token（网页登录的 RT），
    #    而 **Codex 授权拿到的 RT 在 codex_auth 表**里 —— 以前页面根本不显示它，
    #    主人「拿到 RT 却在页面上找不到」就是这么来的。这里 LEFT JOIN 一起带出来。
    cur = con.execute(
        f"SELECT r.email, r.password, r.totp_secret, r.proxy, r.source, "
        f"length(r.access_token) AS at_len, length(r.session_token) AS st_len, "
        f"length(r.refresh_token) AS rt_len, r.extra_json, r.created_at, "
        f"length(ca.refresh_token) AS codex_rt_len, "
        f"length(ca.access_token) AS codex_at_len, "
        f"ca.account_id AS codex_account_id, ca.cli_completed_at AS codex_cli_at, "
        f"ca.invite_processed_at AS codex_invite_at, {_REGISTERED_POOL_COLUMNS} "
        f"FROM registered r LEFT JOIN codex_auth ca ON lower(ca.email)=lower(r.email) "
        f"{_REGISTERED_POOL_JOIN} "
        f"{where} "
        f"ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [_registered_details(dict(row)) for row in cur.fetchall()]


def list_registered_full(limit: int = 5000) -> list[dict]:
    """返回完整凭证（用于批量导出）。每行同 get_registered 的格式。"""
    con = _conn()
    cur = con.execute(
        f"SELECT r.*, {_REGISTERED_POOL_COLUMNS} FROM registered r "
        f"{_REGISTERED_POOL_JOIN} ORDER BY r.created_at DESC LIMIT ?",
        (limit,),
    )
    return [_registered_details(dict(row), full=True) for row in cur.fetchall()]


def list_registered_by_emails(emails: list[str]) -> list[dict]:
    """按 email 列表返回完整凭证（批量导出勾选的号用）。

    - 行序 = created_at 倒序，和「注册结果」表格里看到的一致，方便核对。
    - 查不到的 email 直接不出现（号已被删掉的情况），不报错。
    - SQLite 单条语句变量数有上限（默认 999），所以分批查。
    """
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return []

    con = _conn()
    out = []
    CHUNK = 500
    for i in range(0, len(cleaned), CHUNK):
        part = cleaned[i:i + CHUNK]
        placeholders = ",".join("?" * len(part))
        cur = con.execute(
            f"SELECT r.*, {_REGISTERED_POOL_COLUMNS} FROM registered r "
            f"{_REGISTERED_POOL_JOIN} WHERE r.email IN ({placeholders})",
            part,
        )
        for row in cur.fetchall():
            out.append(_registered_details(dict(row), full=True))

    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out


def get_registered(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute(
        f"SELECT r.*, {_REGISTERED_POOL_COLUMNS} FROM registered r "
        f"{_REGISTERED_POOL_JOIN} WHERE r.email=?",
        (email.lower(),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return _registered_details(dict(row), full=True)


def delete_registered(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_registered_by_emails(emails: list[str]) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM registered WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def delete_all_registered() -> int:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered")
        con.commit()
        return rc.rowcount


# ──────────────────────── 运行记录 ────────────────────────


def create_run(run_id: str, email: str, log_path: str, kind: str = "register") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO runs(run_id, email, status, started_at, log_path, kind, owner_pid) "
            "VALUES (?, ?, 'running', ?, ?, ?, ?)",
            (run_id, email.lower(), time.time(), log_path, kind or "register", os.getpid()),
        )
        con.commit()


def recover_stale_runs() -> int:
    """把「进程已经不存在、状态还写着 running」的 run 标记为中断。

    判断依据是 owner_pid 是否还活着（os.kill(pid, 0)）—— 所以**跨进程安全**：
    管理后台重启不会误杀用户前台正在跑的作业。返回修好的条数。
    """
    fixed = 0
    con = _conn()
    rows = con.execute(
        "SELECT run_id, owner_pid FROM runs WHERE status='running'"
    ).fetchall()
    for r in rows:
        pid = r["owner_pid"]
        alive = False
        if pid:
            try:
                os.kill(int(pid), 0)
                alive = True
            except (ProcessLookupError, ValueError):
                alive = False
            except PermissionError:
                alive = True          # 存在但不属于我 → 当作活着
        if alive:
            continue
        con.execute(
            "UPDATE runs SET status='failed', finished_at=?, error=?, error_category=? "
            "WHERE run_id=? AND status='running'",
            (time.time(), "进程中断（服务重启或被手动停止）", "interrupted", r["run_id"]),
        )
        fixed += 1
    if fixed:
        con.commit()
    return fixed


def set_run_stage(run_id: str, stage: str) -> None:
    """记运行记录当前阶段（进度% 由调用方按 USER_STAGE_PCT 换算）。

    账号工厂补号没有卡密作业，运行记录页只能靠这个字段显示进度 ——
    以前它一直是空的，进度就卡在 0% 然后直接跳 100%。
    """
    name = str(stage or "").strip()
    if not name:
        return
    with _lock:
        con = _conn()
        con.execute("UPDATE runs SET stage=? WHERE run_id=?", (name, run_id))
        con.commit()


def get_run(run_id: str) -> Optional[dict]:
    """读一条运行记录（error 等机密列会被上层解密）。"""
    con = _conn()
    row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def recover_runs_after_restart() -> int:
    """启动时把上次进程留下的 running 清成"已中断"。

    ⚠️ 不能用 recover_stale_runs 的 owner_pid 判断：容器重建后 pid 会复用
    （两次都是 7），os.kill(7,0) 认为"还活着"，僵尸 run 就永远挂在「运行中」，
    运行记录里看着像卡死。调用方此时刚拿到执行器排他锁，因此任何 running 行
    都确定不属于本进程。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE runs SET status='failed', finished_at=?, error=?, error_category=? "
            "WHERE status='running'",
            (time.time(), "进程中断（服务重启或被手动停止）", "interrupted"),
        )
        con.commit()
        return rc.rowcount


def finish_run(run_id: str, status: str, error: str = "", category: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runs SET status=?, finished_at=?, error=?, error_category=? WHERE run_id=?",
            (status, time.time(), (error or "")[:500], category or None, run_id),
        )
        con.commit()


def _runs_where(status: str = "", kind: str = "", search: str = "") -> tuple:
    """运行记录的筛选条件。只搜**非机密列**：邮箱/错误/run_id 在库里是加密标记，
    子串匹配不到明文，硬搜只会给出"明明有却查不到"的假象。"""
    clauses, args = [], []
    if str(status or "").strip():
        clauses.append("status=?")
        args.append(str(status).strip())
    if str(kind or "").strip():
        clauses.append("kind=?")
        args.append(str(kind).strip())
    if str(search or "").strip():
        pattern = f"%{str(search).strip()}%"
        clauses.append("(stage LIKE ? OR kind LIKE ? OR status LIKE ?)")
        args += [pattern, pattern, pattern]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def list_runs(limit: int = 50, *, status: str = "", kind: str = "",
              search: str = "", offset: int = 0) -> list[dict]:
    """运行记录（新的在前）。筛选分页在**服务端**做，前端不要再截断后再本地筛。"""
    where, args = _runs_where(status, kind, search)
    con = _conn()
    cur = con.execute(
        f"SELECT * FROM runs{where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
        (*args, max(0, int(limit)), max(0, int(offset))),
    )
    return [dict(r) for r in cur.fetchall()]


def list_runs_for_emails(emails: list[str], limit: int = 5) -> list[dict]:
    """Batch the recent exact-email history needed by legacy evidence lookup."""
    cleaned = sorted({str(email).strip().lower() for email in (emails or [])
                      if str(email or "").strip()})
    if not cleaned:
        return []
    per_email = max(1, min(100, int(limit)))
    con = _conn()
    out = []
    for i in range(0, len(cleaned), 500):
        batch = cleaned[i:i + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = con.execute(
            "SELECT recent.* FROM (SELECT r.*, ROW_NUMBER() OVER "
            "(PARTITION BY lower(r.email) ORDER BY r.started_at DESC, r.run_id DESC) AS _run_rank "
            f"FROM runs r WHERE lower(r.email) IN ({placeholders})) recent "
            "WHERE recent._run_rank<=? ORDER BY recent.started_at DESC",
            batch + [per_email],
        ).fetchall()
        for row in rows:
            item = dict(row)
            item.pop("_run_rank", None)
            out.append(item)
    out.sort(key=lambda row: row.get("started_at") or 0, reverse=True)
    return out


def count_runs(*, status: str = "", kind: str = "", search: str = "") -> int:
    """与 list_runs 同一套筛选条件的总数，供分页显示。"""
    where, args = _runs_where(status, kind, search)
    con = _conn()
    row = con.execute(f"SELECT COUNT(*) AS n FROM runs{where}", args).fetchone()
    return int(row["n"]) if row else 0


# ──────────────────────── settings (KV) ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    from smsinvit.invitation.runtime import TASK_SETTINGS
    snapshot=TASK_SETTINGS.get()
    if snapshot is not None:return snapshot.get(key,default)
    con = _conn()
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()


# ──────────────────────── 邮箱来源配置 ────────────────────────


def get_mail_config() -> dict:
    """返回邮箱来源配置（密码类字段隐藏明文）。

    provider 声明的配置项自动带出来 —— 加新邮箱时这里不用改，
    新 provider 的 config_fields 会自动出现在返回值里。
    """
    from mail_providers import list_providers

    # pool_mail_source = 账号工厂（补号）单独用的来源；留空表示跟随 mail_source。
    # 补号与兑换各配各的，互不影响。
    out = {
        "mail_source": get_setting("mail_source", "outlook"),
        "pool_mail_source": get_setting("pool_mail_source", ""),
    }
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if f.get("type") == "password":
                out[key] = "***" if get_setting(key) else ""
            else:
                out[key] = get_setting(key, "")
    return out


def save_mail_config(data: dict) -> None:
    """保存邮箱配置。password 类字段传 '***' 表示不修改。

    mail_source 校验改成查 mail_providers 注册表：
        以前是写死的白名单 ("outlook", "cf_temp")，选了别的会被
        **静默改回 outlook** —— 用户看到的是"保存成功但选择没生效"。
        现在未知来源直接抛错，问题当场暴露。
    """
    from mail_providers import get_provider_class, list_providers

    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        get_provider_class(src)  # 未注册的 kind 会抛 MailProviderError
        set_setting("mail_source", src)

    # 补号来源：留空 = 跟随 mail_source；填了就必须是已注册的来源。
    if "pool_mail_source" in data:
        pool_src = str(data["pool_mail_source"]).strip().lower()
        if pool_src:
            get_provider_class(pool_src)
        set_setting("pool_mail_source", pool_src)

    # 按 provider 声明的字段保存，加新邮箱时这里零改动
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if key not in data:
                continue
            val = data[key]
            if f.get("type") == "password":
                if not val or val == "***":
                    continue  # 没填 / 是掩码 → 保持原值
            set_setting(key, str(val).strip())


def get_secret_setting(key: str) -> str:
    """内部用：拿密码类配置的明文。"""
    return get_setting(key, "")


def get_mail_settings() -> dict:
    """内部用：给 create_mail_provider 的 settings（含明文密钥）。

    跟 get_mail_config 的区别：这个不打码，只在服务端构造 provider 时用，
    绝不能直接返回给前端。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            out[f["key"]] = get_setting(f["key"], "")
    return out


def get_cf_admin_token() -> str:
    """内部用：拿明文 admin_token。"""
    return get_setting("cf_admin_token", "")


# ──────────────────────── SMS 接码配置 ────────────────────────


def get_sms_config() -> dict:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    各平台独立 Key：sms_api_key_smsbower / sms_api_key_herosms / sms_api_key_smspool。

    sms_platform_mode:    auto / smsbower / herosms / smspool
    sms_use_monitor_best: '1' = 用 5s 价格任务算出的最优结果覆盖
                          sms_country / sms_max_price（默认开）
    sms_blocked_prefixes: 排除号码前缀，支持 '52:66,52:81' 或 '5266,5281'
    sms_excluded_countries: 排除国家（优先级高于 sms_allowed_countries）
    sms_physical_only:    '1' = 排除虚拟号，只按实体卡(physicalCount)计库存
    """
    return {
        # ── 平台 ──
        "sms_enabled":             get_setting("sms_enabled", "0"),
        # '1' = 把租到的真实号码与收到的验证码打进运行日志（默认开，便于主人核对）
        "sms_log_otp":             get_setting("sms_log_otp", "1"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_platform_mode":       get_setting("sms_platform_mode", "auto"),
        "sms_api_key_smsbower":    "***" if get_setting("sms_api_key_smsbower") else "",
        "sms_api_key_herosms":     "***" if get_setting("sms_api_key_herosms") else "",
        "sms_api_key_smspool":     "***" if get_setting("sms_api_key_smspool") else "",
        "sms_service":             get_setting("sms_service", "dr"),
        # ── 国家 ──
        "sms_country":             get_setting("sms_country", ""),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_excluded_countries":  get_setting("sms_excluded_countries", ""),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0"),
        # ── 号码前缀 ──
        "sms_blocked_prefixes":    get_setting("sms_blocked_prefixes", ""),
        # ── 价格 / 库存 ──
        "sms_min_price":           get_setting("sms_min_price", ""),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_price_margin":        get_setting("sms_price_margin", "0"),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_auto_top_n":          get_setting("sms_auto_top_n", "60"),
        "sms_physical_only":       get_setting("sms_physical_only", "0"),
        # ── 供货商 ──
        "sms_exclude_providers":   get_setting("sms_exclude_providers", ""),
        "sms_include_providers":   get_setting("sms_include_providers", ""),
        # ── 自动择优（价格任务）──
        "sms_use_monitor_best":    get_setting("sms_use_monitor_best", "1"),
        "sms_price_weight":        get_setting("sms_price_weight", "0.5"),
        "sms_min_success_rate":    get_setting("sms_min_success_rate", "0"),
        "sms_price_poll_seconds":  get_setting("sms_price_poll_seconds", "5"),
        "sms_auto_country":        get_setting("sms_auto_country", "0"),
    }


def save_sms_config(cfg: dict) -> None:
    """保存 SMS 配置。

    ⚠️ 打码占位符（`***`）必须理解为「保持不变」：
    `get_sms_config()` 把已配置的 Key 打码成 `***` 返回给前端，
    前端原样回传 —— 如果不识别，就会把真 Key 覆盖成 `***`
    （或像 2026-10-07 那样被长度校验拦住、连好 Key 一起存不进去）。
    """
    obsolete = {"sms_reuse_phone", "sms_phone_success_max", "sms_prefix_max_retry",
                "sms_max_phone_attempts", "sms_per_phone_timeout", "sms_code_retries_per_phone"}
    data = {key: value for key, value in (cfg or {}).items() if key not in obsolete}
    for k in list(data):
        if _is_masked_secret(data[k]):
            data.pop(k, None)          # 打码 = 不改这一项
    with _lock:
        con = _conn()
        for k, v in data.items():
            if v is None:
                continue
            con.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)),
            )
        con.commit()


def get_sms_internal_config() -> dict:
    """内部用：拿各平台明文 api_key，供 sms_provider / 价格任务使用。"""
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_platform_mode":       get_setting("sms_platform_mode", "auto"),
        "sms_api_key":             get_setting("sms_api_key", ""),
        "sms_api_key_smsbower":    get_setting("sms_api_key_smsbower", ""),
        "sms_api_key_herosms":     get_setting("sms_api_key_herosms", ""),
        "sms_api_key_smspool":     get_setting("sms_api_key_smspool", ""),
        "sms_country":             get_setting("sms_country", ""),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_min_price":           get_setting("sms_min_price", ""),
        "sms_price_margin":        get_setting("sms_price_margin", "0"),
        "sms_auto_country":        get_setting("sms_auto_country", "0") in ("1", "true"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0") in ("1", "true"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_excluded_countries":  get_setting("sms_excluded_countries", ""),
        "sms_blocked_prefixes":    get_setting("sms_blocked_prefixes", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_auto_top_n":          get_setting("sms_auto_top_n", "60"),
        "sms_physical_only":       get_setting("sms_physical_only", "0") in ("1", "true"),
        "sms_exclude_providers":   get_setting("sms_exclude_providers", ""),
        "sms_include_providers":   get_setting("sms_include_providers", ""),
        "sms_use_monitor_best":    get_setting("sms_use_monitor_best", "1") in ("1", "true"),
        "sms_price_weight":        get_setting("sms_price_weight", "0.5"),
        "sms_min_success_rate":    get_setting("sms_min_success_rate", "0"),
        "sms_price_poll_seconds":  get_setting("sms_price_poll_seconds", "5"),
    }


# ──────────────────────── 自动导出配置 (CPA / SUB2API) ────────────────────────


def get_export_config() -> dict:
    """返回导出配置（敏感字段做明文/'***' 占位）。

    给前端展示用：
      cpa_mgmt_key / sub2api_api_key 已设置时返回 '***'，未设置返回 ''。
      保存时传 '***' 代表不修改。
    """
    return {
        # CPA
        "cpa_enabled":     get_setting("export_cpa_enabled", "0"),
        "cpa_url":         get_setting("export_cpa_url", ""),
        "cpa_mgmt_key":    "***" if get_setting("export_cpa_mgmt_key") else "",
        "cpa_timeout":     get_setting("export_cpa_timeout", "30"),
        # SUB2API
        "sub2api_enabled":    get_setting("export_sub2api_enabled", "0"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    "***" if get_setting("export_sub2api_api_key") else "",
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }


def save_export_config(data: dict) -> None:
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    # 布尔开关
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
    ):
        if key_in in data:
            v = data[key_in]
            if isinstance(v, bool):
                set_setting(key_out, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key_out, "1" if s in ("1", "true", "yes", "on") else "0")
    # 字符串字段（明文）
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, str(data[key_in] or "").strip())
    # 密文字段（'***' 不修改）
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。供 registrar / app.test 调用。

    返回两个子配置 dict，可分别传给 exporter.export_to_cpa / export_to_sub2api。
    """
    cpa = {
        "enabled":      get_setting("export_cpa_enabled", "0") in ("1", "true"),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":            get_setting("export_sub2api_enabled", "0") in ("1", "true"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


# ════════════════════════════════════════════════════════════════
#  codex_invit 新增
# ════════════════════════════════════════════════════════════════

# ──────────────────────── 代理池（服务端） ────────────────────────


def _proxy_id(con, ident) -> Optional[int]:
    """ident 可以是 int id、数字字符串、或 url。"""
    if isinstance(ident, bool):
        return None
    if isinstance(ident, int):
        row = con.execute("SELECT id FROM proxy_pool WHERE id=?", (ident,)).fetchone()
    elif isinstance(ident, str) and ident.strip().isdigit():
        row = con.execute("SELECT id FROM proxy_pool WHERE id=?", (int(ident.strip()),)).fetchone()
    else:
        row = con.execute(
            "SELECT id FROM proxy_pool WHERE url=?", (str(ident or "").strip(),)
        ).fetchone()
    return row["id"] if row else None


def _proxy_log(con, pid: int, event: str, detail: str = "") -> None:
    url = ""
    row = con.execute("SELECT url FROM proxy_pool WHERE id=?", (pid,)).fetchone()
    if row:
        url = row["url"]
    con.execute(
        "INSERT INTO proxy_events(proxy_id, url, event, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (pid, url, event, (detail or "")[:500], time.time()),
    )


def proxy_add_many(urls: list[str]) -> dict:
    """批量新增代理（按 url 去重）。返回 {added, skipped}。"""
    now = time.time()
    added = skipped = 0
    with _lock:
        con = _conn()
        for raw in urls or []:
            url = str(raw or "").strip()
            if not url:
                continue
            if con.execute("SELECT id FROM proxy_pool WHERE url=?", (url,)).fetchone():
                skipped += 1
                continue
            cur = con.execute(
                "INSERT INTO proxy_pool(url, enabled, added_at, updated_at) VALUES (?, 1, ?, ?)",
                (url, now, now),
            )
            _proxy_log(con, cur.lastrowid, "add")
            added += 1
        con.commit()
    return {"added": added, "skipped": skipped}


def proxy_replace_all(urls: list[str]) -> dict:
    """整池覆盖：删掉不在新列表里的，保留仍在列表里的（**保留其统计**）。"""
    cleaned = [str(u or "").strip() for u in (urls or [])]
    cleaned = [u for u in cleaned if u]
    keep = set(cleaned)
    with _lock:
        con = _conn()
        existing = [r["url"] for r in con.execute("SELECT url FROM proxy_pool").fetchall()]
        for url in existing:
            if url not in keep:
                con.execute("DELETE FROM proxy_pool WHERE url=?", (url,))
        con.commit()
    return proxy_add_many(cleaned)


def proxy_remove(ident) -> bool:
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return False
        con.execute("DELETE FROM proxy_pool WHERE id=?", (pid,))
        con.commit()
        return True


def proxy_set_enabled(ident, enabled: bool) -> bool:
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return False
        con.execute(
            "UPDATE proxy_pool SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, time.time(), pid),
        )
        _proxy_log(con, pid, "enable" if enabled else "disable")
        con.commit()
        return True


def proxy_reset(ident, purpose: str = "both") -> bool:
    """管理员重置。

    purpose='register'/'codex' → 只清该用途的 409 生命周期（永久不可用必须能复位，
    否则不可逆）；'both'（默认）→ 两个维度 + 旧列一起清空并重新启用。
    """
    key = str(purpose or "both").strip().lower()
    health_only = key in ("health", "transport", "unavailable")
    dims = list(PROXY_PURPOSES) if key in ("", "both", "all", "*") or health_only else [normalize_proxy_purpose(key)]
    fields = ["enabled=1", "last_error=NULL"]
    if key in ("", "both", "all", "*") or health_only:
        fields += ["use_count=0", "success_count=0", "fail_count=0",
                   "consecutive_409=0", "total_409=0", "frozen_once=0",
                   "frozen_until=NULL", "freeze_count=0", "permanent_dead=0"]
    for p in ([] if health_only else dims):
        fields += [f"consecutive_409_{p}=0", f"total_409_{p}=0", f"frozen_once_{p}=0",
                   f"frozen_until_{p}=NULL", f"freeze_count_{p}=0", f"permanent_dead_{p}=0"]
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return False
        con.execute(f"UPDATE proxy_pool SET {', '.join(fields)}, updated_at=? WHERE id=?",
                    (time.time(), pid))
        _proxy_log(con, pid, "reset", "purpose=" + (key if health_only else ",".join(dims)))
        con.commit()
        return True


_PROXY_SCHEMES = ("http", "https", "socks5h")


def normalize_proxy_scheme(scheme) -> str:
    """协议词归一：socks5 与 socks5h 同义（我们统一按远端解析走 socks5h）。"""
    wanted = str(scheme or "").strip().lower()
    if wanted == "socks5":
        wanted = "socks5h"
    if wanted not in _PROXY_SCHEMES:
        raise ValueError("不支持的代理协议")
    return wanted


def proxy_set_scheme(ident, scheme: str, *, clear_locks: bool = True) -> Optional[dict]:
    """原地更换协议：保留同一个 id 与全部统计，只改 url。

    为什么不让前端改了文本再整池重存：url 是唯一键，重写整池等于删旧行插新行，
    use_count / 409 生命周期 / permanent_dead 全部归零；而且没有
    invitation.credentials 权限时前端拿到的 url 是脱敏的，根本重写不出来。
    按 id 改则完全由服务端自己拼地址。

    clear_locks：换协议就是换传输方式，把"HTTP 永久不可用"的锁带过去等于白换，
    所以默认一并解除冻结与永久不可用，并写一条 scheme 事件留痕。
    """
    wanted = normalize_proxy_scheme(scheme)
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return None
        row = con.execute("SELECT url FROM proxy_pool WHERE id=?", (pid,)).fetchone()
        url = str((row or {}).get("url") or "")
        authority = url.split("://", 1)[1] if "://" in url else url
        target = f"{wanted}://{authority}"
        if target != url:
            if con.execute("SELECT id FROM proxy_pool WHERE url=? AND id<>?", (target, pid)).fetchone():
                raise sqlite3.IntegrityError("该协议地址已存在")
            con.execute("UPDATE proxy_pool SET url=?, updated_at=? WHERE id=?",
                        (target, time.time(), pid))
        if clear_locks:
            # 只解**锁**，不动**统计**：可用性由 enabled/permanent_dead/frozen_until
            # 与两个用途维度的对应列决定（见 _proxy_available_where），
            # use_count / success_count / fail_count / total_409 / freeze_count 留着。
            fields = ["enabled=1", "last_error=NULL",
                      "consecutive_409=0", "frozen_once=0", "frozen_until=NULL", "permanent_dead=0"]
            for p in PROXY_PURPOSES:
                fields += [f"consecutive_409_{p}=0", f"frozen_once_{p}=0",
                           f"frozen_until_{p}=NULL", f"permanent_dead_{p}=0"]
            con.execute(f"UPDATE proxy_pool SET {', '.join(fields)}, updated_at=? WHERE id=?",
                        (time.time(), pid))
        _proxy_log(con, pid, "scheme", f"{url} -> {target}" + ("（已解除冻结/永久不可用）" if clear_locks else ""))
        con.commit()
        return {"id": pid, "url": target, "previousUrl": url, "changed": target != url,
                "locksCleared": bool(clear_locks)}


# 代理的两个用途维度：注册锁与激活锁互不影响（见 migrations/V11）。
PROXY_PURPOSES = ("register", "codex")
_PROXY_PURPOSE_ALIASES = {
    "register": "register", "reg": "register", "signup": "register", "gpt": "register",
    "codex": "codex", "auth": "codex", "activate": "codex", "activation": "codex",
}


def normalize_proxy_purpose(purpose) -> str:
    """把外部传进来的用途词收敛到 register / codex（默认 codex）。"""
    return _PROXY_PURPOSE_ALIASES.get(str(purpose or "").strip().lower(), "codex")


def _proxy_available_where(purpose: str = "codex") -> str:
    """可用 = 已启用 + 代理自身健康 + 该用途维度没被锁。

    健康维度复用旧列（permanent_dead/frozen_until/…）：它们记录的是**代理自己**
    的问题（连不上、CONNECT 被拒），与用途无关 —— 隧道被拒时两种用途都不可用。
    业务维度（_register/_codex）才是按用途独立的 409 锁。
    """
    p = normalize_proxy_purpose(purpose)
    return ("enabled=1 AND permanent_dead=0 AND (frozen_until IS NULL OR frozen_until <= ?) "
            f"AND permanent_dead_{p}=0 AND (frozen_until_{p} IS NULL OR frozen_until_{p} <= ?)")


def proxy_available_count(exclude: Optional[list] = None, purpose: str = "codex") -> int:
    ex = [str(x).strip() for x in (exclude or []) if str(x).strip()]
    con = _conn()
    sql = f"SELECT COUNT(*) AS n FROM proxy_pool WHERE {_proxy_available_where(purpose)}"
    args: list = [time.time(), time.time()]
    if ex:
        sql += " AND url NOT IN (" + ",".join("?" * len(ex)) + ")"
        args += ex
    return con.execute(sql, args).fetchone()["n"]


def proxy_pick(exclude: Optional[list] = None, purpose: str = "codex",
               exclude_bound_to: Optional[str] = None,
               allow_shared: bool = True) -> Optional[dict]:
    """按 least-used 轮换取一个可用代理，并原子自增 use_count。

    排序：use_count 升序 → 从未用过（last_used_at 为空）优先 → 最久未使用优先。
    一个作业一次尝试只选一次，保证「同一代理 IP」；不同任务自然轮换。

    ── 独占规则（一个代理条目 = 一个出口 IP = 同一时刻只服务一个号）──
    代理池里**一条 = 一个出口 IP**。两个号共用一条时，它们的会话罐 / session_token
    都建在同一个 IP 上、互相踩，所以除自己那条外，已被别的号占用的代理不再发出去。

      · `exclude_bound_to=<邮箱>`：把「已被别的号占用」的排除掉；自己那条不算占用，
        所以重试时仍然拿得回自己那条。
      · `exclude_bound_to=None` + `allow_shared=False`：排除**所有**被占用的（严格
        模式，补号用 —— 宁可这轮不补号，也不跟别的号共用一个出口）。
      · 排完一个都不剩且 `allow_shared=True`：退回原来的「轮换复用」行为，避免把
        重试机会直接归零（补号走严格模式，不受这条影响）。

    「占用」的判据：`outlook_accounts` 里有代理、且状态还没到终态
    （done / failed / gave_up）。
    """
    ex = [str(x).strip() for x in (exclude or []) if str(x).strip()]
    now = time.time()
    order = " ORDER BY use_count ASC, (last_used_at IS NULL) DESC, last_used_at ASC LIMIT 1"
    with _lock:
        con = _conn()
        bound_sql = ("SELECT DISTINCT proxy FROM outlook_accounts "
                     "WHERE proxy IS NOT NULL AND proxy <> '' "
                     "AND status NOT IN ('done', 'failed', 'gave_up')")
        bound_args: list = []
        if exclude_bound_to:
            bound_sql += " AND email <> ?"
            bound_args.append(str(exclude_bound_to).strip().lower())
        bound = [str(r["proxy"]).strip()
                 for r in con.execute(bound_sql, bound_args).fetchall() if r["proxy"]]

        def _select(blocked: list):
            sql = f"SELECT * FROM proxy_pool WHERE {_proxy_available_where(purpose)}"
            args: list = [now, now]
            if blocked:
                sql += " AND url NOT IN (" + ",".join("?" * len(blocked)) + ")"
                args += blocked
            return con.execute(sql + order, args).fetchone()

        blocked = list(dict.fromkeys(ex + bound))
        row = _select(blocked) if blocked else _select([])
        if row is None and allow_shared:
            # 排完一个不剩 → 退回原来的行为（只排除调用方指定的那批）。
            row = _select(ex) if ex else _select([])
        if not row:
            return None
        con.execute(
            "UPDATE proxy_pool SET use_count=use_count+1, last_used_at=?, updated_at=? WHERE id=?",
            (now, now, row["id"]),
        )
        _proxy_log(con, row["id"], "use")
        con.commit()
        out = dict(row)
        out["use_count"] = (out.get("use_count") or 0) + 1
        return out


def proxy_release_use(ident) -> None:
    """尝试还没真正发起网络请求就结束时，把 use_count 回退，避免误计。"""
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return
        con.execute(
            "UPDATE proxy_pool SET use_count=MAX(use_count-1,0), updated_at=? WHERE id=?",
            (time.time(), pid),
        )
        con.commit()


def proxy_record_success(ident, purpose: str = "codex") -> None:
    """该代理在**这个用途**上确认可用：只清该维度的连续 409 计数。

    两个维度刻意互不影响：注册 warmup 通过不代表 Codex 激活不会 409，反之亦然；
    混在一起清会掩盖另一个用途的真实失败率。
    """
    p = normalize_proxy_purpose(purpose)
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return
        con.execute(
            f"UPDATE proxy_pool SET success_count=success_count+1, consecutive_409_{p}=0, "
            "last_error=NULL, updated_at=? WHERE id=?",
            (time.time(), pid),
        )
        _proxy_log(con, pid, f"success:{p}")
        con.commit()


def proxy_record_transport(ident, *, threshold: int = 2, freeze_minutes: int = 20) -> dict:
    """代理**自身不可用**（连不上 / CONNECT 被拒 / 超时）的生命周期。

    与业务 409 同构，但写的是**健康维度**（旧列），因为它与用途无关：
    连续 N 次 → 冻结 F 分钟（pick 会跳过它，于是"切换代理"能真正生效）；
    解冻后再 1 次 → 永久不可用（需人工重置）。全部代理都不可用时，
    调用方应停下并显示「代理不可用」。
    """
    thr = max(1, int(threshold or 2))
    mins = max(1, int(freeze_minutes or 20))
    now = time.time()
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return {"ok": False, "action": "", "frozen_until": None}
        row = con.execute("SELECT * FROM proxy_pool WHERE id=?", (pid,)).fetchone()
        consec = (row["consecutive_409"] or 0) + 1
        frozen_until = row["frozen_until"]
        if row["permanent_dead"]:
            action = "permanent"
            con.execute(
                "UPDATE proxy_pool SET consecutive_409=?, total_409=total_409+1, "
                "last_409_at=?, updated_at=? WHERE id=?",
                (consec, now, now, pid),
            )
        elif row["frozen_once"]:
            action = "permanent"
            con.execute(
                "UPDATE proxy_pool SET consecutive_409=?, total_409=total_409+1, "
                "permanent_dead=1, last_409_at=?, updated_at=? WHERE id=?",
                (consec, now, now, pid),
            )
        elif consec >= thr:
            action = "freeze"
            frozen_until = now + mins * 60
            con.execute(
                "UPDATE proxy_pool SET consecutive_409=0, total_409=total_409+1, "
                "frozen_once=1, frozen_until=?, freeze_count=freeze_count+1, "
                "last_409_at=?, updated_at=? WHERE id=?",
                (frozen_until, now, now, pid),
            )
        else:
            action = "count"
            con.execute(
                "UPDATE proxy_pool SET consecutive_409=?, total_409=total_409+1, "
                "last_409_at=?, updated_at=? WHERE id=?",
                (consec, now, now, pid),
            )
        _proxy_log(con, pid, "transport", f"consecutive={consec}")
        if action in ("freeze", "permanent"):
            _proxy_log(con, pid, f"unavailable:{action}")
        con.commit()
        return {"ok": True, "action": action, "frozen_until": frozen_until}


def proxy_record_fail(ident, error: str = "", purpose: str = "codex") -> None:
    """普通失败：fail_count 仍按代理聚合（不分维度），事件里带上用途。"""
    p = normalize_proxy_purpose(purpose)
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return
        con.execute(
            "UPDATE proxy_pool SET fail_count=fail_count+1, last_error=?, updated_at=? WHERE id=?",
            ((error or "")[:500], time.time(), pid),
        )
        _proxy_log(con, pid, f"fail:{p}", error)
        con.commit()


def proxy_record_409(ident, *, purpose: str = "codex", threshold: int = 2,
                     freeze_minutes: int = 20) -> dict:
    """某个用途维度上的 409 生命周期。

      连续 N 次 409（N=threshold，默认 2）→ 冻结 freeze_minutes 分钟（默认 20）；
      解冻后（该维度 frozen_once 已置位）再出现 1 次 409 → **该维度**永久不可用；
      同维度 record_success 会清零连续计数。

    两个维度各自独立：注册锁冻结不影响激活锁可用，反之亦然。
    返回 {"ok", "action": count|freeze|permanent, "frozen_until", "purpose"}。
    """
    p = normalize_proxy_purpose(purpose)
    thr = max(1, int(threshold or 2))
    mins = max(1, int(freeze_minutes or 20))
    now = time.time()
    with _lock:
        con = _conn()
        pid = _proxy_id(con, ident)
        if pid is None:
            return {"ok": False, "action": "", "frozen_until": None, "purpose": p}
        row = con.execute("SELECT * FROM proxy_pool WHERE id=?", (pid,)).fetchone()
        consec = (row[f"consecutive_409_{p}"] or 0) + 1
        frozen_until = row[f"frozen_until_{p}"]
        if row[f"permanent_dead_{p}"]:
            action = "permanent"
            con.execute(
                f"UPDATE proxy_pool SET consecutive_409_{p}=?, total_409_{p}=total_409_{p}+1, "
                f"last_409_at_{p}=?, updated_at=? WHERE id=?",
                (consec, now, now, pid),
            )
        elif row[f"frozen_once_{p}"]:
            action = "permanent"
            con.execute(
                f"UPDATE proxy_pool SET consecutive_409_{p}=?, total_409_{p}=total_409_{p}+1, "
                f"permanent_dead_{p}=1, last_409_at_{p}=?, updated_at=? WHERE id=?",
                (consec, now, now, pid),
            )
        elif consec >= thr:
            action = "freeze"
            frozen_until = now + mins * 60
            con.execute(
                f"UPDATE proxy_pool SET consecutive_409_{p}=0, total_409_{p}=total_409_{p}+1, "
                f"frozen_once_{p}=1, frozen_until_{p}=?, freeze_count_{p}=freeze_count_{p}+1, "
                f"last_409_at_{p}=?, updated_at=? WHERE id=?",
                (frozen_until, now, now, pid),
            )
        else:
            action = "count"
            con.execute(
                f"UPDATE proxy_pool SET consecutive_409_{p}=?, total_409_{p}=total_409_{p}+1, "
                f"last_409_at_{p}=?, updated_at=? WHERE id=?",
                (consec, now, now, pid),
            )
        _proxy_log(con, pid, f"409:{p}", f"consecutive={consec}")
        if action in ("freeze", "permanent"):
            _proxy_log(con, pid, f"{action}:{p}")
        con.commit()
        return {"ok": True, "action": action, "frozen_until": frozen_until, "purpose": p}


def proxy_list_with_state() -> list[dict]:
    """给前端的列表：附带 status / 冻结剩余秒数。"""
    now = time.time()
    con = _conn()
    rows = [
        dict(r)
        for r in con.execute(
            "SELECT * FROM proxy_pool "
            "ORDER BY permanent_dead_register ASC, permanent_dead_codex ASC, "
            "enabled DESC, use_count ASC, id ASC"
        ).fetchall()
    ]
    for r in rows:
        # 每个用途维度各自一份状态（注册锁 / 激活锁互不影响）
        locks = {}
        for p in PROXY_PURPOSES:
            frozen_until = r.get(f"frozen_until_{p}") or 0
            if r.get(f"permanent_dead_{p}"):
                state, remaining = "dead", 0
            elif not r.get("enabled"):
                state, remaining = "disabled", 0
            elif frozen_until > now:
                state, remaining = "frozen", int(frozen_until - now)
            else:
                state, remaining = "ready", 0
            locks[p] = {
                "status": state,
                "frozenRemaining": remaining,
                "consecutive409": r.get(f"consecutive_409_{p}") or 0,
                "total409": r.get(f"total_409_{p}") or 0,
                "frozenOnce": bool(r.get(f"frozen_once_{p}")),
                "permanentDead": bool(r.get(f"permanent_dead_{p}")),
                "freezeCount": r.get(f"freeze_count_{p}") or 0,
            }
        r["locks"] = locks
        # 健康维度（代理自身：连不上/隧道被拒）—— 与用途无关，两种用途共用。
        health_frozen = r.get("frozen_until") or 0
        if r.get("permanent_dead"):
            health_state, health_remaining = "dead", 0
        elif not r.get("enabled"):
            health_state, health_remaining = "disabled", 0
        elif health_frozen > now:
            health_state, health_remaining = "frozen", int(health_frozen - now)
        else:
            health_state, health_remaining = "ready", 0
        r["health"] = {
            "status": health_state,
            "frozenRemaining": health_remaining,
            "consecutiveFailures": r.get("consecutive_409") or 0,
            "frozenOnce": bool(r.get("frozen_once")),
            "permanentDead": bool(r.get("permanent_dead")),
            "lastError": r.get("last_error") or "",
        }
        # 旧字段保留给未升级的消费者：健康维度 + 两个用途维度**都**不可用才算不可用。
        states = [health_state] + [item["status"] for item in locks.values()]
        if states and all(state == "dead" for state in states):
            r["status"], r["frozen_remaining"] = "dead", 0
        elif not r.get("enabled"):
            r["status"], r["frozen_remaining"] = "disabled", 0
        elif any(state == "ready" for state in states):
            r["status"], r["frozen_remaining"] = "ready", 0
        else:
            waiting = [item["frozenRemaining"] for item in locks.values() if item["status"] == "frozen"]
            r["status"], r["frozen_remaining"] = "frozen", min(waiting) if waiting else 0
    return rows


def proxy_get(ident) -> Optional[dict]:
    con = _conn()
    pid = _proxy_id(con, ident)
    if pid is None:
        return None
    row = con.execute("SELECT * FROM proxy_pool WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def proxy_list_events(proxy_id: Optional[int] = None, limit: int = 200) -> list[dict]:
    con = _conn()
    if proxy_id:
        cur = con.execute(
            "SELECT * FROM proxy_events WHERE proxy_id=? ORDER BY id DESC LIMIT ?",
            (int(proxy_id), limit),
        )
    else:
        cur = con.execute(
            "SELECT * FROM proxy_events ORDER BY id DESC LIMIT ?", (limit,)
        )
    return [dict(r) for r in cur.fetchall()]


def proxy_summary() -> dict:
    """仪表盘用：可用 / 冻结中 / 永久不可用 / 已禁用 / 总数，外加两个用途维度。"""
    out = {"ready": 0, "frozen": 0, "dead": 0, "disabled": 0, "total": 0}
    locks = {p: {"ready": 0, "frozen": 0, "dead": 0, "disabled": 0} for p in PROXY_PURPOSES}
    for r in proxy_list_with_state():
        out["total"] += 1
        out[r["status"]] = out.get(r["status"], 0) + 1
        for p, item in (r.get("locks") or {}).items():
            locks.setdefault(p, {"ready": 0, "frozen": 0, "dead": 0, "disabled": 0})
            locks[p][item["status"]] = locks[p].get(item["status"], 0) + 1
    out["available"] = out["ready"]
    out["locks"] = locks
    health = {"ready": 0, "frozen": 0, "dead": 0, "disabled": 0}
    for r in proxy_list_with_state():
        state = (r.get("health") or {}).get("status") or "ready"
        health[state] = health.get(state, 0) + 1
    out["health"] = health
    return out


# ──────────────────────── 邮箱分配（CDK 兑换用） ────────────────────────


def count_available_mailboxes(kind: str = "") -> int:
    """可兑换账号数（status='registered' 且未绑任何卡密）。

    导入进来但还没注册的 available 邮箱**不算**可兑换 —— 它们没有密码/2FA，
    交给用户只会让执行阶段必失败。函数名沿用旧称，语义已随流程拆分改为「账号」。
    """
    con = _conn()
    sql = (
        "SELECT COUNT(*) AS n FROM outlook_accounts "
        "WHERE status='registered' AND coalesce(cdk_code,'')=''"
    )
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(kind.strip().lower())
    return con.execute(sql, args).fetchone()["n"]


def list_available_mailboxes(kind: str = "", limit: int = 50) -> list[dict]:
    con = _conn()
    sql = (
        "SELECT * FROM outlook_accounts "
        "WHERE status='registered' AND coalesce(cdk_code,'')=''"
    )
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(kind.strip().lower())
    sql += " ORDER BY imported_at ASC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def claim_cdk_mailbox(code: str, kind: str, *, pooled: bool, email: str = "") -> Optional[dict]:
    """把一个**未使用**邮箱原子绑定到卡密。

    pooled=True  → 从号池取一个 available 且未绑 CDK 的行：**优先已接码的号**
                   （注册阶段验过手机的号，兑换时不需要再走 add-phone、也不需要
                   再租号等码），同一档内保持随机（原先就是随机取）。
    pooled=False → 插入一行自建地址（CF 临时邮箱这类现场生成的）。
    """
    code = (code or "").strip()
    if not code:
        return None
    k = (kind or "").strip().lower()
    now = time.time()
    with _lock:
        con = _conn()
        if pooled:
            sql = (
                "SELECT * FROM outlook_accounts WHERE status='registered' "
                "AND coalesce(cdk_code,'')=''"
            )
            args: list = []
            if k:
                sql += " AND kind=?"
                args.append(k)
            # 优先 phone_bound_ok=1（已接码）：这类号兑换时免 add-phone。
            # 同一档内仍随机，保持"随机取号"的原意；已接码的取完了自然回落到未接码。
            sql += (" ORDER BY CASE WHEN phone_bound_ok=1 THEN 0 ELSE 1 END,"
                    " RANDOM() LIMIT 1")
            row = con.execute(sql, args).fetchone()
            if not row:
                return None
            rc = con.execute(
                "UPDATE outlook_accounts SET cdk_code=?, cdk_bound_at=?, status='in_use' "
                "WHERE email=? AND coalesce(cdk_code,'')='' AND status='registered'",
                (code, now, row["email"]),
            )
            con.commit()
            if rc.rowcount != 1:
                return None  # 被并发抢走
            out = dict(row)
            out["cdk_code"] = code
            return out
        addr = (email or "").strip().lower()
        if not addr:
            return None
        try:
            con.execute(
                "INSERT INTO outlook_accounts"
                "(email, password, client_id, refresh_token, relay_url, kind, status, "
                " imported_at, cdk_code, cdk_bound_at) "
                "VALUES (?, '', '', '', '', ?, 'in_use', ?, ?, ?)",
                (addr, k, now, code, now),
            )
            con.commit()
        except sqlite3.IntegrityError:
            return None
        return {"email": addr, "kind": k, "cdk_code": code}


def release_cdk_mailbox(code: str) -> int:
    """把卡密占用的邮箱解绑。

    已经注册成功（status='done'）的邮箱保持 done —— 它被用过了，不该再流入池子；
    还没跑成功的 in_use 行回到它原来的可兑换状态：有注册凭证（registered 表里有
    这行）就回 'registered' 继续给别的卡密用，老数据里没凭证的才回 'available'
    —— 否则会凭空出现一个"可兑换但没有密码"的账号，用户兑到就必失败。
    """
    code = (code or "").strip()
    if not code:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET cdk_code=NULL, cdk_bound_at=NULL, "
            "status=CASE WHEN status='in_use' THEN "
            "  (CASE WHEN EXISTS(SELECT 1 FROM registered r WHERE r.email=outlook_accounts.email) "
            "        THEN 'registered' ELSE 'available' END) "
            "ELSE status END "
            "WHERE cdk_code=?",
            (code,),
        )
        con.commit()
        return rc.rowcount


def set_account_proxy(email: str, proxy: str) -> None:
    """记录某个邮箱注册时用的代理。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET proxy=? WHERE lower(email)=lower(?)",
            ((proxy or "").strip(), (email or "").strip()),
        )
        con.commit()


# ──────────────────────── 卡密 ────────────────────────

_CDK_FIELDS = (
    "batch", "note", "status", "email", "mail_kind", "inviter_email",
    "inviter_subject", "invite_link", "invite_received_at", "invite_sent_at",
    "invite_processed_at", "attempts", "fail_count", "proxy_used", "redeemed_at",
    "finished_at", "result_json", "error",
)


def create_cdk_codes(codes: list[str], batch: str = "", note: str = "") -> dict:
    now = time.time()
    inserted = skipped = 0
    with _lock:
        con = _conn()
        for raw in codes or []:
            code = str(raw or "").strip()
            if not code:
                continue
            try:
                con.execute(
                    "INSERT INTO cdk_codes(code, batch, note, status, created_at) "
                    "VALUES (?, ?, ?, 'unused', ?)",
                    (code, batch or "", note or "", now),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
        con.commit()
    return {"inserted": inserted, "skipped": skipped}


def _cdk_where(status: str = "", batch: str = "", search: str = "") -> tuple[str, list]:
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if batch:
        where.append("batch=?")
        args.append(batch)
    if search:
        where.append("(code LIKE ? OR coalesce(email,'') LIKE ? OR coalesce(inviter_email,'') LIKE ?)")
        like = f"%{search}%"
        args += [like, like, like]
    return (" WHERE " + " AND ".join(where)) if where else "", args


def _search_cdk_rows(status='',batch='',search=''):
    con=_conn()
    where,args=_cdk_where(status,batch,'')
    rows=[dict(r) for r in con.execute(f"SELECT * FROM cdk_codes{where} ORDER BY created_at DESC, code ASC",args).fetchall()]
    if search:
        import re
        # Preserve SQLite LIKE %search% including wildcard behavior after decrypting.
        pattern='.*'+''.join('.*' if c=='%' else '.' if c=='_' else re.escape(c) for c in search)+'.*'
        rows=[r for r in rows if any(re.fullmatch(pattern,str(r.get(k) or ''),re.I|re.S) for k in ('code','email','inviter_email'))]
    return rows

def count_cdk_codes(status='',batch='',search=''):
    return len(_search_cdk_rows(status,batch,search))

def list_cdk_codes(status='',batch='',search='',limit=50,offset=0):
    return _search_cdk_rows(status,batch,search)[int(offset):int(offset)+int(limit)]


def get_cdk_code(code: str) -> Optional[dict]:
    con = _conn()
    row = con.execute(
        "SELECT * FROM cdk_codes WHERE code=?", ((code or "").strip(),)
    ).fetchone()
    return dict(row) if row else None


def get_cdk_codes(codes: list[str]) -> list[dict]:
    cleaned = [str(c or "").strip() for c in (codes or []) if str(c or "").strip()]
    if not cleaned:
        return []
    con = _conn()
    out = []
    for i in range(0, len(cleaned), 500):
        part = cleaned[i:i + 500]
        sql = f"SELECT * FROM cdk_codes WHERE code IN ({','.join('?' * len(part))})"
        out += [dict(r) for r in con.execute(sql, part).fetchall()]
    return out


def set_cdk_status(code: str, status: str, **fields) -> bool:
    code = (code or "").strip()
    if not code:
        return False
    sets, vals = ["status=?"], [status]
    for k, v in (fields or {}).items():
        if k not in _CDK_FIELDS:
            continue
        sets.append(f"{k}=?")
        vals.append(v)
    with _lock:
        con = _conn()
        vals.append(code)
        rc = con.execute(f"UPDATE cdk_codes SET {', '.join(sets)} WHERE code=?", vals)
        con.commit()
        return rc.rowcount > 0


def update_cdk_fields(code: str, **fields) -> bool:
    code = (code or "").strip()
    if not code:
        return False
    sets, vals = [], []
    for k, v in (fields or {}).items():
        if k not in _CDK_FIELDS:
            continue
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    with _lock:
        con = _conn()
        vals.append(code)
        rc = con.execute(f"UPDATE cdk_codes SET {', '.join(sets)} WHERE code=?", vals)
        con.commit()
        return rc.rowcount > 0


def mark_invite_sent(code: str) -> bool:
    return update_cdk_fields(code, invite_sent_at=time.time())


def inc_cdk_fail(code: str) -> int:
    """失败计数 +1，返回新的 fail_count。注意：**绝不自动作废**。"""
    code = (code or "").strip()
    if not code:
        return 0
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE cdk_codes SET fail_count=fail_count+1, attempts=attempts+1 WHERE code=?",
            (code,),
        )
        con.commit()
        row = con.execute("SELECT fail_count FROM cdk_codes WHERE code=?", (code,)).fetchone()
        return int(row["fail_count"]) if row else 0


def reset_cdk(code: str) -> bool:
    """管理员重置：清失败计数与错误，状态回到 bound（保留邮箱绑定）。

    清除前先把旧的 error / finished_at 落成一条 cdk 事件：重置会就地抹掉失败
    原因，没有这条事件就再也查不出"上次为什么失败、什么时候失败的"。
    """
    code = (code or "").strip()
    if not code:
        return False
    with _lock:
        con = _conn()
        previous = con.execute(
            "SELECT error, finished_at FROM cdk_codes WHERE code=?", (code,)
        ).fetchone()
        rc = con.execute(
            "UPDATE cdk_codes SET status=CASE WHEN coalesce(email,'')='' THEN 'unused' ELSE 'bound' END, "
            "fail_count=0, attempts=0, error=NULL, finished_at=NULL WHERE code=?",
            (code,),
        )
        if rc.rowcount > 0 and previous is not None:
            old_error = previous["error"]
            old_finished = previous["finished_at"]
            if old_error or old_finished:
                # 事件与重置同一次提交，避免留下"清了错误却没有事件"的记录。
                con.execute(
                    "INSERT INTO cdk_events(code, job_id, event, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                    (code, "", "reset",
                     json.dumps({"previousError": old_error, "previousFinishedAt": old_finished},
                                ensure_ascii=False)[:2000],
                     time.time()),
                )
        con.commit()
        return rc.rowcount > 0


def void_cdk(code: str, reason: str = "") -> dict:
    """管理员手动作废（唯一的作废途径）：作废并释放邮箱。"""
    code = (code or "").strip()
    if not code:
        return {"ok": False}
    ok = set_cdk_status(code, "void", error=reason or "管理员作废", finished_at=time.time())
    released = release_cdk_mailbox(code)
    return {"ok": ok, "released": released}


def delete_cdk_codes(codes: list[str]) -> int:
    cleaned = [str(c or "").strip() for c in (codes or []) if str(c or "").strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        n = 0
        for i in range(0, len(cleaned), 500):
            part = cleaned[i:i + 500]
            rc = con.execute(
                f"DELETE FROM cdk_codes WHERE code IN ({','.join('?' * len(part))})", part
            )
            n += rc.rowcount
        con.commit()
        return n


def cdk_stats() -> dict:
    con = _conn()
    out = {
        "unused": 0, "bound": 0, "queued": 0, "running": 0,
        "done": 0, "failed": 0, "void": 0, "total": 0,
    }
    for r in con.execute("SELECT status, COUNT(*) AS n FROM cdk_codes GROUP BY status").fetchall():
        st = r["status"] or "unused"
        out[st] = r["n"]
        out["total"] += r["n"]
    out["mail_pool"] = count_available_mailboxes()
    out["jobs_running"] = con.execute(
        "SELECT COUNT(*) AS n FROM cdk_jobs WHERE status='running'"
    ).fetchone()["n"]
    out["jobs_queued"] = con.execute(
        "SELECT COUNT(*) AS n FROM cdk_jobs WHERE status='queued'"
    ).fetchone()["n"]
    return out


def list_cdk_batches() -> list[str]:
    con = _conn()
    return [
        r["batch"]
        for r in con.execute(
            "SELECT DISTINCT batch FROM cdk_codes WHERE coalesce(batch,'')<>'' ORDER BY batch DESC"
        ).fetchall()
    ]


# ──────────────────────── 卡密作业 / 事件 ────────────────────────

_JOB_FIELDS = ("email", "attempt", "status", "stage", "progress", "proxy", "error",
               "started_at", "finished_at")


def create_cdk_job(job_id: str, code: str, email: str = "", attempt: int = 1,
                   proxy: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT OR REPLACE INTO cdk_jobs"
            "(job_id, code, email, attempt, status, stage, progress, proxy, created_at) "
            "VALUES (?, ?, ?, ?, 'queued', 'queued', 0, ?, ?)",
            (job_id, code, email, int(attempt), proxy or "", time.time()),
        )
        con.commit()


def claim_cdk_job(job_id: str, started_at: float | None = None) -> bool:
    """原子认领一个排队作业：只有仍是 queued 的那一个调用者会赢。

    这是 F02 的修复点。旧流程是 ``set_cdk_job(status="running")`` 无条件 UPDATE，
    于是管理员在 worker 取出队列项之前调用 ``stop_job`` 把作业写成 failed 之后，
    worker 仍然会把它改回 running 并真的去执行邀请（继续消耗上游资源、且此时邮箱
    复核已被 stop_job 按「尚未开始」自动解除，没有任何东西再拦它）。

    把「判定仍是 queued」和「写成 running」放进同一条带条件的 UPDATE，用 rowcount
    作为唯一判据，停止与认领之间的竞态就只剩两种安全结果：停止先到 → 认领失败丢弃；
    认领先到 → 停止看到 running 并保留复核。
    """
    job_id = str(job_id or "").strip()
    if not job_id:
        return False
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE cdk_jobs SET status='running', started_at=? WHERE job_id=? AND status='queued'",
            (time.time() if started_at is None else started_at, job_id),
        )
        con.commit()
        return rc.rowcount == 1


def set_cdk_job(job_id: str, **fields) -> None:
    sets, vals = [], []
    for k, v in (fields or {}).items():
        if k not in _JOB_FIELDS:
            continue
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    with _lock:
        con = _conn()
        vals.append(job_id)
        con.execute(f"UPDATE cdk_jobs SET {', '.join(sets)} WHERE job_id=?", vals)
        con.commit()


def list_cdk_jobs(code: str = "", limit: int = 100) -> list[dict]:
    con = _conn()
    if code:
        cur = con.execute(
            "SELECT * FROM cdk_jobs WHERE code=? ORDER BY created_at DESC LIMIT ?",
            (code.strip(), int(limit)),
        )
    else:
        cur = con.execute(
            "SELECT * FROM cdk_jobs ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )
    return [dict(r) for r in cur.fetchall()]


def list_recent_cdk_events(limit: int = 60) -> list[dict]:
    """跨全部卡密的最近事件（新的在前）—— 后台实时监控用。"""
    con = _conn()
    cur = con.execute(
        "SELECT * FROM cdk_events ORDER BY id DESC LIMIT ?", (int(limit),)
    )
    return [dict(r) for r in cur.fetchall()]


def list_cdk_jobs_filtered(statuses: Optional[list] = None, limit: int = 50) -> list[dict]:
    """按状态取作业（新的在前）。statuses 为空则取全部。"""
    con = _conn()
    if statuses:
        marks = ",".join("?" for _ in statuses)
        cur = con.execute(
            f"SELECT * FROM cdk_jobs WHERE status IN ({marks}) "
            f"ORDER BY (CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END), "
            f"created_at DESC LIMIT ?",
            (*[str(x) for x in statuses], int(limit)),
        )
    else:
        cur = con.execute(
            "SELECT * FROM cdk_jobs ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )
    return [dict(r) for r in cur.fetchall()]


def get_cdk_job(job_id: str) -> Optional[dict]:
    con = _conn()
    row = con.execute("SELECT * FROM cdk_jobs WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def add_cdk_event(code: str, job_id: str, event: str, detail: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO cdk_events(code, job_id, event, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            ((code or "").strip(), job_id or "", event or "", (detail or "")[:2000], time.time()),
        )
        con.commit()


def list_cdk_events(code: str = "", limit: int = 200, offset: int = 0) -> list[dict]:
    con = _conn()
    if code:
        cur = con.execute(
            "SELECT * FROM cdk_events WHERE code=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (code.strip(), int(limit), int(offset)),
        )
    else:
        cur = con.execute(
            "SELECT * FROM cdk_events ORDER BY id DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        )
    return [dict(r) for r in cur.fetchall()]


def recover_stuck_cdk() -> int:
    """进程重启后把卡死的 queued/running 作业收尾（可重试，不自动重放）。"""
    now = time.time()
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE cdk_jobs SET status='failed', error='服务重启，请重试', finished_at=? "
            "WHERE status IN ('queued','running')",
            (now,),
        )
        con.execute(
            "UPDATE cdk_codes SET status='failed', error='服务重启，请重试' "
            "WHERE status IN ('queued','running')"
        )
        con.commit()
        return rc.rowcount


# ──────────────────────── 接码价格 / 成功率 ────────────────────────


def sms_price_upsert(platform: str, service: str, rows: list[dict],
                     ok: bool = True, error: str = "") -> int:
    """整批替换某平台+服务的价格快照（本次没出现的国家会被删掉，避免陈旧数据）。"""
    now = time.time()
    with _lock:
        con = _conn()
        if ok:
            con.execute(
                "DELETE FROM sms_prices WHERE platform=? AND service=?",
                (platform, service),
            )
            for r in rows or []:
                con.execute(
                    "INSERT OR REPLACE INTO sms_prices"
                    "(platform, service, country, price, count, physical_count, min_price, "
                    " raw_json, updated_at, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)",
                    (
                        platform, service, str(r.get("country") or ""),
                        r.get("price"), r.get("count"), r.get("physical_count"),
                        r.get("min_price"), (r.get("raw") or "")[:4000], now,
                    ),
                )
        con.execute(
            "INSERT INTO sms_price_state(platform, last_poll_at, last_ok_at, last_error) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(platform) DO UPDATE SET last_poll_at=excluded.last_poll_at, "
            "last_ok_at=COALESCE(excluded.last_ok_at, sms_price_state.last_ok_at), "
            "last_error=excluded.last_error",
            (platform, now, now if ok else None, (error or "")[:500] if not ok else None),
        )
        con.commit()
    return len(rows or [])


def sms_price_list(platform: str = "", service: str = "") -> list[dict]:
    con = _conn()
    sql = "SELECT * FROM sms_prices"
    where, args = [], []
    if platform:
        where.append("platform=?")
        args.append(platform)
    if service:
        where.append("service=?")
        args.append(service)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY price ASC, count DESC"
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def sms_price_state_list() -> list[dict]:
    con = _conn()
    return [dict(r) for r in con.execute("SELECT * FROM sms_price_state").fetchall()]


def sms_price_state_set(platform: str, **fields) -> None:
    allowed = ("balance", "best_country", "best_price", "best_score",
               "best_success_rate", "best_at", "last_error")
    extra = [(k, v) for k, v in (fields or {}).items() if k in allowed]
    with _lock:
        con = _conn()
        con.execute(
            "INSERT OR IGNORE INTO sms_price_state(platform) VALUES (?)", (platform,)
        )
        if extra:
            sets = ", ".join(f"{k}=?" for k, _ in extra)
            vals = [v for _, v in extra] + [platform]
            con.execute(
                f"UPDATE sms_price_state SET {sets} WHERE platform=?", vals
            )
        con.commit()


def sms_stats_bump(platform: str, service: str, country: str, event: str) -> None:
    """成功率统计：一次租号 = attempt，最终 ok/fail 各一次。"""
    if not platform or not country:
        return
    now = time.time()
    with _lock:
        con = _conn()
        con.execute(
            "INSERT OR IGNORE INTO sms_stats(platform, service, country, attempts, ok, fail) "
            "VALUES (?, ?, ?, 0, 0, 0)",
            (platform, service or "", str(country)),
        )
        if event == "attempt":
            con.execute(
                "UPDATE sms_stats SET attempts=attempts+1, last_used_at=? "
                "WHERE platform=? AND service=? AND country=?",
                (now, platform, service or "", str(country)),
            )
        elif event == "ok":
            con.execute(
                "UPDATE sms_stats SET ok=ok+1, last_used_at=? "
                "WHERE platform=? AND service=? AND country=?",
                (now, platform, service or "", str(country)),
            )
        elif event == "fail":
            con.execute(
                "UPDATE sms_stats SET fail=fail+1, last_used_at=? "
                "WHERE platform=? AND service=? AND country=?",
                (now, platform, service or "", str(country)),
            )
        con.commit()


def sms_stats_list() -> list[dict]:
    con = _conn()
    return [dict(r) for r in con.execute("SELECT * FROM sms_stats").fetchall()]


# ──────────────────────── Codex 授权凭证 ────────────────────────


def save_codex_auth(email: str, **fields) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    allowed = ("account_id", "access_token", "refresh_token", "id_token", "auth_json",
               "web_access_token", "session_token", "last_refresh",
               "invite_processed_at", "invite_link", "cli_completed_at",
               "cli_completed_account_id")
    cols = ["email"] + [k for k in allowed if k in (fields or {})]
    vals = [email] + [fields[k] for k in allowed if k in (fields or {})]
    cols.append("updated_at")
    vals.append(time.time())
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "email")
    with _lock:
        con = _conn()
        con.execute(
            f"INSERT INTO codex_auth({', '.join(cols)}) VALUES ({','.join('?' * len(vals))}) "
            f"ON CONFLICT(email) DO UPDATE SET {updates}",
            vals,
        )
        con.commit()


def get_codex_auth(email: str) -> Optional[dict]:
    con = _conn()
    row = con.execute(
        "SELECT * FROM codex_auth WHERE email=?", ((email or "").strip().lower(),)
    ).fetchone()
    return dict(row) if row else None




# Every operation shares the explicit MySQL transaction context; no import-time DDL.
from smsinvit.invitation.mysql import transactional as _transactional
for _name, _fn in tuple(globals().items()):
    if callable(_fn) and getattr(_fn,"__module__",None)==__name__ and not _name.startswith("_") and _name!="init_db":
        globals()[_name]=_transactional(_fn)

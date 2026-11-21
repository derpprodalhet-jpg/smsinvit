"""codex_invit 管理后台：FastAPI 主程序（路由 + SSE 流式日志）。

启动:
    python start_admin.py            # 默认 127.0.0.1:5093
"""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import logging
import secrets
import shutil
import string
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import cdk, cdk_formats, codex_auth, db, export_formats, mail_alloc, proxy_pool, registrar, sms_prices  # noqa: E402
from .exporter import _decode_jwt_payload, _get_auth  # noqa: E402
from mail_providers import (  # noqa: E402
    ImportValidationError,
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_pooled_providers,
    list_providers,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="codex_invit 管理后台", docs_url=None, redoc_url=None)


# ──────────────────────── Pydantic 模型 ────────────────────────


class ImportReq(BaseModel):
    text: str = Field(..., description="每行一个号，格式由 kind 决定")
    kind: str = Field(
        "",
        description="邮箱来源（outlook / ...）。留空则按段数猜，"
                    "但 Outlook 和 Gmail 都是 4 段，猜不出来，建议前端必填",
    )


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""
    otp_timeout: int = 10
    allow_existing_login: bool = True
    # 注册成功后自动绑定 TOTP 2FA。前端两个页面都**默认开**（主人要求每个号都绑）。
    # 这里的 default 保持 False —— 它只在「调用方没传这个字段」时生效，是给旧前端
    # 缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不替调用方做一个不可逆的决定。
    # 真实默认值由前端 form store 的 want2fa / autoWant2fa 决定。
    want_2fa: bool = False
    # 账号工厂模式：注册到 2FA 就停住入池（status='registered'），不做 Codex 授权、
    # 不导出面板。池的消费方（CDK 兑换 + 启动执行）见 smsinvit.invitation.pool。
    pool_only: bool = False


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
def api_import(req: ImportReq):
    """批量导入号池。**有一行不合法就整批拒绝**，一个都不写库。

    非法时返回 422，body 里带每一行的行号和原因，前端直接展示即可：

        {"ok": false, "message": "...", "errors": [{"line": 3, "error": "..."}]}
    """
    try:
        result = db.import_accounts(req.text, kind=req.kind)
    except ImportValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "message": str(e), "errors": e.errors},
        )
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result, "stats": db.stats()}


@app.get("/api/accounts")
def api_accounts(status: str = "", limit: int = 50, offset: int = 0, kind: str = ""):
    items = db.list_accounts(status=status, limit=limit, offset=offset, kind=kind)
    total = db.count_accounts(status=status, kind=kind)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "by_kind": db.stats_by_kind(),
    }


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


# ──────────────────────── 代理连通性测试 ────────────────────────


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="要测试的代理列表")
    timeout: int = Field(12, description="每个代理检测总时间预算（秒）")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="测试目标 URL（默认返回出口 IP）")


@app.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """检测结果区分代理可达、目标拒绝与安全策略；每条代理共享总时间预算。"""
    from .proxy_test import DEFAULT_TARGET, test_proxies

    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies 不能为空")
    return test_proxies(proxies, target=(req.test_url or DEFAULT_TARGET).strip(),
                        timeout=max(1, min(int(req.timeout or 12), 60)))


@app.post("/api/register")
def api_register(req: RegisterReq):
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    # 「加入待兑换账号池」走**补号来源**（pool_mail_source），和账号工厂保持一致；
    # 普通单次注册仍走兑换侧的 mail_source。两个来源各配各的。
    if req.pool_only:
        mail_source = str(db.get_setting("pool_mail_source", "") or "").strip().lower() or db.get_setting("mail_source", "outlook")
    else:
        mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 要不要 claim 号池，由 provider 自己声明的 pooled 决定 ——
    # 原来写死 `mail_source == "cf_temp"`，加一种非池化邮箱就得改这里。
    if not provider_cls.pooled:
        # 非池化：地址由 provider 现造，用占位 account 走完后面的流程
        import time as _t
        account = {
            "email": f"{mail_source}_placeholder_{int(_t.time())}@placeholder.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "relay_url": "",
            "kind": mail_source,
        }
    elif req.email:
        account = db.claim_account(req.email)
        if not account:
            raise HTTPException(400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)")
        if (account.get("kind") or "outlook") != mail_source:
            # 号池里混放多种邮箱，点名的号必须和当前来源一致，
            # 否则会拿 Outlook 的凭证去初始化 Gmail provider
            db.release_unused(account["email"])
            raise HTTPException(
                400,
                f"{req.email} 是 {account.get('kind')} 的号，"
                f"当前邮箱来源是 {mail_source}，请先切换来源",
            )
    else:
        account = db.claim_next(kind=mail_source)
        if not account:
            raise HTTPException(
                400,
                f"号池里没有 available 的 {provider_cls.display_name} 账号；请先批量导入",
            )

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": req.proxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
        "want_2fa": req.want_2fa,
        "pool_only": bool(req.pool_only),
        "mail_source": mail_source,
    }
    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """SSE 实时推送日志 + 事件。"""
    q = registrar.get_run_queue(run_id)
    if q is None:
        raise HTTPException(404, "run_id not found or finished")

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 从队列取消息（用 run_in_executor 避免阻塞 event loop）
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is None:
                    # sentinel: 任务结束
                    yield "event: end\ndata: {}\n\n"
                    break
                if msg.startswith("__EVENT__:"):
                    yield f"event: status\ndata: {msg[len('__EVENT__:'):]}\n\n"
                else:
                    yield f"event: log\ndata: {json.dumps({'line': msg}, ensure_ascii=False)}\n\n"
        finally:
            registrar.remove_run_queue(run_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 避免 nginx 缓冲
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=60)
    except Exception:
        return ""  # 心跳：返空串让 SSE 检查 disconnect


@app.get("/api/runs/{run_id}/log")
def api_run_log(run_id: str, tail_kb: int = 512):
    """取某条 run 的**完整日志**（给运行记录页的「详情」按钮用）。

    以前页面上只有一句 error —— 主人想看为什么失败，只能自己去 webui/logs 翻文件。
    `tail_kb` 限制返回大小（默认 512KB，够看全过程；超大时返回尾部并说明截断）。
    """
    row = None
    for r in db.list_runs(limit=2000):
        if r.get("run_id") == run_id:
            row = r
            break
    if not row:
        raise HTTPException(404, f"run_id 不存在: {run_id}")
    path = Path(str(row.get("log_path") or ""))
    limit = max(1, min(int(tail_kb or 512), 8192)) * 1024
    text, truncated = "", False
    try:
        if path.exists():
            size = path.stat().st_size
            with path.open("rb") as f:
                if size > limit:
                    f.seek(size - limit)
                    truncated = True
                text = f.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"读日志失败: {e}")
    return {
        "ok": True,
        "run": row,
        "log_path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "truncated": truncated,
        "text": text,
        # 顺带把该邮箱的卡密事件也带上，一眼看到"哪一步花了钱/被拒"
        "events": [e for e in db.list_recent_cdk_events(limit=400)
                   if str(e.get("code") or "") in _codes_of_email(row.get("email") or "")],
    }


def _codes_of_email(email: str) -> set:
    try:
        return {c["code"] for c in db.list_cdk_codes(limit=500)
                if (c.get("email") or "").lower() == (email or "").lower()}
    except Exception:  # noqa: BLE001
        return set()


@app.get("/api/runs")
def api_runs(limit: int = 50, status: str = "", kind: str = "", search: str = "",
             offset: int = 0):
    """运行记录。

    ⚠️ 光有 status=running 等于没说 —— 主人只看到"在跑"，不知道卡在哪一步、
    跑多久了、用的哪个代理。所以给在跑的 run 补上作业的真实阶段/进度/尝试次数，
    并把服务端时间戳一起返回，前端每 2 秒刷新一次就是实时看板。
    """
    import time as _t

    items = db.list_runs(limit=limit, status=status, kind=kind, search=search, offset=offset)
    total = db.count_runs(status=status, kind=kind, search=search)
    labels = dict(codex_auth.USER_STAGE_LABEL)
    labels.update({"queued": "正在排队", "bound": "待开始", "unused": "未兑换"})

    live: dict = {}
    # 只用于给本页记录补"当前阶段"；活动任务本就不多，取 200 足够覆盖。
    for j in db.list_cdk_jobs_filtered(statuses=["running", "queued"], limit=200):
        key = str(j.get("email") or "").lower()
        if key and key not in live:
            live[key] = j

    for it in items:
        key = str(it.get("email") or "").lower()
        j = live.get(key)
        it["elapsed"] = (
            int(_t.time() - it["started_at"]) if it.get("started_at") and it.get("status") == "running"
            else (int(it["finished_at"] - it["started_at"]) if it.get("finished_at") and it.get("started_at") else None)
        )
        # 运行记录自带的阶段：账号工厂补号没有卡密作业，只能靠这个，
        # 否则进度永远 0%，跑完直接跳 100%。
        if it.get("stage"):
            it["stage_label"] = labels.get(it["stage"], it["stage"])
            it["progress"] = codex_auth.USER_STAGE_PCT.get(it["stage"]) or 0
        if it.get("status") == "running" and j:
            # 有卡密作业时以作业为准（它还带 attempt / proxy 等更细的信息）
            it["stage"] = j.get("stage")
            it["stage_label"] = labels.get(j.get("stage") or "", j.get("stage") or "")
            it["progress"] = j.get("progress") or 0
            it["attempt"] = j.get("attempt") or 0
            it["proxy"] = j.get("proxy") or ""
            it["job_id"] = j.get("job_id")
        if it.get("status") == "done":
            it["stage"], it["stage_label"], it["progress"] = "done", labels.get("done", "完成"), 100
        elif it.get("status") == "failed" and not it.get("stage"):
            # 没有阶段可就地说明失败，别让运行记录显示成"—"
            it["stage_label"] = labels.get("failed", "失败")
    return {"ok": True, "items": items, "total": total, "now": _t.time()}


@app.get("/api/registered")
def api_registered(limit: int = 20, offset: int = 0, filter: str = "all"):
    items = db.list_registered(limit=limit, offset=offset, filter_rt=filter)
    total = db.count_registered(filter_rt=filter)
    return {"ok": True, "items": items, "total": total}


@app.get("/api/registered/{email}")
def api_registered_one(email: str):
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, "not found")
    # 带上 Codex 授权的凭证 —— 注册结果页的 refresh 列读的是 registered（网页 RT），
    # 而 Codex RT 在 codex_auth 表，页面要显示/复制它必须从这里拿。
    try:
        row["codex"] = db.get_codex_auth(email) or {}
    except Exception:  # noqa: BLE001
        row["codex"] = {}
    return {"ok": True, "data": row}


@app.delete("/api/registered/{email}")
def api_delete_registered(email: str):
    ok = db.delete_registered(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    if req.all:
        n = db.delete_all_registered()
        return {"ok": True, "deleted": n, "by": "all"}
    if req.emails:
        n = db.delete_registered_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "需要 emails 或 all=true")


# ──────────────────────── 批量导出（文本） ────────────────────────
# ⚠️ 路由顺序：
#   - formats 是 4 段路径，不会被 3 段的 GET /api/registered/{email} 吃掉；
#   - export 是 POST，而 {email} 那两条是 GET / DELETE，也不冲突。
# 要加新格式只改 webui/export_formats.py，这里和前端都不用动。


@app.get("/api/registered/export/formats")
def api_export_formats():
    """导出格式清单，前端下拉菜单据此渲染。"""
    return {"ok": True, "formats": export_formats.list_formats()}


class ExportRegisteredReq(BaseModel):
    format: str = Field(..., description="格式 id，见 GET /api/registered/export/formats")
    emails: Optional[list[str]] = Field(None, description="要导出的 email 列表")
    all: bool = Field(False, description="true = 导出全部（跨页），忽略 emails")


@app.post("/api/registered/export")
def api_export_registered(req: ExportRegisteredReq):
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")

    if req.all:
        rows = db.list_registered_full(limit=100000)
    elif req.emails:
        rows = db.list_registered_by_emails(req.emails)
    else:
        raise HTTPException(400, "需要 emails 或 all=true")

    # 不跳行：勾了几个号就几行 / 几个文件，字段为空也照样出。
    # 手动导出**不做 refresh_token 刷新、不因为缺 rt 拦截**，这是和自动推送的区别。
    base = {
        "ok": True,
        "count": len(rows),
        "filename": fmt.filename,
        "label": fmt.label,
        "mode": fmt.mode,
        "mime": fmt.mime,
    }

    if fmt.mode == "download":
        # 二进制（zip / json 文件）走 base64，前端解出来直接存盘，不弹预览
        blob = export_formats.render_bytes(rows, fmt)
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob)}

    return {**base, "text": export_formats.render_text(rows, fmt)}


# ──────────────────────── 邮箱来源配置 ────────────────────────


@app.get("/api/mail/providers")
def api_mail_providers(pooled_only: bool = False):
    """列出所有已注册的邮箱 provider 及其能力 / 配置项声明。

    前端据此渲染「邮箱来源」单选和对应的动态表单 ——
    以后加邮箱，前端一行都不用改。

        pooled_only=true  只返回能导入号池的（导入页用）
    """
    return {
        "ok": True,
        "providers": list_pooled_providers() if pooled_only else list_providers(),
        "current": db.get_setting("mail_source", "outlook"),
    }


@app.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    """字段不再写死。

    mail_source 之外的配置项由各 provider 的 config_fields 声明，
    前端原样回传，db.save_mail_config 按声明逐项存 ——
    加 provider 时这个模型不用动。
    """

    model_config = {"extra": "allow"}

    mail_source: Optional[str] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    try:
        db.save_mail_config(req.model_dump(exclude_none=True))
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail():
    """测试当前邮箱来源的连通性，具体怎么测由 provider 的 self_test() 决定。

    原来这里写死了 CF 的 api_url/domain/token 三个字段，
    换成让 provider 自检 —— 加邮箱不用回来改这个路由。
    """
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 池化 provider 的连通性绑定在具体某个号上，没号可测 ——
    # 它的"测试"就是导入时的格式校验 + 跑一次注册。
    if provider_cls.pooled:
        raise HTTPException(
            400,
            f"{provider_cls.display_name} 是号池类型，不需要单独测试；"
            f"导入时会校验格式",
        )

    try:
        provider = create_mail_provider(mail_source, db.get_mail_settings())
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"构造 {provider_cls.display_name} 失败: {e}")

    try:
        result = provider.self_test()
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")
    if not result.get("ok"):
        raise HTTPException(500, result.get("message") or "连接失败")
    return {"ok": True, "message": result.get("message", "连接成功")}


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    """接码配置。字段按各平台官方 API 对齐，这里用 extra=allow 原样透传给 db。

    （新增配置项时只改 db.get_sms_config/save_sms_config，这个模型不用动。）
    """

    model_config = {"extra": "allow"}

    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms / smspool
    sms_platform_mode: Optional[str] = None        # auto / 单个平台 / 平台 CSV
    sms_api_key_smsbower: Optional[str] = None
    sms_api_key_herosms: Optional[str] = None
    sms_api_key_smspool: Optional[str] = None
    sms_country: Optional[str] = None
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_min_price: Optional[str] = None
    sms_price_margin: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None
    sms_excluded_countries: Optional[str] = None
    sms_blocked_prefixes: Optional[str] = None
    sms_prefix_max_retry: Optional[str] = None
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_auto_top_n: Optional[str] = None
    sms_physical_only: Optional[str] = None
    sms_exclude_providers: Optional[str] = None
    sms_include_providers: Optional[str] = None
    sms_use_monitor_best: Optional[str] = None
    sms_price_weight: Optional[str] = None
    sms_min_success_rate: Optional[str] = None
    sms_price_poll_seconds: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None
    sms_per_phone_timeout: Optional[str] = None


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    data = req.model_dump(exclude_none=True)
    if "sms_platform_mode" in data:
        mode = str(data["sms_platform_mode"] or "auto").strip().lower() or "auto"
        selected = list(dict.fromkeys(sms_prices.split_csv(mode)))
        if mode != "auto" and (not selected or any(pk not in sms_prices.PLATFORMS for pk in selected)):
            raise HTTPException(400, "接码平台选择无效，请重新选择。")
        data["sms_platform_mode"] = "auto" if mode == "auto" else ",".join(selected)
    # ★ API Key 长度校验：真事故 —— 一个 1 字符的假 Key（"k"）被写进库，
    #   接码全部 401 Unauthorized，号租不到、退号也查不到，排查半天。
    #   平台 Key 都是 32 位，短于 16 位一律拒收（要清空就传空串）。
    for k in ("sms_api_key_smsbower", "sms_api_key_herosms", "sms_api_key_smspool"):
        v = str(data.get(k) or "").strip()
        if db._is_masked_secret(v):      # `***` = 保持原值，不是要写入的值
            data.pop(k, None)
            continue
        if v and len(v) < 16:
            raise HTTPException(400, f"{k} 长度只有 {len(v)} 位，明显不是有效 Key（平台 Key 为 32 位）；"
                                     f"要清空请留空。已拒绝保存，避免把好 Key 覆盖掉。")
    db.save_sms_config(data)
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """Query all configured providers independently; never purchase a number."""
    cfg = db.get_sms_internal_config()
    from concurrent.futures import ThreadPoolExecutor
    from smsinvit.invitation.safety import redact
    from sms_provider import create_sms_provider

    def query(pk):
        start = time.monotonic()
        entry = {"label": sms_prices.PLATFORM_LABELS[pk], "configured": sms_prices.platform_key_configured(cfg, pk),
                 "status": "unconfigured", "ok": False, "balance": None, "elapsed_ms": 0, "error": ""}
        if not entry["configured"]:
            entry["error"] = "未配置 API Key"
            return pk, entry
        try:
            provider = create_sms_provider(pk, dict(cfg))
            # Use the same guarded route as rentals, with a bounded query timeout.
            request = getattr(provider, "_request", None)
            if request:
                def bounded(params, **kwargs):
                    kwargs["timeout"] = min(10, kwargs.get("timeout", 10))
                    return request(params, **kwargs)
                provider._request = bounded
            balance = provider.get_balance()
            import math
            if not math.isfinite(float(balance)) or float(balance) < 0:
                raise ValueError("平台余额响应无效")
            entry.update(status="success", ok=True, balance=float(balance))
        except Exception as exc:  # noqa: BLE001
            entry.update(status="failed", error=redact(str(exc))[:300])
        entry["elapsed_ms"] = round((time.monotonic() - start) * 1000)
        return pk, entry

    with ThreadPoolExecutor(max_workers=3) as pool:
        platforms = dict(pool.map(query, sms_prices.PLATFORMS))
    return {"ok": True, "all_ok": all(item["ok"] for item in platforms.values()), "platforms": platforms}


def _sms_country_quotes(cfg: dict):
    """Use fresh selected-platform snapshots first; missing quotes are read in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    service = str(cfg.get("sms_service") or "dr").strip() or "dr"
    platforms = tuple(pk for pk in sms_prices.selected_platforms(cfg)
                      if sms_prices.platform_key_configured(cfg, pk))
    cached = {pk: [] for pk in platforms}
    try:
        states = {row["platform"]: row for row in db.sms_price_state_list()}
        max_age = max(30, 3 * sms_prices._int(cfg.get("sms_price_poll_seconds"), 5))
        now = time.time()
        for row in db.sms_price_list(service=service):
            pk = row.get("platform")
            if (pk in cached and not states.get(pk, {}).get("last_error")
                    and now - sms_prices._float(row.get("updated_at"), 0) <= max_age):
                cached[pk].append(dict(row))
    except Exception:
        cached = {pk: [] for pk in platforms}

    def read(pk):
        try:
            provider = sms_prices.SmsPriceMonitor._readonly_provider(pk, dict(cfg), "getPrices")
            return pk, provider.get_top_countries(service=service)
        except Exception:
            return pk, []

    missing = [pk for pk in platforms if not cached[pk]]
    live = {}
    if missing:
        with ThreadPoolExecutor(max_workers=len(missing)) as pool:
            live = dict(pool.map(read, missing))
    rows = [dict(row, platform=pk) for pk in platforms for row in (cached[pk] or live.get(pk, []))]
    rows.sort(key=lambda row: (sms_prices._float(row.get("price"), float("inf")),
                               -sms_prices._int(row.get("count"), 0)))
    return rows, "live" if any(live.values()) else "cache"


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询所选接码平台的国家报价，不租号、不使用其它平台回退。"""
    from sms_provider import OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
    cfg = db.get_sms_internal_config()
    if not any(sms_prices.platform_key_configured(cfg, pk) for pk in sms_prices.selected_platforms(cfg)):
        raise HTTPException(400, "所选接码平台未配置 API Key")
    rows, source = _sms_country_quotes(cfg)
    if not rows:
        raise HTTPException(500, "当前所选接码平台暂无国家报价，请稍后重试。")
    countries = [dict(row, openai_sms_safe=str(row.get("country")) in OPENAI_SMS_COUNTRIES,
                      name_cn=SMS_COUNTRY_NAMES_CN.get(str(row.get("country")), "未知"))
                 for row in rows[:30]]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": source}


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """返回当前平台实际有库存的国家（动态查询）；查询失败则 fallback 到静态字典。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES

    cfg = dict(db.get_sms_internal_config())
    if provider:
        cfg["sms_platform_mode"] = provider
    rows, source = _sms_country_quotes(cfg)
    countries, seen = [], set()
    for row in rows:
        cid = str(row.get("country") if row.get("country") is not None else "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        countries.append({"id": cid, "name_cn": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
                          "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                          "price": row.get("price"), "count": row.get("count")})
    if countries:
        return {"ok": True, "countries": countries,
                "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": source}

    # fallback: 静态字典
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {"id": cid, "name_cn": name, "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES}
        for cid, name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


# ──────────────────────── 自动导出 (CPA / SUB2API) ────────────────────────


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # 传 '***' 表示不修改
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # '***' 不修改
    sub2api_group_ids: Optional[str] = None  # 逗号分隔，例 "2" 或 "1,2,3"
    sub2api_timeout: Optional[str] = None


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from . import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        raise HTTPException(400, f"未知 target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"测试失败: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="选择导出目标：cpa / sub2api")


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # 手动触发：强制启用
        try:
            out["cpa"] = exporter.export_to_cpa(cred, cpa_cfg)
        except Exception as e:
            out["cpa"] = {"ok": False, "error": str(e)}
    if "sub2api" in targets:
        sub2api_cfg = dict(cfg["sub2api"])
        sub2api_cfg["enabled"] = True
        try:
            out["sub2api"] = exporter.export_to_sub2api(cred, sub2api_cfg)
        except Exception as e:
            out["sub2api"] = {"ok": False, "error": str(e)}

    return {"ok": True, **out}


class UpdateCredReq(BaseModel):
    email: str = Field(..., description="要修改的已注册账号邮箱")
    # None = 该字段不动；空串 = 主动清空。前端不填的字段就别传。
    password: Optional[str] = Field(None, description="新密码，None=不修改")
    totp_secret: Optional[str] = Field(None, description="新 TOTP secret，None=不修改")


@app.post("/api/registered/update_credentials")
def api_update_credentials(req: UpdateCredReq):
    """手动修正已注册账号的密码 / TOTP secret。

    ⚠️ 只改本地库，不会同步到 OpenAI。用途是把外部已知凭证补进来或修正记录。

    改完的值会被登录流程直接用上（registrar 的 account_callback 走
    db.get_registered，不区分数据来源），所以 totp_secret 必须过 base32
    校验 —— 脏值存进去要等真登录时才炸，那时根本看不出是手填填错的。
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    if req.password is None and req.totp_secret is None:
        raise HTTPException(400, "没有要修改的字段")
    try:
        ok = db.update_registered_manual(
            email, password=req.password, totp_secret=req.totp_secret
        )
    except ValueError as e:
        # 校验失败：把具体原因带给前端，别让用户猜哪里填错了
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"未找到已注册账号: {email}")

    changed = [n for n, v in (("密码", req.password), ("TOTP secret", req.totp_secret))
               if v is not None]
    logger.info(f"[registered] 手动修改凭证 email={email} 字段={'+'.join(changed)}")
    return {"ok": True, "email": email, "changed": changed}


# ──────────────────────── Plus 试用检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="查询代理，留空直连")


# 封号在 401/403 响应体里的措辞。OpenAI 不止一种写法，全部小写后子串匹配。
# 新措辞加在这里即可；日志会打出未匹配的 401/403 原文方便补充。
_DEACTIVATED_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "has been deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",          # violating / violation of our policies
    "potential abuse",
    "terminated",
)


def _body_text(resp) -> str:
    """安全取响应体文本，任何异常都不许打断检测循环。"""
    try:
        return (resp.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _looks_deactivated(body: str) -> bool:
    return any(m in body.lower() for m in _DEACTIVATED_MARKERS)


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """用 access_token 查询账号的 Plus 试用状态。"""
    from http_client import create_http_session

    log = logging.getLogger("webui")
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 走和注册流程同一个 create_http_session，不再自己拼 proxies dict。
    # 它负责两件这里以前漏掉的事：
    #   1) socks5:// -> socks5h://，DNS 交给代理端解析。用本地 DNS 打
    #      chatgpt.com 经常握手失败，这是「填了 SOCKS5 就检测不出来」的真正原因。
    #   2) trust_env=False + 显式空代理，代理留空时是真直连，
    #      不会被系统 HTTP_PROXY/HTTPS_PROXY 悄悄接管。
    proxy = req.proxy.strip()
    try:
        sess = create_http_session(proxy=proxy or None, impersonate="chrome110")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"创建 HTTP 会话失败: {e}")

    note = ""

    def _check(access_token: str, account_id: str = "", device_id: str = ""):
        """打一次检测请求。

        ⚠️ 这里**不再自动降级直连**。原来的行为是：代理第一次报错就永久切直连，
        后面所有号都用主人的真实 IP 去打 chatgpt.com 的账号接口，而提示只是
        结果末尾一句小字。2026-08-10 实测踩到：主人改了代理池密码，这页却还在
        用 localStorage 里的旧代理 → curl:(97) 鉴权被拒 → 静默直连。
        检测失败重试一次就好，不值得拿真实 IP 换。

        请求头按 chatgpt.com 前端真实发的补齐（Origin/Referer/ChatGPT-Account-ID/
        OAI-Device-Id）。以前只发 Authorization，缺 Origin/Referer 属于典型的
        非浏览器特征，容易被风控挑出来；account_id 从 access_token 的 JWT 里解，
        不额外请求。
        """
        nonlocal note
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": ua,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        if device_id:
            headers["OAI-Device-Id"] = device_id
        try:
            return sess.get(url, headers=headers, timeout=15)
        except Exception as e:  # noqa: BLE001
            if proxy and not note:
                # 把 curl 的错误码带出来：(97)=SOCKS5 鉴权被拒，(7)=连不上，
                # 笼统一句「代理连不通」会让人以为是网络抖动，其实是密码/配额问题。
                msg = str(e)
                if "(97)" in msg or "rejected by the SOCKS5" in msg:
                    note = "代理认证被拒（SOCKS5 (97)）—— 检查代理账号密码/配额是否已变更"
                elif "(7)" in msg:
                    note = "代理连不上（curl (7)）—— 检查代理地址端口是否可达"
                else:
                    note = f"代理请求失败（{type(e).__name__}）—— 已保持代理，未改直连"
                log.warning(f"[check_plus] {note}: {msg[:140]}")
            raise

    results = {}
    for email in req.emails:
        cred = db.get_registered(email)
        if not cred:
            results[email] = {"status": "not_found", "label": "未找到"}
            continue
        at = (cred.get("access_token") or "").strip()
        if not at:
            results[email] = {"status": "no_at", "label": "无AT"}
            continue
        # account_id 直接从 AT 的 JWT payload 解（实测 12/12 都带），不发额外请求。
        auth_claims = _get_auth(_decode_jwt_payload(at))
        account_id = str(
            auth_claims.get("chatgpt_account_id") or auth_claims.get("account_id") or ""
        ).strip()
        # device_id 库里普遍是空的（注册时没落盘），按邮箱派生一个稳定 UUID：
        # 同一个号每次检测都是同一个 device，比每次随机更像正常客户端。
        device_id = (cred.get("device_id") or "").strip() or str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"dango-check-plus:{email}")
        )
        try:
            resp = _check(at, account_id, device_id)
        except Exception as e:  # noqa: BLE001
            results[email] = {"status": "error", "label": "网络失败"}
            log.warning(f"[check_plus] {email} 请求失败: {str(e)[:140]}")
            continue
        if resp.status_code in (401, 403):
            # 401/403 的**响应体必须看**。以前这里只看状态码就贴「凭证失效」，
            # 结果是封号号 100% 显示成凭证失效：账号被封时 access_token 会被一起
            # 吊销 → 请求在这里就 401 了 → 永远走不到下面 200 分支的 is_deactivated
            # 判据。2026-08-10 实测某个被封号：JWT exp 还有 239 小时、
            # 13:53 检测还是 plus_eligible，之后被封 → 同一个 token 直接 401。
            #
            # 未过期却失效 = 被吊销，而 OpenAI 会在响应体里写明原因，
            # 所以按响应体内容区分「封号」和「单纯的凭证过期/轮换」。
            body = _body_text(resp)
            if _looks_deactivated(body):
                results[email] = {"status": "banned", "label": "封号"}
                log.info(f"[check_plus] {email} 判定封号 (HTTP {resp.status_code}): {body[:200]}")
                continue
            if resp.status_code == 401:
                results[email] = {"status": "token_invalid", "label": "凭证失效"}
                # 日志留原文：万一是没覆盖到的封号措辞，主人看一眼就能告诉我补进去。
                log.info(f"[check_plus] {email} 401 响应体: {body[:200]}")
                continue
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            log.info(f"[check_plus] {email} 403 响应体: {body[:200]}")
            continue
        if resp.status_code != 200:
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            results[email] = {"status": "error", "label": "响应非 JSON"}
            continue
        accts = data.get("accounts", {})
        if not accts:
            results[email] = {"status": "error", "label": "无账户数据"}
            continue
        info = next(iter(accts.values()))
        acct = info.get("account", {})
        ent = info.get("entitlement", {})
        promo = info.get("eligible_promo_campaigns", {})
        if acct.get("is_deactivated", False):
            results[email] = {"status": "banned", "label": "封号"}
            continue
        plan = acct.get("plan_type", "free")
        has_sub = ent.get("has_active_subscription", False)
        has_plus_promo = "plus" in promo and promo["plus"].get("id") == "plus-1-month-free"
        if plan == "plus" or has_sub:
            results[email] = {"status": "plus_active", "label": "Plus生效中"}
        elif has_plus_promo:
            results[email] = {"status": "plus_eligible", "label": "可领Plus试用"}
        else:
            results[email] = {"status": "free", "label": "Free"}

    try:
        sess.close()
    except Exception:  # noqa: BLE001
        pass

    checked_at = time.time()
    for email, info in results.items():
        # not_found / no_at / error 不写库：它们不是「检测结论」而是**没检测成**
        # （号不在库里、没凭证、代理挂了），写进去号就从 unchecked 过滤器里消失，
        # 看着像已经检测过。修好后重点一次即可。
        #
        # token_invalid **要写**（2026-08-10 改）。原先不写的理由是「凭证问题不是
        # 账号问题，换新凭证后该重查」，但实测下来：AT 没过期却 401 = 被吊销，
        # 大概率就是封号（2026-08-10 实测那个号即是）。不写库的实际后果是这号
        # 一直挂着上次的 plus_eligible，列表上显示「可领Plus试用」——比标成凭证
        # 失效误导得多。写库后 unchecked 过滤器会跳过它，正是想要的：它已经有结论了。
        if info["status"] not in ("not_found", "no_at", "error"):
            db.update_plus_check(email, {**info, "checked_at": checked_at})

    return {"ok": True, "results": results, "note": note}


# ════════════════════════════════════════════════════════════════
#  codex_invit 新增：接码价格 / 代理池 / 卡密 / 邀请设置 / Codex 授权
# ════════════════════════════════════════════════════════════════

# ──────────────────────── 概览（仪表盘） ────────────────────────


@app.get("/api/overview")
def api_overview():
    """仪表盘一次拿全：号池 + 卡密 + 代理池 + 最优接码 + 最近运行。"""
    try:
        best = sms_prices.compute_best()
    except Exception:  # noqa: BLE001
        best = None
    return {
        "ok": True,
        "stats": db.stats(),
        "by_kind": db.stats_by_kind(),
        "cdk": db.cdk_stats(),
        "proxies": db.proxy_summary(),
        "sms": {
            "best": best,
            "poll_seconds": sms_prices.MONITOR.interval(),
            "rounds": sms_prices.MONITOR._rounds,
            "last_error": sms_prices.MONITOR._last_error,
        },
        "runs": db.list_runs(limit=10),
        # 注册/接受邀请/CLI 三段分开，避免把"注册成功"当成"邀请完成"。
        "authorization": db.authorization_counts(),
        "mail_source": db.get_setting("mail_source", "outlook"),
        # 同一套筛选条件：分配不看来源，统计也不能看，否则"池里有号却显示 0"。
        "pool_available": db.count_available_mailboxes(),
    }


# ──────────────────────── 接码价格（5s 任务） ────────────────────────


@app.get("/api/sms/prices")
def api_sms_prices():
    return {"ok": True, **sms_prices.MONITOR.snapshot()}


@app.post("/api/sms/prices/refresh")
def api_sms_prices_refresh():
    result = sms_prices.MONITOR.refresh_once()
    return {"ok": True, "refreshed": result, **sms_prices.MONITOR.snapshot()}


@app.post("/api/sms/selftest")
def api_sms_selftest():
    """只读探测各平台的真实响应字段（确认 physicalCount / phoneException 支持情况）。"""
    return {"ok": True, **sms_prices.MONITOR.selftest()}


@app.post("/api/sms/config/apply_best")
def api_sms_apply_best():
    best = sms_prices.compute_best()
    if not best:
        raise HTTPException(400, "当前没有可选的最优结果（检查国家/价格/库存/成功率过滤）")
    db.save_sms_config({
        "sms_country": str(best["country"]),
        "sms_max_price": str(best["price"]),
        "sms_provider": str(best["platform"]),
    })
    return {
        "ok": True,
        "applied": {"sms_country": best["country"], "sms_max_price": best["price"],
                    "sms_provider": best["platform"]},
        "config": db.get_sms_config(),
    }


# ──────────────────────── 代理池（服务端） ────────────────────────


@app.get("/api/proxy/pool")
def api_proxy_pool():
    return {
        "ok": True,
        "items": db.proxy_list_with_state(),
        "summary": db.proxy_summary(),
        "config": {
            "threshold": proxy_pool.threshold(),
            "freeze_minutes": proxy_pool.freeze_minutes(),
            "attempts_limit": proxy_pool.attempts_limit(),
            # 每个代理有两套独立的 409 锁：register=GPT 注册，codex=Codex 激活
            "purposes": list(proxy_pool.PURPOSES),
        },
    }


class ProxyPoolTextReq(BaseModel):
    text: str = Field("", description="多行代理，每行一个")


@app.post("/api/proxy/pool/add")
def api_proxy_pool_add(req: ProxyPoolTextReq):
    return {"ok": True, **proxy_pool.add_from_text(req.text)}


@app.post("/api/proxy/pool/replace")
def api_proxy_pool_replace(req: ProxyPoolTextReq):
    """覆盖保存：不在新列表里的删掉，仍在新列表里的**保留统计**。"""
    return {"ok": True, **proxy_pool.replace_from_text(req.text)}


class ProxyPoolIdReq(BaseModel):
    id: Optional[int] = None
    url: str = ""
    enabled: bool = True


def _proxy_ident(req: ProxyPoolIdReq):
    return req.id if req.id is not None else (req.url or "").strip()


@app.post("/api/proxy/pool/enable")
def api_proxy_pool_enable(req: ProxyPoolIdReq):
    if not db.proxy_set_enabled(_proxy_ident(req), req.enabled):
        raise HTTPException(404, "代理不存在")
    return {"ok": True, "items": db.proxy_list_with_state(), "summary": db.proxy_summary()}


@app.post("/api/proxy/pool/reset")
def api_proxy_pool_reset(req: ProxyPoolIdReq, purpose: str = "both"):
    """重置代理状态。purpose=register/codex 只清该用途的 409 锁（永久不可用必须可复位）。"""
    if purpose not in ("both", "register", "codex"):
        raise HTTPException(400, "purpose 只能是 both / register / codex")
    if not db.proxy_reset(_proxy_ident(req), purpose):
        raise HTTPException(404, "代理不存在")
    return {"ok": True, "items": db.proxy_list_with_state(), "summary": db.proxy_summary()}


class ProxyPoolSchemeReq(BaseModel):
    id: Optional[int] = None
    url: str = ""
    scheme: str = Field(..., description="http / https / socks5（socks5 归一为 socks5h）")
    clear_locks: bool = Field(True, description="换协议时一并解除冻结与永久不可用")


@app.post("/api/proxy/pool/scheme")
def api_proxy_pool_scheme(req: ProxyPoolSchemeReq):
    """按 id 原地更换协议：保留统计，且不依赖前端手里的（可能已脱敏的）地址。"""
    try:
        result = db.proxy_set_scheme(_proxy_ident(req), req.scheme, clear_locks=req.clear_locks)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该协议地址已存在，请先处理重复条目") from None
    if result is None:
        raise HTTPException(404, "代理不存在")
    return {"ok": True, **result, "items": db.proxy_list_with_state(), "summary": db.proxy_summary()}


@app.post("/api/proxy/pool/remove")
def api_proxy_pool_remove(req: ProxyPoolIdReq):
    if not db.proxy_remove(_proxy_ident(req)):
        raise HTTPException(404, "代理不存在")
    return {"ok": True, "items": db.proxy_list_with_state(), "summary": db.proxy_summary()}


@app.get("/api/proxy/pool/events")
def api_proxy_pool_events(proxy_id: Optional[int] = None, limit: int = 200):
    return {"ok": True, "items": db.proxy_list_events(proxy_id=proxy_id, limit=limit)}


@app.get("/api/cdk/monitor")
def api_cdk_monitor(limit: int = 40):
    """后台「实时执行监控」：正在跑/排队的作业 + 每个卡密的进度 + 最近事件。

    前端每 2 秒拉一次就能看到作业走到哪一步（等邀请信 / 注册 / 登录 / 接受邀请 /
    Codex 授权 / sayhi）、试了几次、用的哪个代理、收件箱读到几封、错在哪。
    """
    import time as _t

    now = _t.time()
    labels = dict(codex_auth.USER_STAGE_LABEL)
    labels.update({"queued": "正在排队", "unused": "未兑换", "bound": "待开始"})

    jobs = db.list_cdk_jobs_filtered(statuses=None, limit=max(1, min(int(limit), 200)))
    items = []
    for j in jobs:
        c = db.get_cdk_code(j.get("code") or "") or {}
        started, finished = j.get("started_at"), j.get("finished_at")
        elapsed = None
        if started:
            elapsed = int((finished or now) - started)
        items.append({
            "job_id": j.get("job_id"),
            "code": j.get("code"),
            "email": j.get("email"),
            "status": j.get("status"),
            "stage": j.get("stage"),
            "stage_label": labels.get(j.get("stage") or "", j.get("stage") or ""),
            "progress": j.get("progress") or 0,
            "attempt": j.get("attempt") or 0,
            "proxy": j.get("proxy") or "",
            "error": j.get("error") or "",
            "started_at": started,
            "finished_at": finished,
            "elapsed": elapsed,
            "cdk_status": c.get("status") or "",
            "cdk_fail_count": c.get("fail_count") or 0,
            "cdk_attempts": c.get("attempts") or 0,
            "proxy_used": c.get("proxy_used") or "",
            "invite_sent_at": c.get("invite_sent_at"),
            "invite_processed_at": c.get("invite_processed_at"),
            "inviter_email": c.get("inviter_email") or "",
        })

    stats = cdk.CONTROLLER.stats()
    global_counts = db.cdk_stats()
    return {
        "ok": True,
        "now": now,
        "items": items,
        "events": db.list_recent_cdk_events(limit=60),
        "stage_labels": labels,
        "summary": {
            # 这些是**全局**计数：旧实现只统计本页返回的 items，于是 limit 之外的
            # 排队/执行任务在"实时监控"里凭空消失。
            "running": int((global_counts or {}).get("jobs_running") or 0),
            "queued": int((global_counts or {}).get("jobs_queued") or 0),
            "done": int((global_counts or {}).get("done") or 0),
            "failed": int((global_counts or {}).get("failed") or 0),
            "itemsReturned": len(items),
            "itemsLimit": max(1, min(int(limit), 200)),
            "workers": stats.get("worker_alive"),
            "max_concurrency": stats.get("max_concurrency"),
            "proxy_available": (stats.get("proxies") or {}).get("available"),
            "mail_pool": (db.stats() or {}).get("mail_pool"),
        },
    }


@app.post("/api/proxy/diagnose")
def api_proxy_diagnose():
    """一键诊断：直连 / 本机系统代理 / 池内代理 各打一次，比出口 IP，给出唯一结论。

    ⚠️ 这个接口的网络 I/O 发生在**服务进程**里 —— 所以要让服务跑在你自己的
    终端里，结果才代表你的网络（否则测的是启动服务的那个环境的出口）。
    """
    from . import proxy_diag

    return proxy_diag.diagnose()


# ──────────────────────── 卡密 ────────────────────────

_CDK_ALPHABET = string.ascii_uppercase + string.digits


class CdkGenerateReq(BaseModel):
    count: int = Field(10, ge=1, le=5000)
    batch: str = ""
    prefix: str = "CDK-"
    length: int = Field(20, ge=8, le=64)
    note: str = ""


@app.post("/api/cdk/generate")
def api_cdk_generate(req: CdkGenerateReq):
    prefix = (req.prefix or "CDK-")[:32]
    length = max(8, min(64, int(req.length)))
    want = int(req.count)
    codes: set[str] = set()
    guard = 0
    while len(codes) < want and guard < want * 20:
        guard += 1
        codes.add(prefix + "".join(secrets.choice(_CDK_ALPHABET) for _ in range(length)))
    batch = (req.batch or "").strip() or time.strftime("%Y%m%d-%H%M%S")
    res = db.create_cdk_codes(sorted(codes), batch=batch, note=req.note or "")
    return {"ok": True, "batch": batch, **res, "codes": sorted(codes)}


@app.get("/api/cdk/list")
def api_cdk_list(status: str = "", batch: str = "", search: str = "",
                 limit: int = 50, offset: int = 0):
    items = db.list_cdk_codes(status=status, batch=batch, search=search,
                              limit=limit, offset=offset)
    total = db.count_cdk_codes(status=status, batch=batch, search=search)
    return {"ok": True, "items": items, "total": total, "batches": db.list_cdk_batches(),
            "stats": db.cdk_stats()}


@app.get("/api/cdk/batches")
def api_cdk_batches():
    return {"ok": True, "batches": db.list_cdk_batches()}


@app.get("/api/cdk/stats")
def api_cdk_stats():
    return {"ok": True, "stats": cdk.CONTROLLER.stats()}


@app.get("/api/cdk/export/formats")
def api_cdk_export_formats():
    return {"ok": True, "formats": cdk_formats.list_formats()}


class CdkExportReq(BaseModel):
    format: str
    codes: Optional[list[str]] = None
    batch: str = ""
    status: str = ""
    search: str = ""
    all: bool = False


@app.post("/api/cdk/export")
def api_cdk_export(req: CdkExportReq):
    fmt = cdk_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")
    if req.codes:
        rows = db.get_cdk_codes(req.codes)
        rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    else:
        rows = db.list_cdk_codes(status=req.status, batch=req.batch, search=req.search,
                                 limit=100000, offset=0)
    base = {"ok": True, "count": len(rows), "filename": fmt.filename,
            "label": fmt.label, "mode": fmt.mode, "mime": fmt.mime}
    if fmt.mode == "download":
        blob = cdk_formats.render_bytes(rows, fmt)
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob)}
    return {**base, "text": cdk_formats.render_text(rows, fmt)}


@app.get("/api/cdk/records")
def api_cdk_records(code: str, limit: int = 100):
    """卡密使用记录：作业历史 + 事件流水（含邀请方邮箱、实际代理、失败原因）。"""
    cdk_row = db.get_cdk_code(code)
    if not cdk_row:
        raise HTTPException(404, "卡密不存在")
    return {
        "ok": True,
        "code": cdk_row,
        "jobs": db.list_cdk_jobs(code=code, limit=limit),
        "events": db.list_cdk_events(code=code, limit=limit),
    }


class CdkCodeReq(BaseModel):
    code: str


@app.post("/api/cdk/reset")
def api_cdk_reset(req: CdkCodeReq):
    if not db.reset_cdk(req.code):
        raise HTTPException(404, "卡密不存在")
    return {"ok": True, "code": db.get_cdk_code(req.code)}


@app.post("/api/cdk/void")
def api_cdk_void(req: CdkCodeReq):
    res = db.void_cdk(req.code, reason="管理员作废")
    if not res.get("ok"):
        raise HTTPException(404, "卡密不存在")
    return {"ok": True, **res, "code": db.get_cdk_code(req.code)}


@app.post("/api/cdk/unbind")
def api_cdk_unbind(req: CdkCodeReq):
    """解绑邮箱（邮箱回池、卡密回到未兑换）。"""
    n = db.release_cdk_mailbox(req.code)
    db.set_cdk_status(req.code, "unused", email=None, mail_kind=None, invite_link=None,
                      inviter_email=None, inviter_subject=None, invite_received_at=None,
                      invite_sent_at=None, invite_processed_at=None, redeemed_at=None,
                      attempts=0, fail_count=0, proxy_used=None, error=None,
                      finished_at=None, result_json=None)
    return {"ok": True, "released": n, "code": db.get_cdk_code(req.code)}


class CdkUpdateReq(BaseModel):
    code: str
    note: Optional[str] = None
    batch: Optional[str] = None


@app.post("/api/cdk/update")
def api_cdk_update(req: CdkUpdateReq):
    fields = {}
    if req.note is not None:
        fields["note"] = req.note
    if req.batch is not None:
        fields["batch"] = req.batch
    if not fields:
        raise HTTPException(400, "没有要修改的字段")
    if not db.update_cdk_fields(req.code, **fields):
        raise HTTPException(404, "卡密不存在")
    return {"ok": True, "code": db.get_cdk_code(req.code)}


class CdkDeleteReq(BaseModel):
    codes: list[str]


@app.post("/api/cdk/delete")
def api_cdk_delete(req: CdkDeleteReq):
    """删除卡密记录（**不**自动把邮箱放回池子；要放回请先「解绑」）。

    删之前先解绑，避免邮箱被一张不存在的卡密永久占住。
    """
    for c in req.codes or []:
        try:
            db.release_cdk_mailbox(c)
        except Exception:  # noqa: BLE001
            pass
    n = db.delete_cdk_codes(req.codes)
    return {"ok": True, "deleted": n}


# ──────────────────────── 邀请与执行设置 ────────────────────────

_INVITE_KEYS = (
    "invite_wait_timeout", "invite_max_attempts", "invite_want_2fa",
    "invite_otp_timeout", "cdk_max_concurrency", "proxy_409_threshold",
    "proxy_freeze_minutes", "codex_binary", "sayhi_prompt", "sayhi_model",
    "sentinel_behavior_ms", "sayhi_direct", "invite_direct", "cookie_jar_sso_max_minutes",
    "cli_timeout", "codex_home_root",
    # 账号池自动补号。只作用于"账号工厂"（注册到 2FA 就停），与上面的邀请参数
    # 互不读取：补号有自己的超时、阈值与并发额度（工位 17-32）。
    # 开/关本身是 pool_auto_enabled，走专用接口，不在这里。
    "pool_min_available", "pool_register_concurrency", "pool_check_seconds",
    "pool_otp_timeout",
)
_INVITE_DEFAULTS = {
    "invite_wait_timeout": "150",
    "invite_max_attempts": "3",
    "invite_want_2fa": "1",   # 默认开：和本项目一样，每次执行都绑 2FA
    "invite_otp_timeout": "120",
    "cdk_max_concurrency": "3",
    "proxy_409_threshold": "2",
    "proxy_freeze_minutes": "20",
    "codex_binary": "codex",
    "codex_originator": "codex_work_desktop",   # ★ 客户端身份，决定服务端认不认这次回合
    "sayhi_prompt": "你好",
    "sayhi_model": "gpt-5.6-luna",
    # Sentinel PoW 里"模拟人类操作"的补齐等待（毫秒）：事件本身约 2s，
    # 写死 4200 等于每次白等 1-2s。0 = 不补齐。
    "sentinel_behavior_ms": "1200",
    # CLI 兑现这一步是否直连（不走代理）。实测直连模型阶段 8.1s，
    # 走慢代理 18.3s 甚至整轮跑不完。
    # 实测：CLI 真跑一轮直连 10.6s，走慢代理 33.3s 甚至整轮跑不完 → 默认直连。
    # ⚠️ 这里的默认值只影响"后台页面显示什么"：运行时读的是
    # codex_auth._truthy_setting("sayhi_direct", "1")，两处必须一致
    # （曾经这里写 1、运行时写 0 且库里没有这一行 → 页面显示直连已开，
    # 生产却一直走代理，三段计时 16/33/36s）。
    "sayhi_direct": "1",
    # 邀请/授权是否直连优先（被 CF 拦时自动改用代理重试一次）。
    # 实测直连会被 CF 间歇 403（同一流程一次 200、随后连续 403），故默认**关**。
    "invite_direct": "0",
    # 会话罐还能"免登录"的最长年龄（分钟）。超过就只回放 chatgpt.com 会话，
    # auth.openai.com 的旧 cookie 一律丢弃（实测它们有害）。
    "cookie_jar_sso_max_minutes": "25",
    "cli_timeout": "180",
    "codex_home_root": "",
    "pool_min_available": "10",
    "pool_register_concurrency": "2",
    "pool_check_seconds": "60",
    "pool_otp_timeout": "180",
}


@app.get("/api/settings/invite")
def api_get_invite_config():
    return {"ok": True, "config": {k: db.get_setting(k, _INVITE_DEFAULTS[k]) for k in _INVITE_KEYS}}


class SaveInviteConfigReq(BaseModel):
    model_config = {"extra": "allow"}


@app.post("/api/settings/invite")
def api_save_invite_config(req: SaveInviteConfigReq):
    data = req.model_dump(exclude_none=True)
    for k in _INVITE_KEYS:
        if k in data:
            db.set_setting(k, str(data[k]).strip())
    return {"ok": True, "config": {k: db.get_setting(k, _INVITE_DEFAULTS[k]) for k in _INVITE_KEYS}}


@app.get("/api/settings/runtime_check")
def api_runtime_check():
    """运行依赖自检：node / codex / curl_cffi / 私有 CODEX_HOME 是否就绪。"""
    from . import codex_cli

    codex_bin = db.get_setting("codex_binary", "codex") or "codex"
    home = db.get_setting("codex_home_root", "") or str(codex_cli.DEFAULT_HOME_ROOT)
    try:
        import curl_cffi  # noqa: PLC0415
        cffi = getattr(curl_cffi, "__version__", "已安装")
    except Exception as e:  # noqa: BLE001
        cffi = f"未安装（{e}）"
    writable = False
    try:
        Path(home).mkdir(parents=True, exist_ok=True)
        probe = Path(home) / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except Exception:  # noqa: BLE001
        writable = False
    return {
        "ok": True,
        "node": shutil.which("node") or "",
        "codex": shutil.which(codex_bin) or (codex_bin if Path(codex_bin).exists() else ""),
        "codex_binary_setting": codex_bin,
        "curl_cffi": cffi,
        "codex_home": home,
        "codex_home_writable": writable,
        "sms_platforms": {
            pk: sms_prices.platform_key_configured(db.get_sms_internal_config(), pk)
            for pk in sms_prices.PLATFORMS
        },
    }


# ──────────────────────── 手动 Codex 授权（已注册邮箱） ────────────────────────


class CodexAuthReq(BaseModel):
    email: str
    proxy: str = ""


def _codex_auth_worker(run_id: str, q, email: str, proxy: str) -> None:
    _rid, _log, _q, handler = registrar.begin_run(
        email, kind="codex_auth", run_id=run_id, existing_queue=q
    )
    res = {}
    try:
        res = codex_auth.authorize_registered_email(
            email, run_id=run_id, proxy_override=proxy
        )
        ok = bool(res.get("ok"))
        err = "" if ok else str(res.get("reason") or "")
        try:
            category = registrar.classify_error(err) if err else ""
        except Exception:  # noqa: BLE001
            category = "unknown"
        if not res.get("_run_finalized"):
            db.finish_run(run_id, "done" if ok else "failed", error=err, category=category)
            res["_run_finalized"] = True
        registrar._emit_status(run_id, "done" if ok else "error", {
            "email": email, "attempts": res.get("attempts"),
            "message": err or "Codex 授权完成",
            "cdk_sync_status": res.get("cdk_sync_status"),
            "cdk_sync_error": res.get("cdk_sync_error", ""),
        })
    except Exception as e:  # noqa: BLE001
        logging.getLogger("webui").exception("[codex_auth] 任务异常")
        if not res.get("_run_finalized"):
            db.finish_run(run_id, "done" if res.get("ok") else "failed",
                          error="" if res.get("ok") else str(e),
                          category="" if res.get("ok") else "unknown")
        try:
            registrar._emit_status(run_id, "done" if res.get("ok") else "error", {
                "email": email, "message": "授权已完成，状态通知异常" if res.get("ok") else str(e),
                "cdk_sync_status": res.get("cdk_sync_status"),
                "cdk_sync_error": res.get("cdk_sync_error", ""),
            })
        except Exception:
            logging.getLogger("webui").exception("[codex_auth] 状态通知失败")
    finally:
        registrar.end_run(run_id, handler)


@app.post("/api/registered/codex_auth")
def api_registered_codex_auth(req: CodexAuthReq):
    # 同一邮箱已经有在跑的授权 → 直接拒绝。
    # 真事故：连点两次「Codex 授权」，两条 run 同时跑同一个号，
    # 各自租号/换代理，互相搅在一起，看起来就像"卡住了"。
    try:
        _email = (getattr(req, "email", "") or "").strip().lower()
        for _r in db.list_runs(limit=50):
            if (_r.get("status") == "running"
                    and str(_r.get("email") or "").lower() == _email
                    and str(_r.get("kind") or "") == "codex_auth"):
                raise HTTPException(409, f"{_email} 的授权正在跑（run={_r.get('run_id')}），等它结束或先停掉")
    except HTTPException:
        raise
    except Exception:
        pass
    """对已注册邮箱手动补跑「邀请 + Codex 授权 + sayhi」。

    走 runs + SSE，前端用现有日志面板看真实过程。
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    if not db.get_registered(email):
        raise HTTPException(404, f"未找到已注册账号: {email}")
    proxy = (req.proxy or "").strip()
    if not proxy and not proxy_pool.has_available():
        raise HTTPException(400, "暂无可用代理：请先到「代理池」页导入/解冻代理")

    run_id = uuid.uuid4().hex[:12]
    q = registrar.pre_register(run_id)
    threading.Thread(
        target=_codex_auth_worker, args=(run_id, q, email, proxy),
        daemon=True, name=f"codex-auth-{run_id}",
    ).start()
    return {"ok": True, "run_id": run_id, "email": email}


# ──────────────────────── 邮箱分配预览（后台排查用） ────────────────────────


@app.get("/api/mail/pool")
def api_mail_pool(limit: int = 50):
    """可兑换账号池：数量 + 明细（后台排查「为什么兑换不了」）。

    拆分后这里列的是 status='registered'（已注册到 2FA、没被卡密占用）的账号，
    不是刚导入的原始邮箱。"""
    kind = db.get_setting("mail_source", "outlook")
    available = mail_alloc.pool_available()
    return {
        "ok": True,
        "mail_source": kind,
        "pooled": mail_alloc.kind_is_pooled(kind),
        "available": available,
        "items": db.list_available_mailboxes(kind, limit=limit),
    }


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR),check_dir=False), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.app:app", host="127.0.0.1", port=8765, reload=False)

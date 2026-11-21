"""codex_invit 用户前台（独立端口，默认 5094）。

**故意做得极简**：兑换 → 显示邮箱 → 「我已发送邀请信」 → 「开始执行」 → 进度条。
用户看得到的东西只有：

  · 邮箱地址（要发给邀请方）
  · 一个脱敏的阶段文案 + 进度百分比
  · 完成 / 失败

看不到（也**永远不会**返回）：代理、密码、2FA、令牌、日志、内部错误码、队列实现。

本进程是唯一跑 CDK worker 的进程（管理后台不启动 worker），
所以「同时只跑 3 个」在全局成立。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import cdk, db, mail_alloc, proxy_pool  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("user_app")

STATIC_DIR = Path(__file__).resolve().parent / "static_user"

app = FastAPI(title="codex_invit 用户前台", docs_url=None, redoc_url=None)

MAX_CODES_PER_REQUEST = 50

# ── 极简限流：每 IP 60s 窗口 120 次 ──
_RL_LOCK = threading.Lock()
_RL_HITS: dict[str, list[float]] = {}
RL_WINDOW = 60.0
RL_LIMIT = 120


def _rate_limit(request: Request) -> None:
    ip = "unknown"
    try:
        ip = (request.client.host if request.client else "") or "unknown"
    except Exception:  # noqa: BLE001
        pass
    now = time.time()
    with _RL_LOCK:
        hits = [t for t in _RL_HITS.get(ip, []) if now - t < RL_WINDOW]
        hits.append(now)
        _RL_HITS[ip] = hits
        if len(hits) > RL_LIMIT:
            raise HTTPException(429, "请求过于频繁，请稍后再试")


def _normalize_codes(codes: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes or []:
        c = str(raw or "").strip().upper()
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


# ──────────────────────── API ────────────────────────


@app.get("/api/u/health")
def api_health():
    return {"ok": True}


@app.get("/api/u/meta")
def api_meta():
    """前台只需要两个信息：还有多少邮箱可换、现在能不能开始。

    `ready` 只是布尔 —— 不暴露代理数量、代理地址或任何池内细节。
    """
    available = mail_alloc.pool_available()
    try:
        ready = proxy_pool.has_available()
    except Exception:  # noqa: BLE001
        ready = False
    st = cdk.CONTROLLER.stats()
    return {
        "ok": True,
        "pool_available": available,          # -1 = 不限量（自建来源）
        "ready": bool(ready),
        "concurrency": st.get("max_concurrency", 3),
        "running": st.get("jobs_running", 0),
        "queued": st.get("jobs_queued", 0),
    }


class RedeemReq(BaseModel):
    codes: list[str] = Field(default_factory=list)


@app.post("/api/u/redeem")
def api_redeem(req: RedeemReq, request: Request):
    _rate_limit(request)
    codes = _normalize_codes(req.codes)
    if not codes:
        raise HTTPException(400, "请输入至少一个卡密")
    if len(codes) > MAX_CODES_PER_REQUEST:
        raise HTTPException(400, f"一次最多 {MAX_CODES_PER_REQUEST} 条")

    rows = {r["code"]: r for r in db.get_cdk_codes(codes)}
    errors: list[dict] = []
    for c in codes:
        row = rows.get(c)
        if not row:
            errors.append({"code": c, "reason": "未找到该卡密"})
        elif row.get("status") != "unused":
            errors.append({"code": c, "reason": "该卡密已兑换"})
    if errors:
        # 整批拒绝：不做部分成功，避免用户对不上账
        return {"ok": False, "errors": errors, "items": []}

    available = mail_alloc.pool_available()
    if available >= 0 and len(codes) > available:
        raise HTTPException(
            400, f"当前可用邮箱 {available} 个，本次需要 {len(codes)} 个，邮箱不足"
        )

    items: list[dict] = []
    done: list[str] = []
    try:
        for c in codes:
            info = mail_alloc.redeem_mailbox(c)
            items.append({"code": c, "email": info["email"], "status": "bound"})
            done.append(c)
    except Exception as e:  # noqa: BLE001
        logger.warning("[redeem] 分配失败，回滚本次已绑：%s", e)
        for c in done:
            try:
                db.release_cdk_mailbox(c)
                db.set_cdk_status(c, "unused", email=None, mail_kind=None,
                                  redeemed_at=None, error=None)
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(500, f"兑换失败：{e}（本次未扣减任何卡密）")

    logger.info("[redeem] %d 张卡密兑换成功", len(items))
    return {"ok": True, "items": items, "errors": []}


class CodeReq(BaseModel):
    code: str


@app.post("/api/u/invite_sent")
def api_invite_sent(req: CodeReq, request: Request):
    """用户确认「我已发送邀请信」。

    没有这一步就不允许开始执行 —— 否则后端会白等 150s 邀请信。
    """
    _rate_limit(request)
    code = str(req.code or "").strip().upper()
    row = db.get_cdk_code(code)
    if not row:
        raise HTTPException(404, "未找到该卡密")
    if row.get("status") not in ("bound", "failed"):
        raise HTTPException(400, "当前状态不能确认邀请信")
    db.mark_invite_sent(code)
    db.add_cdk_event(code, "", "invite_sent", "用户确认已发送邀请信")
    return {"ok": True, "code": code}


@app.post("/api/u/start")
def api_start(req: CodeReq, request: Request):
    _rate_limit(request)
    code = str(req.code or "").strip().upper()
    res = cdk.CONTROLLER.enqueue(code)
    if not res.get("ok"):
        reason = str(res.get("reason") or "")
        if reason == "busy":
            return {"ok": False, "reason": "busy", "message": "当前繁忙，请稍后重试"}
        raise HTTPException(400, str(res.get("message") or "无法开始"))
    item = cdk.CONTROLLER.status_for_codes([code])[0]
    return {"ok": True, "already": bool(res.get("already")), "item": item}


@app.get("/api/u/status")
def api_status(codes: str = "", request: Request = None):  # type: ignore[assignment]
    _rate_limit(request) if request is not None else None
    wanted = _normalize_codes(str(codes or "").split(","))
    if not wanted:
        return {"ok": True, "items": [], "pool_available": mail_alloc.pool_available()}
    if len(wanted) > MAX_CODES_PER_REQUEST * 4:
        raise HTTPException(400, "一次查询的卡密太多")
    return {
        "ok": True,
        "items": cdk.CONTROLLER.status_for_codes(wanted),
        "pool_available": mail_alloc.pool_available(),
    }


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(500, "用户前台还没构建：在 webui/frontend_user 里跑 pnpm build")
    return FileResponse(index)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("webui.user_app:app", host="127.0.0.1", port=5094, reload=False)

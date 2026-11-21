"""CDK 兑换时的邮箱分配（一码一邮箱）。

按「邮箱来源配置」的语义：
  · 池化来源（outlook / icloud_relay）→ 从号池**随机**取一个 available
    且没被任何卡密占用的行；
  · 自建来源（cf_temp 等）→ 现场生成一个新地址并入库钉住。

分配出去的地址就是这个 CDK 后续**注册与 Codex 授权**用的地址（全程不换）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mail_providers import create_mail_provider, get_provider_class  # noqa: E402

from . import db  # noqa: E402

logger = logging.getLogger("mail_alloc")


def current_kind(settings: Optional[dict] = None) -> str:
    settings = settings or db.get_mail_settings()
    return str(settings.get("mail_source") or "outlook").strip().lower()


def kind_is_pooled(kind: str) -> bool:
    try:
        return bool(get_provider_class(kind).pooled)
    except Exception:  # noqa: BLE001 — 未知来源按池化保守处理
        return True


def pool_available(settings: Optional[dict] = None) -> int:
    """可兑换账号数 = 已注册 GPT、尚未 Codex 授权、且没被卡密占用的号。

    拆分后**不分池化/非池化**：CF 临时邮箱这类来源的地址虽然由注册时现造，
    但注册成功后同样会登记成 status='registered' 进号池，所以数量永远是实数，
    不再有"-1 表示不限量"这种会误导用户的语义。

    也**不按当前配置的来源过滤**：分配（`allocate_mailbox` → `claim_cdk_mailbox`
    传空 kind）本来就不看来源，统计必须用同一套筛选条件，否则池里明明有号却报 0，
    前端据此把提交整个拦掉（连"重新输入原卡密查原邮箱"都被挡）。

    `settings` 保留是为了兼容既有调用方；筛选条件不再取决于它。
    """
    return db.count_available_mailboxes()


def allocate_mailbox(code: str, *, settings: Optional[dict] = None) -> dict:
    """给一张卡密分配一个**已注册 GPT**的号。失败抛 RuntimeError（调用方整批回滚）。

    拆分后号码池可以混放多种来源（补号来源与兑换来源各配各的），每个号按自己
    登记时的 kind 取信（见 build_provider_for_account），所以这里**不按当前配置的
    kind 过滤**：谁先注册好就发给谁。地址不再现场生成 —— 号必须先由账号工厂
    注册成 GPT 账号才有资格被兑换。
    """
    row = db.claim_cdk_mailbox(code, "", pooled=True)
    if not row:
        raise RuntimeError("号池里没有可兑换的账号（已注册 GPT、未 Codex 授权、未被占用）")
    kind = str(row.get("kind") or "outlook").strip().lower()
    logger.info("[mail_alloc] 卡密 %s 分到 %s（来源=%s）", code, row["email"], kind)
    return {"email": row["email"], "kind": kind, "pooled": True}


def redeem_mailbox(code: str, *, settings: Optional[dict] = None) -> dict:
    """兑换：分配一个未使用邮箱，并把卡密标记为 bound、记下邮箱。

    这是用户前台兑换时唯一的入口 —— 分配和状态更新绑在一起，避免出现
    「邮箱占了但卡密还是 unused」的中间态。
    """
    import time as _time

    info = allocate_mailbox(code, settings=settings)
    ok = db.set_cdk_status(
        code, "bound",
        email=info["email"],
        mail_kind=info["kind"],
        redeemed_at=_time.time(),
        error=None,
    )
    if not ok:
        # 卡密行不存在（并发删了）→ 把刚占的邮箱放回去，别让它变成孤儿
        db.release_cdk_mailbox(code)
        raise RuntimeError(f"卡密 {code} 不存在或已失效")
    db.add_cdk_event(code, "", "redeem", f"分配邮箱 {info['email']}（来源={info['kind']}）")
    return info


def build_provider_for_account(email: str, settings: Optional[dict] = None):
    """按已分配的邮箱构造 provider，并把地址钉住（自建来源绝不再新造）。

    返回 (provider, kind, account_row)。
    """
    settings = settings or db.get_mail_settings()
    kind = current_kind(settings)
    account = db.get_account(email) or {
        "email": email, "password": "", "client_id": "",
        "refresh_token": "", "relay_url": "", "kind": kind,
    }
    kind = str(account.get('mail_kind') or account.get('kind') or kind)
    provider = create_mail_provider(kind, settings, account)
    provider.pin_mailbox(email)
    return provider, kind, account


def release_mailbox(code: str) -> int:
    return db.release_cdk_mailbox(code)

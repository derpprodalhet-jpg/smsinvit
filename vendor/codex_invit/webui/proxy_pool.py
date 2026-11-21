"""服务端代理池：least-used 轮换 + 409 生命周期 + 开跑前预检查。

规则（主人定的，别改语义）：

  1. **轮换**：每次开始新任务，从「可用且 use_count 最小」的代理开始轮换，
     不把一个 IP 怼死；同一作业内的重试排除本次已经用过的代理。
  2. **409 生命周期**：某代理连续 N 次 409（`proxy_409_threshold`，默认 2）
     → 冻结 F 分钟（`proxy_freeze_minutes`，默认 20）；解冻后再出现 1 次 409
     → **永久不可用**（只能管理员手动「重置」救回来）。
  3. **成功清零**：warmup 通过（该 IP 确认能拿到 oai-did）会清零连续 409 计数，
     所以偶发一次 409 不会累积成冻结。
  4. **预检查**：池里没有可用代理时**不启动**任务；调用方据此告诉用户
     「当前繁忙，请稍后重试」，卡密保持可重试、不作废。

本模块只做策略与文本解析，状态全部落在 `proxy_pool` 表（见 db.py）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from http_client import normalize_proxy  # noqa: E402

from . import db  # noqa: E402

logger = logging.getLogger("proxy_pool")


def _int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


# ──────────────────────── 文本 <-> 池 ────────────────────────


def parse_pool_text(text: str) -> list[str]:
    """多行代理文本 → 归一化去重列表（跳过空行与 `#` 注释）。

    归一化复用注册链路同一套规则（http_client.normalize_proxy），
    保证「池里存的就是跑号时用的」，不会出现存 socks5、跑 socks5h 的偏差。
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        n = normalize_proxy(s)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def add_from_text(text: str) -> dict:
    urls = parse_pool_text(text)
    res = db.proxy_add_many(urls)
    res["parsed"] = len(urls)
    return res


def replace_from_text(text: str) -> dict:
    urls = parse_pool_text(text)
    res = db.proxy_replace_all(urls)
    res["parsed"] = len(urls)
    return res


# ──────────────────────── 配置 ────────────────────────


def threshold() -> int:
    return max(1, _int(db.get_setting("proxy_409_threshold", "2"), 2))


def freeze_minutes() -> int:
    return max(1, _int(db.get_setting("proxy_freeze_minutes", "20"), 20))


def attempts_limit() -> int:
    return max(1, _int(db.get_setting("invite_max_attempts", "3"), 3))


# ──────────────────────── 409 判定 ────────────────────────


def is_409_error(err) -> bool:
    """本项目里「409 信号」的两种来源：

      - `flow.warmup()` 预检查失败：错误文案含「继续注册必然 409 invalid_state」；
      - 真实 HTTP 409：异常文案里带 409。

    （auth_flow 的 `classify_error` 也已把 `invalid_state` 归为 network。）
    """
    s = str(err or "").lower()
    return "409" in s or "invalid_state" in s


# 代理本身不可达/拒绝隧道（≠ OpenAI 业务 409）。这些是把"代理坏了"误判成
# "账号不可用"的元凶：curl 7 = 连不上、CONNECT tunnel failed, response 403/407
# = 代理拒绝隧道（鉴权/配额/目标被策略拦）、28 = 超时、35/56 = TLS/收包中断。
_TRANSPORT_MARKERS = (
    "connect tunnel failed", "response 403", "response 407", "curl: (7)", "curl: (28)",
    "curl: (35)", "curl: (56)", "failed to connect", "connection refused",
    "proxy error", "tunnel connection failed", "timed out", "timeout",
    # SOCKS 握手/隧道故障：不认这些，一个坏掉的 SOCKS5 代理永远不会被冻结，
    # 会被 least-used 轮换反复挑中，然后一直失败。
    "curl: (97)", "socks", "socks closed", "socks handshake",
    "超时",          # codex CLI 一轮跑不完（实测 71.9s 未完成）同样是代理不可用
)


def is_transport_error(err) -> bool:
    """是不是**代理/网络层**的问题（而不是 OpenAI 的业务拒绝）。

    区分这两种的意义：传输错误应当**换代理重试**，绝不能下"账号要重注册"的结论；
    而且必须记到代理账上，否则坏代理会被 least-used 轮换反复挑中。
    """
    text = str(err or "").lower()
    if not text:
        return False
    return any(marker in text for marker in _TRANSPORT_MARKERS)


# ──────────────────────── 选择 / 记账 ────────────────────────


# 两个用途维度：'register' = GPT 注册（warmup 预检），'codex' = Codex 激活。
PURPOSES = ("register", "codex")


def normalize_purpose(purpose) -> str:
    return db.normalize_proxy_purpose(purpose)


def pick(exclude: Optional[list] = None, purpose: str = "codex",
         exclude_bound_to: Optional[str] = None,
         allow_shared: bool = True) -> Optional[dict]:
    """按用途选一个可用代理（least-used），并原子自增 use_count。

    purpose 必须区分：注册锁冻掉的代理，不该阻止 Codex 激活继续使用它。

    独占：一条代理 = 一个出口 IP = 同一时刻只服务一个号（详见 db.proxy_pick）。
      · exclude_bound_to=<邮箱>：不抢别的号占着的那条，但自己那条还能拿回来；
      · allow_shared=False：严格模式，被占用的全不发（补号用）。
    """
    return db.proxy_pick(exclude, purpose, exclude_bound_to=exclude_bound_to,
                         allow_shared=allow_shared)


def release_use(ident) -> None:
    """这次尝试还没真正发起网络请求就结束了 → 回退 use_count。"""
    db.proxy_release_use(ident)


def record_success(ident, purpose: str = "codex") -> None:
    db.proxy_record_success(ident, purpose)


def record_fail(ident, error: str = "", purpose: str = "codex") -> None:
    db.proxy_record_fail(ident, error, purpose)


def record_transport_failure(ident) -> dict:
    """代理**自身不可用**（连不上 / CONNECT 被拒 / 超时）→ 记健康维度。

    连续 2 次 → 冻结 20 分钟（从此 pick 跳过它，于是"切换代理"真正生效）；
    解冻后再 1 次 → 永久不可用。所有代理都不可用时调用方应停下并显示「代理不可用」。
    """
    res = db.proxy_record_transport(ident, threshold=threshold(), freeze_minutes=freeze_minutes())
    if res.get("action") == "freeze":
        logger.warning("[proxy] %s 连续 %d 次不可用（连不上/隧道被拒），冻结 %d 分钟",
                       ident, threshold(), freeze_minutes())
    elif res.get("action") == "permanent":
        logger.warning("[proxy] %s 解冻后仍不可用，标记为永久不可用（需人工重置）", ident)
    return res


def record_409(ident, purpose: str = "codex") -> dict:
    """记一次 409，按阈值自动冻结 / 该维度永久拉黑。

    规则（两个维度各自独立）：连续 2 次 → 冻结 20 分钟；解冻后再 1 次 → 永久不可用。
    返回 {"action": count|freeze|permanent, "purpose": register|codex}。
    """
    key = normalize_purpose(purpose)
    res = db.proxy_record_409(
        ident, purpose=key, threshold=threshold(), freeze_minutes=freeze_minutes()
    )
    label = "注册" if key == "register" else "激活"
    if res.get("action") == "freeze":
        logger.warning("[proxy] %s 连续 %d 次 409（%s），冻结 %d 分钟",
                       ident, threshold(), label, freeze_minutes())
    elif res.get("action") == "permanent":
        logger.warning("[proxy] %s 解冻后再次 409（%s），标记该用途永久不可用", ident, label)
    return res


def reset(ident, purpose: str = "both") -> bool:
    return db.proxy_reset(ident, purpose)


def available_count(exclude: Optional[list] = None, purpose: str = "codex") -> int:
    return db.proxy_available_count(exclude, purpose)


def has_available(exclude: Optional[list] = None, purpose: str = "codex") -> bool:
    return available_count(exclude, purpose) > 0


def summary() -> dict:
    return db.proxy_summary()

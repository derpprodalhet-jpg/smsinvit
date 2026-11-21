"""Codex CLI「say hi」：跑一个真实回合，跑通才算流程成功。

为什么以它为准（主人定的）：Codex 授权拿到 RT 只证明拿到了令牌，
账号能不能**真的用**要看 CLI 能不能跑完一个完整回合。所以判据是：

    turn.started → item.completed(agent_message 且文本非空) → turn.completed
    全程没有 turn.failed / error，且未超时、退出码 0

CLI 结束（无论成败）都要回读 auth.json —— Codex 会轮换令牌，轮换后的
凭据必须落库，否则下次还得重跑。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
# 真正启动 codex 的是 codex_turn：那条路径**故意**使用标准库 subprocess ——
# 沙箱网络策略只模拟 TCP，会把 codex 自带解析器的 DNS 直接拒掉（见 codex_turn 头注释）。
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from http_client import normalize_proxy  # noqa: E402

logger = logging.getLogger("codex_cli")

DEFAULT_HOME_ROOT = Path(os.environ["CODEX_HOMES_ROOT"])

# 服务端拒绝模型名的两种口径：`The 'gpt-5.6-sol' model is not supported when using
# Codex with a ChatGPT account.` / `model_not_found`。命中才做默认模型兜底。
_MODEL_UNSUPPORTED = re.compile(r"model is not supported|model_not_found", re.I)


def parse_turn(stdout: str) -> dict:
    """解析 Codex CLI 的 --json 事件流，判断回合是否完整。"""
    started = replied = completed = failed = False
    last_text = ""
    for line in str(stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or "")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if etype == "turn.started":
            started, completed, replied = True, False, False
        elif etype == "item.completed":
            if str(item.get("type") or "") == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    replied = True
                    last_text = text[:200]
        elif etype == "turn.completed":
            completed = started and replied
        elif etype in ("turn.failed", "error"):
            failed = True
    return {
        "started": started,
        "replied": replied,
        "completed": completed,
        "failed": failed,
        "strict_ok": bool(started and replied and completed and not failed),
        "last_text": last_text,
    }


def _settings(overrides: Optional[dict] = None) -> dict:
    from . import codex_turn, db

    safe = codex_turn.DEFAULT_SAYHI_MODEL
    out = {
        "codex_binary": db.get_setting("codex_binary", "codex") or "codex",
        # 默认值照本项目 webui/codex_turn.py（它是实跑确认的：内容「你好」）
        "sayhi_prompt": db.get_setting("sayhi_prompt", "你好") or "你好",
        "sayhi_model": db.get_setting("sayhi_model", safe) or safe,
        "codex_originator": db.get_setting("codex_originator", "codex_work_desktop") or "codex_work_desktop",
        "cli_timeout": db.get_setting("cli_timeout", "180") or "180",
        # Keep the empty/default distinction: an explicit custom root must be a
        # private child, while the deployment default creates tasks under root.
        "codex_home_root": db.get_setting("codex_home_root", "") or "",
    }
    out.update({k: v for k, v in (overrides or {}).items() if v not in (None, "")})
    return out


def build_command(settings: dict) -> list[str]:
    """CLI 命令。--ignore-user-config + 私有 CODEX_HOME = 不沾宿主配置。"""
    cmd = [
        str(settings.get("codex_binary") or "codex"),
        "exec", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral", "--json",
        "-c", 'model_provider="openai"',
        "-c", 'approval_policy="never"',
        "-c", 'model_reasoning_effort="low"',
        "--sandbox", "read-only",
    ]
    model = str(settings.get("sayhi_model") or "").strip()
    if model:
        cmd += ["-m", model]
    cmd.append(str(settings.get("sayhi_prompt") or "say hi"))
    return cmd


def write_auth_json(home: Path, creds: dict) -> dict:
    """按 Codex CLI 的 auth.json 结构写入私有 CODEX_HOME。"""
    tokens = {
        "access_token": str(creds.get("access_token") or "").strip(),
        "refresh_token": str(creds.get("refresh_token") or "").strip(),
        "id_token": str(creds.get("id_token") or "").strip(),
        "account_id": str(creds.get("account_id") or "").strip(),
    }
    missing = [k for k in ("access_token", "refresh_token", "account_id") if not tokens[k]]
    if missing:
        raise RuntimeError(f"CODEX_CREDENTIALS_MISSING: 缺 {','.join(missing)}")
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except Exception:  # noqa: BLE001
        pass
    data = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": tokens,
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    auth = home / "auth.json"
    auth.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(auth, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return data


def read_rotated_tokens(home: Path) -> dict:
    """回读 CLI 轮换后的 tokens（读不到就返回空 dict）。"""
    auth = home / "auth.json"
    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    toks = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(toks, dict):
        return {}
    return {
        "access_token": str(toks.get("access_token") or "").strip(),
        "refresh_token": str(toks.get("refresh_token") or "").strip(),
        "id_token": str(toks.get("id_token") or "").strip(),
        "account_id": str(toks.get("account_id") or "").strip(),
    }


def run_say_hi(
    job_id: str,
    creds: dict,
    *,
    proxy: str = "",
    settings: Optional[dict] = None,
    keep_home: bool = False,
) -> dict:
    """跑一个 Codex CLI 回合（**照本项目 webui/codex_turn.py 的逻辑**）。

    本项目已经实跑确认（见 codex_turn.py 模块头）：
      · referral_context 的 redemption_action = codex_turn → 必须真跑一轮 codex，
        只拿 RT 不算兑现（邀请人那边一直是「待处理」）；
      · 必须是真 CLI 跑出来的一轮；
      · **客户端身份必须对**：CODEX_INTERNAL_ORIGINATOR_OVERRIDE=codex_work_desktop；
      · **出口 IP 必须对**：给子进程注入本次 run 的代理；
      · stdin 必须 /dev/null，否则 codex 会停在等 stdin；
      · 成功需开始事件、非空助手回复、完成事件、退出码 0，且无失败事件；
      · 无论成败读回 auth.json，四项轮换凭据均交给调用方回写。

    模型兜底：ChatGPT 账号只接受部分模型（实测 `gpt-5.6-sol`、`gpt-5.4` 等直接
    400 `model is not supported`）。模型名写错或被下线**不该让一次兑换失败** ——
    命中这个错误时用 `codex_turn.DEFAULT_MODEL`（gpt-5.5）再跑一次，结果里写明
    `attempts` / `fallback_model`。其它错误照旧直接失败，不用重试掩盖真问题。
    """
    from . import codex_turn

    cfg = _settings(settings)
    primary = str(cfg.get("sayhi_model") or codex_turn.DEFAULT_SAYHI_MODEL)
    res = _run_turn(job_id, creds, proxy=proxy, cfg=cfg)
    # 凭据账本：单轮只返回**这一轮变化过**的非空值，所以"返回空"不等于"没有轮换"。
    # 每一轮的变化都要并进账本（同名字段取较新一轮），否则模型回退一旦重跑，
    # 第一轮刷新的 refresh_token 就被第二轮的返回值盖掉，最后拿旧值存库 ——
    # 而旧 RT 在轮换那一刻就已经作废了。
    ledger = {k: v for k, v in (res.get("tokens") or {}).items() if v}
    unreadable = bool(res.get("tokens_unreadable"))
    attempts = [primary]
    fallback = ""
    if (not res.get("ok") and primary != codex_turn.DEFAULT_MODEL
            and _MODEL_UNSUPPORTED.search(str(res.get("reason") or ""))):
        fallback = codex_turn.DEFAULT_MODEL
        logger.warning("[codex_cli] 模型 %s 不被支持（%s）→ 用默认模型 %s 重跑一次",
                       primary, str(res.get("reason") or "")[:160], fallback)
        # 两次尝试共用同一份 CLI 预算：第一次已经花掉的时间不能白送第二轮，
        # 否则"整段 CLI 不超预算"只是注释里的一句话。
        budget = float(cfg.get("cli_timeout") or codex_turn.DEFAULT_TIMEOUT)
        remaining = max(min(30.0, budget), budget - float(res.get("elapsed") or 0))
        res = _run_turn(job_id, {**creds, **ledger}, proxy=proxy,
                        cfg={**cfg, "sayhi_model": fallback, "cli_timeout": remaining})
        attempts.append(fallback)
        ledger.update({k: v for k, v in (res.get("tokens") or {}).items() if v})
        unreadable = unreadable or bool(res.get("tokens_unreadable"))
    out = dict(res)
    out["tokens"] = ledger
    out["tokens_unreadable"] = unreadable
    out["attempts"] = attempts
    out["fallback_model"] = fallback if (fallback and res.get("ok")) else ""
    return out


def _run_turn(job_id: str, creds: dict, *, proxy: str, cfg: dict) -> dict:
    """跑**一个**模型的一轮 CLI（run_say_hi 的单次实现，便于兜底时重跑）。"""
    from . import codex_turn

    overrides = {
        "CODEX_REDEEM_PROMPT": str(cfg.get("sayhi_prompt") or codex_turn.DEFAULT_PROMPT),
        "CODEX_REDEEM_MODEL": str(cfg.get("sayhi_model") or codex_turn.DEFAULT_SAYHI_MODEL),
        "CODEX_REDEEM_TIMEOUT": str(int(float(cfg.get("cli_timeout") or codex_turn.DEFAULT_TIMEOUT))),
        "CODEX_REDEEM_ORIGINATOR": str(cfg.get("codex_originator") or codex_turn.DEFAULT_ORIGINATOR),
    }
    if str(cfg.get("codex_binary") or "").strip():
        overrides["CODEX_BIN"] = str(cfg["codex_binary"]).strip()
    if str(cfg.get("codex_home_root") or "").strip():
        overrides["CODEX_HOMES_ROOT"] = str(cfg["codex_home_root"]).strip()

    email = str(creds.get("email") or "").strip().lower() or f"job-{job_id}"
    t0 = time.time()
    res = codex_turn.run_codex_turn(
        email=email,
        access_token=str(creds.get("access_token") or ""),
        refresh_token=str(creds.get("refresh_token") or ""),
        id_token=str(creds.get("id_token") or ""),
        proxy=normalize_proxy(proxy) if proxy else "",
        overrides=overrides,
    )
    rotated = {}
    for name in ("access_token", "refresh_token", "id_token", "account_id"):
        if res.get(name):
            rotated[name] = res[name]

    return {
        "ok": bool(res.get("ok")),
        "reason": "" if res.get("ok") else str(res.get("error") or "CLI 回合未完成"),
        "reply": str(res.get("reply") or ""),
        "turn": {"reply": str(res.get("reply") or ""), "usage": res.get("usage") or {}},
        # 调用方约定：tokens 里放**被轮换过**的凭据（没有就是不轮换，保持原值）
        "tokens": rotated,
        # auth.json 写坏/读不出来时，rotated 会是空 —— 那不等于"没轮换"。
        "tokens_unreadable": bool(res.get("tokens_unreadable")),
        "exit_code": None,
        "timed_out": "超时" in str(res.get("error") or ""),
        "elapsed": round(time.time() - t0, 1),
        "stdout_tail": "",
        "home": str(res.get("home") or ""),
        "model": overrides["CODEX_REDEEM_MODEL"],
    }

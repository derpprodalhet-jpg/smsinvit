"""Codex 邀请的「兑现」：用该账号真跑一轮 codex，让邀请真正生效。

2026-10-06 本机实跑确认的结论
------------------------------
1. 邀请信 referral_context 里 `redemption_action = codex_turn` —— 服务端要求被邀请
   账号**真用一次 Codex**。只拿到 refresh_token 不算兑现：邀请人那边的列表一直是
   「待处理」，被邀请号自己的奖励位（rate-limit-reset-credits 等）也一直是空的。
2. 必须是**真 codex CLI** 跑出来的一轮（手搓 HTTP 请求不算）。
3. **客户端身份要对**：CLI 默认身份是 `codex_exec` / `codex-tui`，服务端不认；
   把 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE` 设成 `codex_work_desktop`
   （Codex 桌面 App 的身份）后，同样一轮 `codex exec` 立刻被认账
   —— 邀请人列表从「待处理」变成已完成（当天实测）。
4. **出口 IP 要对**：codex 子进程必须注入本次 run 用的代理（HTTP(S)_PROXY），
   否则会从服务器本机 IP 出去；同一个 token 换地区 IP 有被判 token_invalidated 的风险。

实现要点
--------
- 每个账号一个独立 `CODEX_HOME`：`<数据目录>/codex-homes/<邮箱>/`，绝不碰 `~/.codex`。
- 子进程 stdin 必须接 DEVNULL，否则 codex 会停在 "Reading additional input from stdin..."。
- 成功判据与 LocalFlow 副本一致：`--json` 里出现非空的 `agent_message` 回复即算
  兑现，超时不算；其余 `error` / `turn.failed` 事件只用于失败诊断。
- 子进程按 LocalFlow 副本的方式**直接运行**并注入本次 run 的代理。codex 自带
  hickory DNS 解析器，发 DNS 必须先建 UDP 套接字；放进只模拟 TCP 的沙箱网络代理
  会被 EPERM 直接拒绝，导致所有请求在发出前失败。
- 跑完读回 `auth.json`：codex 可能刷新并轮换 refresh_token，有新值交给调用方回写。
- 跑完清理 session/state/queue/logs 等，只留 `auth.json` + `config.toml`（钥匙 600）。
- **父进程被强杀后子进程可能残留**：本模块用 `start_new_session` 把 codex 放进独立
  进程组，正常退出、提前收工与超时都会 `killpg` 整组回收；但父进程自己被 SIGKILL
  时没有任何清理代码会执行，同组的 codex 会继续跑完。收敛边界是容器（PID namespace
  随容器销毁，`pids_limit` 兜住数量），因此不引入 `preexec_fn`（多线程下不安全）或
  常驻监督进程。运维动作：内存超限/进程被杀之后，先确认没有 codex 残留再放新任务。

可用环境变量覆盖
----------------
- `CODEX_BIN`              指定 codex 可执行文件（最优先）
- `CODEX_REDEEM_MODEL`     模型，默认 gpt-5.5（gpt-6-sol 等对 ChatGPT 账号不被支持）
- `CODEX_REDEEM_PROMPT`    发的内容，默认「你好」
- `CODEX_REDEEM_TIMEOUT`   单轮超时秒数，默认 180
- `CODEX_REDEEM_ORIGINATOR` 身份覆盖值，默认 codex_work_desktop
- `CODEX_TURN_LEAN`        1 = 追加减开销配置（关掉这一轮用不到的特性、钉死
  workspace id、可配本地模型目录）—— 实验档，默认关，实测有效才启用
- `CODEX_TURN_MODEL_CATALOG` 本地模型目录 JSON 路径（lean 档用，文件不存在就不写）
"""
from __future__ import annotations

import base64
from contextvars import ContextVar
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("codex_turn")

# 服务端确认可用、且作为"模型被拒"时兜底的模型。**不要**把它改成没实测过的名字：
# 兜底必须是一个 ChatGPT 账号一定能跑的模型。
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_PROMPT = "你好"
DEFAULT_TIMEOUT = 180
DEFAULT_ORIGINATOR = "codex_work_desktop"
# 「say hi」这一轮的默认执行模型。留空 `sayhi_model` 设置时用它；换它必须先在真账号
# 上做出 ok 率 100% 的矩阵（见 docs/validation/2026-10-09-cli-turn-speed.md）。
DEFAULT_SAYHI_MODEL = "gpt-5.6-luna"

# 只保留这两个文件，其余（sessions/state/queue/logs/cache/plugins…）跑完清掉
_KEEP_FILES = {"auth.json", "config.toml"}

# CODEX_TURN_LEAN=1 时额外关掉的特性：这一轮只发一句话，这些既用不到、又会把工具表
# 和说明塞进 Codex 自带的 ~10k token 提示里（生产实测 in≈10039）。名字取自镜像里的
# `codex features list`（stable/experimental）；**保留 shell / unified_exec /
# apply_patch**，确保服务端仍认这是"真跑一轮 codex"。默认关，实测有效才启用。
LEAN_FEATURES = (
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "in_app_browser", "in_app_chat", "in_app_dictation",
    "in_app_local_automation", "in_app_updates", "goals", "hooks", "worktrees",
    "workspace_dependencies", "image_generation", "realtime_conversation",
    "multi_agent", "sleep_tool", "tool_suggest", "mentions_v2", "plugin_sharing",
    "skill_mcp_dependency_install", "skill_search",
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """打包版 = 可执行文件同级目录；源码版 = 项目根目录（与 runtime_paths 一致）。"""
    try:
        from runtime_paths import data_dir as _dd  # type: ignore
        return Path(_dd())
    except Exception:
        return _project_root()


def homes_root() -> Path:
    # An empty setting preserves the deployment root; custom settings can only
    # choose a child of that root, which is a separately bounded runtime mount.
    from smsinvit.invitation.process import managed_home_directory, managed_homes_root
    custom = str(_OVERRIDES.get().get("CODEX_HOMES_ROOT") or "").strip()
    if custom:
        return managed_home_directory(custom)
    return managed_homes_root()


# codex_invit：设置来自数据库（不是环境变量），所以支持 overrides 覆盖。
_OVERRIDES = ContextVar("invitation_cli_overrides", default={})


def _env_flag_str(name: str, default: str) -> str:
    v = str(_OVERRIDES.get().get(name) or "").strip() or (os.getenv(name) or "").strip()
    return v or default


def _account_id_from_id_token(id_token: str) -> str:
    """Read the account claim from either an ID token or an access token."""
    try:
        payload = (id_token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        auth = claims.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
    except Exception:
        return ""


def find_codex_binary() -> str:
    """三级查找：CODEX_BIN -> 数据目录里自带的 -> PATH / 常见安装位置。"""
    env = _env_flag_str("CODEX_BIN", "")
    if env and Path(env).exists():
        return env

    names = ["codex.exe", "codex.cmd", "codex"] if os.name == "nt" else ["codex"]
    candidates: list[Path] = []
    for base in (data_dir() / "codex", data_dir() / "codex" / "bin",
                 data_dir() / "node_modules" / ".bin", _project_root() / "codex"):
        for n in names:
            candidates.append(base / n)
    for c in candidates:
        if c.is_file():
            return str(c)

    found = shutil.which("codex")
    if found:
        return found

    for extra in ("~/.local/bin/codex", "/usr/local/bin/codex", "/opt/homebrew/bin/codex"):
        p = Path(os.path.expanduser(extra))
        if p.is_file():
            return str(p)
    return ""


def _env_truthy(name: str) -> bool:
    return str(_env_flag_str(name, "") or "").strip().lower() in ("1", "true", "on", "yes")


def _lean_config_lines(account_id: str) -> list[str]:
    """`CODEX_TURN_LEAN=1` 时的减开销配置（默认关，实测有效才启用）。

    依据：codex 二进制自身启动只要 0.02s，而生产 `启动` 段是 4.7-5.1s —— 那是启动期
    的网络调用（模型目录刷新、workspace routing discovery；直连时降到 2.9s）。这里把
    已知的两件事钉死：workspace 直接给 auth.json 里的 account id（不再 discovery），
    模型目录用本地 JSON（`CODEX_TURN_MODEL_CATALOG` 指向的文件存在才写）。
    """
    lines = ["check_for_update_on_startup = false"]
    catalog = _env_flag_str("CODEX_TURN_MODEL_CATALOG", "")
    if catalog and Path(catalog).is_file():
        lines.append("model_catalog_json = " + json.dumps(catalog, ensure_ascii=False))
    # account_id 来自 JWT claim，写进 TOML 前必须严格校验形状（与模型名同一注入面）。
    if re.fullmatch(r"[0-9a-fA-F-]{8,64}", str(account_id or "")):
        lines.append("forced_chatgpt_workspace_id = " + json.dumps(account_id, ensure_ascii=False))
    return lines


def _write_home(email: str, access_token: str, refresh_token: str,
                id_token: str) -> Path:
    """建/刷新这个账号的 CODEX_HOME。"""
    root = homes_root().resolve()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    home = Path(tempfile.mkdtemp(prefix="task-",dir=root))
    from smsinvit.invitation.process import managed_home_directory
    managed_home_directory(home)
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except Exception:
        pass

    account_id = (_account_id_from_id_token(id_token)
                  or _account_id_from_id_token(access_token))
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
            "id_token": id_token,
        },
    }
    ap = home / "auth.json"
    ap.write_text(json.dumps(auth, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(ap, 0o600)
    except Exception:
        pass

    # 与实测成功时用的最小配置保持一致（模型名不要写 gpt-6-sol：
    # ChatGPT 账号会被服务端拒：The 'gpt-6-sol' model is not supported ...）
    settings = [
        "model_reasoning_effort = \"low\"",
        "approval_policy = \"never\"",
        "sandbox_mode = \"read-only\"",
    ]
    features = ["apps = false", "plugins = false", "remote_plugin = false"]
    if _env_truthy("CODEX_TURN_LEAN"):
        settings += _lean_config_lines(account_id)
        features += [f"{name} = false" for name in LEAN_FEATURES]
    (home / "config.toml").write_text(
        'model = ' + json.dumps(_env_flag_str("CODEX_REDEEM_MODEL", DEFAULT_SAYHI_MODEL), ensure_ascii=False) + '\n'
        + "\n".join(settings) + '\n'
        + '[features]\n'
        + "\n".join(features) + '\n',
        encoding="utf-8",
    )
    return home

def _read_home_tokens(home: Path) -> dict:
    try:
        d = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        t = d.get("tokens") or {}
        from smsinvit.invitation.mysql import remember_secrets
        names = ("access_token", "refresh_token", "id_token", "account_id")
        remember_secrets(t.get(name) for name in names)
        return {name: str(t.get(name) or "") for name in names}
    except Exception:
        return {}


def _home_tokens_unreadable(home: Path) -> bool:
    """auth.json 存在、但解析不出可用的 tokens（写一半 / 被提前杀掉）。

    与"这一轮没有轮换"必须区分：两者在 `_read_home_tokens` 眼里都返回空，
    但前者意味着我们**不知道**新凭据是什么，绝不能当成"没变"。
    """
    path = home / "auth.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False                      # 文件不在/读不到：不是"写坏了"
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return True
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return True
    return not any(str(tokens.get(name) or "").strip()
                   for name in ("access_token", "refresh_token", "id_token", "account_id"))


def _cleanup_home(home):
    # Rotated tokens have already been read into the encrypted repository.
    # A private task directory must not retain plaintext credentials on disk.
    shutil.rmtree(home,ignore_errors=True)


def _log_turn_breakdown(log, marks: dict, usage: dict, elapsed: float) -> None:
    """一行说清这一轮慢在哪：启动 / 模型 / 收尾 + token 用量 + WS 重试次数。

    实测（2026-10-08，gpt-5.5 跑「你好」）：启动 3.2s、模型 9.7s、收尾 4.4s，
    in≈13k/out≈30/reasoning=0 —— 所以"慢"主要在固定开销与 Codex 自带的长提示，
    换模型或减输入都拿不到可靠收益；这一行就是给主人看这件事的。
    """
    if not marks:
        return
    def sec(key):
        value = marks.get(key)
        return (int(value) / 1000.0) if value is not None else None
    thread_ms, turn_ms, done_ms, exit_ms = sec("thread_ms"), sec("turn_ms"), sec("completed_ms"), sec("exit_ms")
    startup = thread_ms
    model = (done_ms - turn_ms) if (done_ms is not None and turn_ms is not None) else None
    teardown = (exit_ms - done_ms) if (exit_ms is not None and done_ms is not None) else None
    used = usage or marks.get("usage") or {}
    log.info(
        "[codex_turn] 明细: 启动 %s / 模型 %s / 收尾 %s / 合计 %.1fs / in=%s out=%s reason=%s / WS重试 %s",
        f"{startup:.1f}s" if startup is not None else "-",
        f"{model:.1f}s" if model is not None else "-",
        f"{teardown:.1f}s" if teardown is not None else "-",
        elapsed,
        used.get("input_tokens", "-"),
        used.get("output_tokens", "-"),
        used.get("reasoning_output_tokens", "-"),
        marks.get("ws_retries", 0),
    )


def _parse_turn(stdout: str) -> dict:
    """Read assistant output and terminal failures, independently of stderr noise."""
    reply = ""
    usage: dict = {}
    failed = False
    started = completed = replied = False
    error = ""
    turn_error = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if not isinstance(o, dict):
            continue
        t = o.get("type")
        if t == "turn.started":
            started, completed, replied = True, False, False
        elif t == "item.completed":
            item = o.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    reply = text.strip()
                    replied = True
        elif t == "turn.completed":
            completed = started and replied
            if isinstance(o.get("usage"), dict):
                usage = o["usage"]
        elif t in ("error", "turn.failed"):
            failed = True
            detail = o.get("error")
            message = detail.get("message") if isinstance(detail, dict) else detail
            if not isinstance(message, str) or not message.strip():
                message = o.get("message")
            if isinstance(message, str) and message.strip():
                if t == "turn.failed":
                    turn_error = message.strip()
                else:
                    error = message.strip()
    return {"reply": reply, "usage": usage, "failed": failed, "error": turn_error or error,
            "completed": bool(started and replied and completed and not failed)}


def _parse_reply(stdout: str) -> tuple[str, dict]:
    parsed = _parse_turn(stdout)
    return parsed["reply"], parsed["usage"]


def _event_summary(stdout: str, limit: int = 40) -> str:
    """Ordered JSONL events with redacted failure messages, so a failed turn can
    be reconstructed safely without exposing URLs, credentials or tokens."""
    parts: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        label = event["type"]
        if label in ("error", "turn.failed"):
            detail = event.get("error")
            message = detail.get("message") if isinstance(detail, dict) else detail
            if not isinstance(message, str) or not message.strip():
                message = event.get("message")
            if isinstance(message, str) and message.strip():
                label += ": " + _bounded_evidence(message, 200)
        parts.append(label)
    return " | ".join(parts[:limit]) or "无 JSON 事件"


def _bounded_evidence(message: str, limit: int = 1200) -> str:
    from smsinvit.invitation.safety import redact
    # CLI errors quote request/proxy URLs; hosts and credentials must not reach
    # the log, so the same URL strip as the reported diagnostic is applied.
    message = re.sub(r"(?i)(?:https?|socks5h?)://[^\s\"'<>]+", "[URL]", redact(message))
    return " ".join(message.split())[:limit]


def _safe_diagnostic(message: str) -> str:
    from smsinvit.invitation.safety import redact
    # CLI errors can quote request/proxy URLs. The category/message is useful;
    # the URL (including credentials or query values) is not needed here.
    message = redact(message)
    message = re.sub(r"(?i)(?:https?|socks5h?)://[^\s\"'<>]+", "[URL]", message)
    return " ".join(message.split())[:400]


def access_token_stale(access_token: str, *, skew: int = 30) -> bool:
    """access_token 是否已经过期（只看本地 exp，不发网络请求）。

    为什么要这个：凭据陈旧时 codex 会**先打 7 次 WebSocket 重试**（实测
    1.8→17.7s）再失败 —— 每次白烧 ~18s，还会连带浪费代理与接码预算。
    本地就能判定过期的，直接不启动 CLI。
    """
    token = str(access_token or "").strip()
    if token.count(".") != 2:
        return False                       # 形状不认识 → 不拦，交给 CLI 自己判
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = float(claims.get("exp") or 0)
    except Exception:  # noqa: BLE001
        return False
    return bool(exp) and exp <= time.time() + max(0, int(skew))


def _output_text(output) -> str:
    return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")


# 回复到手后，最多再等这么久让 CLI 把 auth.json（可能轮换过的凭证）刷完。
# 生产实测：turn.completed 之后还要等 4.4s（负载高时 9.9s）才退出 —— 这段时间
# 纯粹白等，回复早就解析完了。给 1.2s 宽限足够覆盖写盘，之后立刻收工。
EARLY_EXIT_GRACE_SECONDS = float(os.environ.get("CODEX_TURN_EARLY_EXIT_GRACE", "1.2"))
# 与受控子进程路径（process.MAX_CHILD_OUTPUT）同一口径：这条路直连 codex，
# 没有输出上限就没法保证一个刷日志的 CLI 不会把内存吃掉。
MAX_TURN_OUTPUT = 4 * 1024 * 1024


def _note_cli_event(line: str, at: float, marks: dict) -> None:
    """记录 codex 事件流里的关键时间点（用于耗时分解）。"""
    text = (line or "").strip()
    if not text:
        return
    if "responses_websocket" in text:
        marks["ws_retries"] = int(marks.get("ws_retries") or 0) + 1
        marks.setdefault("first_ws_ms", int(at * 1000))
        marks["last_ws_ms"] = int(at * 1000)
    if not text.startswith("{"):
        return
    try:
        event = json.loads(text)
    except Exception:  # noqa: BLE001
        return
    kind = event.get("type")
    if kind == "thread.started":
        marks.setdefault("thread_ms", int(at * 1000))
    elif kind == "turn.started":
        marks.setdefault("turn_ms", int(at * 1000))
    elif kind == "item.completed":
        marks.setdefault("item_ms", int(at * 1000))
        item = event.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message":
            marks.setdefault("reply_ms", int(at * 1000))
    elif kind == "turn.completed":
        marks["completed_ms"] = int(at * 1000)
        usage = event.get("usage")
        if isinstance(usage, dict):
            marks["usage"] = usage
    elif kind == "turn.failed":
        marks["failed_ms"] = int(at * 1000)


def _run_codex_process(cmd, *, env, cwd, timeout, **kwargs):
    """Run one codex turn as an ordinary child process.

    Returns ``(stdout, stderr, returncode, timed_out, marks)``；marks 里是分段耗时
    （启动/模型/收尾）与 token 用量。以前用 communicate() 一次性收输出，事件时间
    全丢，出事只能看到"一轮 33.5s"，无法判断慢在启动、模型还是收尾。
    """
    handle = None
    timed_out = False
    marks: dict = {}
    out_lines: list = []
    err_lines: list = []
    # 这条路径直连 codex，不走受控的 process.run_child，所以输出上限必须自己兜：
    # 没有上限时一个刷日志的 CLI 能把整轮输出堆进内存。
    guard = {"oversized": False}
    started = time.time()
    try:
        handle = subprocess.Popen(
            cmd, env=env, cwd=cwd,
            stdin=subprocess.DEVNULL,             # 关键：否则 codex 等 stdin 挂住
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=True, **kwargs,
        )

        def pump(stream, sink):
            # 两个流都要看：codex 的 JSON 事件在 stdout，而
            # `responses_websocket ... failed` 重试日志在 **stderr**
            # （只数 stdout 会把重试次数永远统计成 0）。
            total = 0
            try:
                for line in stream:
                    total += len(line.encode("utf-8", "replace"))
                    if total > MAX_TURN_OUTPUT:
                        guard["oversized"] = True
                        return
                    sink.append(line)
                    _note_cli_event(line, time.time() - started, marks)
            except Exception:  # noqa: BLE001
                pass

        readers = [
            threading.Thread(target=pump, args=(handle.stdout, out_lines), daemon=True),
            threading.Thread(target=pump, args=(handle.stderr, err_lines), daemon=True),
        ]
        for reader in readers:
            reader.start()
        # 轮询式等待：一旦"回复已解析 + turn.completed"就只留一点宽限时间抓轮换凭证，
        # 然后立即结束进程 —— 不再白等它的收尾（4-10s）。
        deadline = started + max(1, timeout)
        while True:
            if handle.poll() is not None:
                break
            if guard["oversized"]:
                marks["oversized"] = 1
                _kill_group(handle)
                break
            completed = marks.get("completed_ms")
            if completed is not None and marks.get("reply_ms") is not None:
                if time.time() - (started + int(completed) / 1000.0) >= EARLY_EXIT_GRACE_SECONDS:
                    marks["early_exit"] = 1
                    _kill_group(handle)
                    break
            if time.time() >= deadline:
                timed_out = True
                _kill_group(handle)
                break
            time.sleep(0.05)
        try:
            handle.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for reader in readers:
            reader.join(timeout=5)
        marks["exit_ms"] = int((time.time() - started) * 1000)
        if guard["oversized"]:
            marks["oversized"] = 1
        return "".join(out_lines), "".join(err_lines), handle.returncode, timed_out, marks
    finally:
        if handle is not None:
            _kill_group(handle)


def _kill_group(handle) -> None:
    """Terminate the whole task group so no codex helper survives its turn.

    不因为"主进程已经退出"就提前返回：正常退出时同组的 codex 辅助进程可能还在，
    它们同样必须被回收。进程组已经消失时下面的异常分支会安静收场。
    """
    try:
        os.killpg(os.getpgid(handle.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            handle.kill()
        except OSError:
            pass


def run_codex_turn(
    *,
    email: str,
    access_token: str,
    refresh_token: str,
    id_token: str = "",
    proxy: str = "",
    log: Optional[logging.Logger] = None,
    overrides: Optional[dict] = None,
) -> dict[str, Any]:
    """用该账号真跑一轮 codex。返回：

        {"ok": bool, "reply": str, "error": str, "usage": dict,
         "access_token": str|None, "refresh_token": str|None,
         "id_token": str|None, "account_id": str|None, "home": str}

    四项凭据只在**被 codex 轮换过**时才返回（调用方据此回写）。
    """
    log = log or logger
    _OVERRIDES.set(dict(overrides or {}))
    email = (email or "").strip().lower()
    if not email:
        return {"ok": False, "error": "缺少邮箱", "reply": "", "usage": {}}
    if not (access_token or "").strip():
        return {"ok": False, "error": "缺少 access_token，无法跑 codex", "reply": "", "usage": {}}

    binary = find_codex_binary()
    if not binary:
        return {
            "ok": False,
            "error": "找不到 codex 可执行文件（可设 CODEX_BIN，或让程序首次运行时自动安装）",
            "reply": "", "usage": {},
        }

    # 凭据已过期就不要启动 CLI：否则 codex 会先打 7 次 WebSocket 重试
    # （实测 1.8→17.7s）再失败，每次白烧 ~18s，还连带浪费代理/接码预算。
    if access_token_stale(access_token):
        return {
            "ok": False, "reply": "", "usage": {}, "home": "",
            "error": "Codex access_token 已过期（未启动 CLI，避免 ~18s 无效重试）；"
                     "请在授权前刷新该账号凭据后再试",
        }

    model = _env_flag_str("CODEX_REDEEM_MODEL", DEFAULT_MODEL)
    prompt = _env_flag_str("CODEX_REDEEM_PROMPT", DEFAULT_PROMPT)
    originator = _env_flag_str("CODEX_REDEEM_ORIGINATOR", DEFAULT_ORIGINATOR)
    try:
        timeout = int(_env_flag_str("CODEX_REDEEM_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except Exception:
        timeout = DEFAULT_TIMEOUT

    home = _write_home(email, access_token.strip(), (refresh_token or "").strip(), id_token)
    before = _read_home_tokens(home)

    from smsinvit.invitation.safety import child_environment, require_binary
    require_binary(binary)
    env = child_environment()
    env.update({
        "CODEX_HOME": str(home),
        "SMSINVIT_TASK_HOME": str(home),
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": originator,
        # HOME 与 XDG_* 必须真的指到本任务的私有目录。这条路径不经过
        # process.SafeSubprocess（那里才把 SMSINVIT_TASK_HOME 翻译成 HOME），
        # 不显式设置的话子进程**根本没有 HOME**，os.path.expanduser("~") 会回退到
        # 真实家目录 —— 隔离形同虚设。
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    })
    # A private TMPDIR keeps codex scratch files inside the task directory.
    scratch = home / "tmp"
    scratch.mkdir(mode=0o700, exist_ok=True)
    env["TMPDIR"] = str(scratch)
    if proxy:
        from smsinvit.invitation.safety import require_proxy
        from urllib.parse import urlsplit
        require_proxy(proxy)
        # 按协议分流：SOCKS 代理塞进 HTTP_PROXY 会被只认 HTTP 代理的客户端误解析。
        # ALL_PROXY 是 SOCKS 的标准入口，保留它；http/https 才写 HTTP(S)_PROXY。
        if urlsplit(proxy).scheme.startswith("socks"):
            env.update({"ALL_PROXY": proxy, "all_proxy": proxy})
            # codex CLI（Rust 二进制）**不认 ALL_PROXY 里的 SOCKS**：实测它照样直连，
            # 于是启动期的「模型目录刷新 / workspace routing discovery」被 Cloudflare
            # 秒拒 403（同一时刻 Python 侧走同一代理是通的：直连 403 / 经代理 200），
            # 整轮以 `workspace routing discovery failed` 结束、退出码 1。
            #
            # 实测：该代理端口同时提供 HTTP 代理（http:// 同端口连通），把 HTTP 形式补进
            # HTTP(S)_PROXY 后，同一轮立刻成功（退出码 0，拿到 agent_message）。
            # 若某代理端口只支持 SOCKS，这里等价于多一次连不上的尝试，最坏与原本的直连
            # 同归失败，不会比改动前更差。
            http_form = "http://" + proxy.split("://", 1)[1]
            env.update({
                "HTTP_PROXY": http_form, "HTTPS_PROXY": http_form,
                "http_proxy": http_form, "https_proxy": http_form,
            })
        else:
            env.update({
                "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy,
                "http_proxy": proxy, "https_proxy": proxy, "all_proxy": proxy,
            })

    cmd = [binary, "exec", "-m", model, "--skip-git-repo-check", "--json", "--sandbox", "read-only", "-c", 'approval_policy="never"', "--ephemeral"]
    if os.name == "nt" and binary.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd          # npm 装的 codex 是 .cmd 壳
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW：别弹黑窗

    workdir = tempfile.mkdtemp(prefix="codex-turn-",dir=homes_root())
    cmd += ["-C", workdir, "--", prompt]
    t0 = time.time()
    log.info(
        "[codex_turn] 跑一轮兑现：%s（模型 %s，身份 %s，代理 %s）",
        email, model, originator, "有" if proxy else "无",
    )
    launch_error = ""
    timed_out = False
    returncode = None
    stdout = stderr = ""
    marks: dict = {}
    # LocalFlow-compatible launch: codex runs as an ordinary child with the run's
    # proxy injected. No kernel network broker is involved, so codex' own resolver
    # (hickory, UDP DNS) and WebSocket transport work normally.
    try:
        stdout, stderr, returncode, timed_out, marks = _run_codex_process(
            cmd, env=env, cwd=workdir, timeout=timeout, **kwargs,
        )
    except Exception as e:
        launch_error = f"启动 codex 失败: {type(e).__name__}: {e}"

    parsed = _parse_turn(stdout)
    reply, usage = parsed["reply"], parsed["usage"]
    elapsed = time.time() - t0
    _log_turn_breakdown(log, marks, usage, elapsed)

    # 钥匙可能被 codex 刷新/轮换
    after = _read_home_tokens(home)
    # `auth.json` 读不出来（写一半、被提前杀掉）与"这一轮没轮换"是两件事，
    # 绝不能混为一谈：混了就会拿旧凭据去覆盖刚轮换过的值。
    tokens_unreadable = _home_tokens_unreadable(home)
    if tokens_unreadable:
        log.warning("[codex_turn] auth.json 无法解析；本轮按“未观察到轮换”处理并保留原凭据")
    new_tokens = {}
    for name in ("access_token", "refresh_token", "id_token", "account_id"):
        if after.get(name) and after[name] != before.get(name):
            new_tokens[name] = after[name]
    if new_tokens:
        log.info("[codex_turn] codex 轮换了凭证，需要回写: %s", sorted(new_tokens))

    _cleanup_home(home)
    shutil.rmtree(workdir, ignore_errors=True)

    # 判据与 LocalFlow 副本及交付说明一致：拿到非空的 CLI agent_message 回复
    # 即视为这一轮兑现成功。附加的 error/turn.failed 事件只用于失败诊断，不
    # 能否决已经产生的回复（codex 会把重连提示也报成 error 事件）。
    oversized = bool(marks.get("oversized"))
    if reply and not timed_out and not launch_error and not oversized:
        log.info("[codex_turn] 兑现成功（%.1fs）：%s", elapsed, _safe_diagnostic(reply[:80]))
        return {"ok": True, "reply": reply, "usage": usage, "error": "",
                "home": str(home), "breakdown": marks,
                "tokens_unreadable": tokens_unreadable, **new_tokens}

    if oversized:
        err = f"codex 输出超过上限（{MAX_TURN_OUTPUT} 字节），本轮按失败处理（未判定代理问题）"
    elif timed_out:
        # 带上 ASCII "timeout"：代理池据此判传输失败 → 换代理。
        # 否则一个跑不完的代理会一直以"健康"身份被反复选中。
        err = f"codex 超时 timeout（{timeout}s，代理太慢或不可用）"
    elif launch_error:
        err = launch_error
    else:
        err = f"codex 没有产出回复（退出码 {returncode}，{elapsed:.0f}s）"
    if parsed["error"]:
        err += "；" + parsed["error"]
    elif not timed_out and not launch_error and not oversized:
        # Remote MCP setup logs are incidental to this one-message task. Use
        # JSONL terminal errors first; do not present MCP chatter as the cause.
        stderr_lines = [line for line in stderr.strip().splitlines()
                        if not re.search(r"(?i)(?:\bmcp\b|\brmcp\b|/ps/mcp)", line)]
        if stderr_lines:
            err += "；stderr: " + " | ".join(stderr_lines[-3:])
    err = _safe_diagnostic(err)
    log.error("[codex_turn] %s", err)
    if not timed_out and not launch_error:
        # The reported error stays free of incidental remote-MCP chatter, but the
        # complete redacted CLI evidence is kept in the log: without it a failed
        # turn is undiagnosable, and the CLI removes its own task directory.
        log.warning("[codex_turn] CLI 证据：stdout 事件=%s；stderr=%s",
                    _event_summary(stdout), _bounded_evidence(stderr.strip()) or "空")
    # 失败也要带上分段计时：否则"这一轮慢在哪"只能靠日志文字猜（sol 的 400、
    # terra 的 401 都发生在不同的阶段）。
    return {"ok": False, "error": err, "reply": "", "usage": usage,
            "home": str(home), "breakdown": marks,
            "tokens_unreadable": tokens_unreadable, **new_tokens}

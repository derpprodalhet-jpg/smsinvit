"""CLI 兑现回合的耗时明细与"陈旧 token 快速失败"。

实测背景（2026-10-08，真账号真跑）：
  · 凭据正确：传输+启动 1.8s（直连）/3.3s（走代理），WS 重试 **0** 次；
  · 一轮完整兑现：启动 ~3.2s + 模型 ~9.7s + 收尾 ~4.4s ≈ 17s，
    in≈13k tokens / out≈30 / reasoning=0；
  · 凭据陈旧：codex 会先打 **7 次 WebSocket 重试**（1.8→17.7s）再失败 —— 纯浪费。
所以这里要保证：① 三个阶段的耗时和 token 能打进日志；② 过期凭据不再启动 CLI。
"""
import json
import os
import sys
import time

import pytest

from webui import codex_turn


def _jwt(exp_delta: int) -> str:
    import base64

    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': int(time.time()) + exp_delta})}.sig"


def test_access_token_stale_detects_expiry_only():
    assert codex_turn.access_token_stale(_jwt(-10)) is True
    assert codex_turn.access_token_stale(_jwt(3600)) is False
    # 形状不认识就不拦（交给 CLI 自己判），绝不误杀
    assert codex_turn.access_token_stale("not-a-jwt") is False
    assert codex_turn.access_token_stale("") is False


def test_process_timings_capture_the_three_phases():
    """用假进程产出 codex 的 JSON 事件流，验证分段计时与 WS 重试计数。"""
    script = (
        "import json,sys,time\n"
        "def emit(**kw): print(json.dumps(kw, ensure_ascii=False), flush=True)\n"
        "time.sleep(0.05); emit(type='thread.started')\n"
        "time.sleep(0.05); emit(type='turn.started')\n"
        "print('ERROR responses_websocket: failed to connect', file=sys.stderr, flush=True)\n"
        "time.sleep(0.05)\n"
        "emit(type='item.completed', item={'type':'agent_message','text':'你好'})\n"
        "emit(type='turn.completed', usage={'input_tokens':13007,'output_tokens':26,'reasoning_output_tokens':0})\n"
        "time.sleep(0.05)\n"
    )
    out, err, code, timed_out, marks = codex_turn._run_codex_process(
        [sys.executable, "-c", script], env=dict(os.environ), cwd=None, timeout=30,
    )
    assert code == 0 and not timed_out and "你好" in out
    assert marks["ws_retries"] == 1
    for key in ("thread_ms", "turn_ms", "completed_ms", "exit_ms"):
        assert isinstance(marks.get(key), int), (key, marks)
    assert marks["turn_ms"] <= marks["completed_ms"] <= marks["exit_ms"]
    assert marks["usage"]["input_tokens"] == 13007


def test_breakdown_log_mentions_every_phase(caplog):
    import logging

    marks = {"thread_ms": 3200, "turn_ms": 3300, "completed_ms": 13900, "exit_ms": 19100,
             "ws_retries": 0, "usage": {"input_tokens": 12799, "output_tokens": 26,
                                        "reasoning_output_tokens": 0}}
    with caplog.at_level(logging.INFO):
        codex_turn._log_turn_breakdown(logging.getLogger("codex_turn"), marks, marks["usage"], 19.1)
    text = caplog.text
    assert "启动 3.2s" in text and "模型 10.6s" in text and "收尾 5.2s" in text
    assert "in=12799" in text and "WS重试 0" in text


def test_expired_token_never_launches_the_cli(monkeypatch):
    """过期凭据必须立刻失败：不能启动 CLI（否则白烧 ~18s 的 WS 重试）。"""
    def forbidden(*args, **kwargs):
        raise AssertionError("过期的 access_token 不得启动 CLI")

    monkeypatch.setattr(codex_turn, "_run_codex_process", forbidden)
    res = codex_turn.run_codex_turn(
        email="stale@example.test", access_token=_jwt(-3600), refresh_token="rt",
    )
    assert res["ok"] is False and "已过期" in res["error"]


def test_fresh_token_still_runs(monkeypatch, tmp_path):
    """没过期的凭据照常走 CLI 路径（只替换进程 seam，不碰真网络）。"""
    calls = []
    # workdir 也要建在受管的 homes root 下
    (tmp_path / "homes").mkdir()
    monkeypatch.setenv("CODEX_HOMES_ROOT", str(tmp_path / "homes"))

    def fake(cmd, *, env, cwd, timeout, **kwargs):
        calls.append(cmd)
        return ('{"type":"item.completed","item":{"type":"agent_message","text":"你好"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":10}}\n',
                "", 0, False, {"thread_ms": 1, "turn_ms": 2, "completed_ms": 3, "exit_ms": 4})

    monkeypatch.setattr(codex_turn, "_run_codex_process", fake)
    monkeypatch.setattr(codex_turn, "_write_home", lambda *a, **k: __import__("pathlib").Path("/tmp"))
    monkeypatch.setattr(codex_turn, "_read_home_tokens", lambda home: {})
    monkeypatch.setattr(codex_turn, "_cleanup_home", lambda home: None)
    monkeypatch.setattr(codex_turn, "find_codex_binary", lambda: "/bin/true")
    from smsinvit.invitation import safety
    monkeypatch.setattr(safety, "require_binary", lambda binary: None)
    monkeypatch.setattr(safety, "child_environment", lambda: {})
    res = codex_turn.run_codex_turn(
        email="ok@example.test", access_token=_jwt(3600), refresh_token="rt",
    )
    assert calls and res["ok"] is True and res["breakdown"]["exit_ms"] == 4


def test_cli_timeout_counts_as_a_transport_failure():
    """一个跑不完一轮的代理必须被记账换掉，不能一直"健康"被反复选中。"""
    from webui import proxy_pool

    assert proxy_pool.is_transport_error("codex 超时 timeout（60s，代理太慢或不可用）") is True
    assert proxy_pool.is_transport_error("超时") is True
    # 业务性失败不能误判成代理问题
    assert proxy_pool.is_transport_error("authorize/continue 失败: HTTP 409 invalid_state") is False


def test_the_cli_child_receives_a_private_home_and_xdg(monkeypatch, tmp_path):
    """这条路径不经过 SafeSubprocess，HOME/XDG 必须自己显式指到任务目录。

    旧实现只设 CODEX_HOME/SMSINVIT_TASK_HOME，子进程**根本没有 HOME**，
    os.path.expanduser("~") 会回退到真实家目录 —— 隔离形同虚设。
    """
    (tmp_path / "homes").mkdir()
    monkeypatch.setenv("CODEX_HOMES_ROOT", str(tmp_path / "homes"))
    seen = {}

    def fake(cmd, *, env, cwd, timeout, **kwargs):
        seen.update(env)
        return ('{"type":"item.completed","item":{"type":"agent_message","text":"你好"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":1}}\n',
                "", 0, False, {"thread_ms": 1, "turn_ms": 2, "completed_ms": 3,
                               "reply_ms": 3, "exit_ms": 4})

    monkeypatch.setattr(codex_turn, "_run_codex_process", fake)
    monkeypatch.setattr(codex_turn, "find_codex_binary", lambda: "/bin/true")
    from smsinvit.invitation import safety
    monkeypatch.setattr(safety, "require_binary", lambda binary: None)
    monkeypatch.setattr(safety, "child_environment", lambda: {})
    result = codex_turn.run_codex_turn(email="home@example.test", access_token=_jwt(3600),
                                       refresh_token="rt")
    assert result["ok"] is True
    home = seen["CODEX_HOME"]
    assert seen["HOME"] == home and seen["SMSINVIT_TASK_HOME"] == home
    assert seen["XDG_CONFIG_HOME"] == home + "/.config"
    assert seen["XDG_CACHE_HOME"] == home + "/.cache"
    assert seen["XDG_DATA_HOME"] == home + "/.local/share"
    assert seen["TMPDIR"] == home + "/tmp"


def test_runaway_cli_output_is_capped_and_fails_the_turn():
    """直连 codex 的这条路没有受控子进程的输出上限，必须自己兜。"""
    script = ("import sys\n"
              "line = 'x' * 4096 + '\\n'\n"
              "for _ in range(4000):\n"
              "    sys.stdout.write(line)\n")
    out, err, code, timed_out, marks = codex_turn._run_codex_process(
        [sys.executable, "-c", script], env=dict(os.environ), cwd=None, timeout=30)
    assert marks.get("oversized") == 1 and timed_out is False
    # 上限之外最多再多一行在途数据。
    assert len(out) <= codex_turn.MAX_TURN_OUTPUT + 8192


def test_kill_group_does_not_skip_a_process_that_already_exited(monkeypatch):
    """旧实现在 poll() 非空时直接 return，主进程正常退出后同组 helper 就漏掉了。"""
    calls = []

    class Handle:
        pid = 424242

        def poll(self):
            return 0            # 主进程已经退出

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(codex_turn.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(codex_turn.os, "killpg",
                        lambda pid, sig: calls.append(("killpg", pid, sig)))
    codex_turn._kill_group(Handle())
    assert calls and calls[0][0] == "killpg" and calls[0][1] == 424242


def test_a_normal_exit_still_runs_the_group_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(codex_turn, "_kill_group", lambda handle: calls.append(handle))
    codex_turn._run_codex_process([sys.executable, "-c", "print('{}')"],
                                  env=dict(os.environ), cwd=None, timeout=10)
    assert len(calls) >= 1


def _late_rotation_child(tmp_path, delay):
    script = tmp_path / "late_rotation.py"
    script.write_text(
        "import json, os, time\n"
        "def emit(**kw): print(json.dumps(kw), flush=True)\n"
        "emit(type='item.completed', item={'type':'agent_message','text':'hi'})\n"
        "emit(type='turn.completed')\n"
        f"time.sleep({float(delay)!r})\n"
        "p = os.path.join(os.environ['CODEX_HOME'], 'auth.json')\n"
        "data = json.load(open(p))\n"
        "data['tokens']['refresh_token'] = 'rotated-late'\n"
        "json.dump(data, open(p, 'w'))\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def test_a_late_credential_write_back_is_lost_with_the_current_grace(monkeypatch, tmp_path):
    """记录现状：1.2s 宽限之后才写回的轮换抓不到（默认值刻意不动）。

    真实危害由账本 + `tokens_unreadable` 兜住：读不到就保留原凭据、留下告警，
    不会拿旧值去覆盖刚轮换过的值。要真正抓到这个轮换，只能调高宽限，
    那等于把每轮固定多等几秒 —— 所以这里只把取舍钉成可执行的证据。
    """
    home = tmp_path / "home"
    home.mkdir()
    auth = home / "auth.json"
    env = {**os.environ, "CODEX_HOME": str(home)}

    auth.write_text(json.dumps({"tokens": {"refresh_token": "before"}}), encoding="utf-8")
    monkeypatch.setattr(codex_turn, "EARLY_EXIT_GRACE_SECONDS", 1.2)
    codex_turn._run_codex_process(_late_rotation_child(tmp_path, 3.0),
                                 env=env, cwd=None, timeout=30)
    assert json.loads(auth.read_text())["tokens"]["refresh_token"] == "before"

    monkeypatch.setattr(codex_turn, "EARLY_EXIT_GRACE_SECONDS", 6.0)
    codex_turn._run_codex_process(_late_rotation_child(tmp_path, 3.0),
                                 env=env, cwd=None, timeout=30)
    assert json.loads(auth.read_text())["tokens"]["refresh_token"] == "rotated-late"


def test_a_broken_auth_json_is_distinguished_from_no_rotation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    auth = home / "auth.json"
    assert codex_turn._home_tokens_unreadable(home) is False      # 文件不在
    auth.write_text("", encoding="utf-8")
    assert codex_turn._home_tokens_unreadable(home) is False      # 空文件
    auth.write_text('{"tokens":', encoding="utf-8")
    assert codex_turn._home_tokens_unreadable(home) is True       # 写了一半
    auth.write_text('{"tokens":{"refresh_token":"r"}}', encoding="utf-8")
    assert codex_turn._home_tokens_unreadable(home) is False      # 正常
    auth.write_text('{"tokens":{"refresh_token":""}}', encoding="utf-8")
    assert codex_turn._home_tokens_unreadable(home) is True       # 四项全空

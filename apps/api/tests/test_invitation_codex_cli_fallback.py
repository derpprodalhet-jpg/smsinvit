"""模型兜底 + `CODEX_TURN_LEAN` 实验档（默认关）的离线断言。

背景（真账号实测）：
  · ChatGPT 账号只接受部分模型：`gpt-5.6-sol`/`gpt-5.4` 等直接 400
    `model is not supported` —— 模型名写错或被下线不该让一次兑换失败；
  · 生产 `启动` 段是 4.7-5.1s，而 codex 二进制自身启动只要 0.02s，说明那是启动期的
    网络调用（模型目录刷新、workspace routing discovery）→ lean 档把它们钉死。
"""
import base64
import importlib.util
import json
import os
import tempfile
import tomllib

import pytest

# 这两个目录必须在导入 webui.* 之前存在（registrar/codex_cli 在模块级读环境变量）。
os.environ.setdefault('SMSINVIT_INVITATION_LOG_DIR', tempfile.mkdtemp(prefix='invitation-logs-'))
os.environ.setdefault('CODEX_HOMES_ROOT', tempfile.mkdtemp(prefix='codex-homes-'))

from test_invitation_process import ROOT, child  # noqa: E402,F401  （fixture 复用）

ACCOUNT_ID = '123e4567-e89b-42d3-a456-426614174000'


@pytest.fixture
def turn(child, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'isolated_lean_codex_turn', ROOT / 'vendor/codex_invit/webui/codex_turn.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'find_codex_binary', lambda: str(child[0]))
    return module


def id_token(account_id='', claim='chatgpt_account_id'):
    payload = base64.urlsafe_b64encode(json.dumps({
        'https://api.openai.com/auth': {claim: account_id},
    }).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


# ──────────────────────── lean 档 ────────────────────────


def test_lean_is_off_by_default(turn, monkeypatch):
    monkeypatch.delenv('CODEX_TURN_LEAN', raising=False)
    home = turn._write_home('a@example.test', id_token(ACCOUNT_ID), 'rt', '')
    config = tomllib.loads((home / 'config.toml').read_text())
    assert 'check_for_update_on_startup' not in config
    assert 'forced_chatgpt_workspace_id' not in config
    assert config['features'] == {'apps': False, 'plugins': False, 'remote_plugin': False}
    turn._cleanup_home(home)


def test_lean_pins_the_workspace_and_cuts_the_startup_knobs(turn, monkeypatch):
    monkeypatch.setenv('CODEX_TURN_LEAN', '1')
    home = turn._write_home('a@example.test', id_token(ACCOUNT_ID), 'rt', '')
    config = tomllib.loads((home / 'config.toml').read_text())
    assert config['check_for_update_on_startup'] is False
    assert config['forced_chatgpt_workspace_id'] == ACCOUNT_ID
    assert config['features']['browser_use'] is False
    assert config['features']['computer_use'] is False
    # 这一轮真正要用的能力绝不能被关掉（否则服务端可能不认这是"真跑一轮 codex"）
    for keep in ('shell_tool', 'unified_exec', 'apply_patch'):
        assert keep not in config['features']
    turn._cleanup_home(home)


def test_lean_refuses_a_non_uuid_workspace_id(turn, monkeypatch):
    """account_id 来自 JWT claim —— 形状不对就不写，绝不新增 TOML 注入面。"""
    monkeypatch.setenv('CODEX_TURN_LEAN', '1')
    hostile = 'x"\nsandbox_mode="danger-full-access'
    home = turn._write_home('a@example.test', id_token(hostile), 'rt', '')
    text = (home / 'config.toml').read_text()
    assert 'forced_chatgpt_workspace_id' not in text
    assert 'danger-full-access' not in text
    turn._cleanup_home(home)


# ──────────────────────── 模型兜底 ────────────────────────


def test_admin_default_and_runtime_default_for_sayhi_direct_agree(monkeypatch):
    """根因回归：**后台默认值与运行时默认值必须一致**。

    真实事故：`_INVITE_DEFAULTS["sayhi_direct"]="1"`（页面显示"直连已开"），而运行时读
    `_truthy_setting("sayhi_direct","0")`、库里又没有这一行 → 生产每一轮都走代理，
    三段计时 16s / 33s / 36s。这条断言把两个默认值钉在一起。
    """
    from test_invitation_process import _cli_stubs
    from webui import app as webui_app, codex_auth

    seen = {}

    def spy(key, default=""):
        seen[key] = default
        return str(default).strip().lower() in ('1', 'true', 'on', 'yes')

    creds = _cli_stubs(monkeypatch, codex_auth,
                       lambda job_id, creds, proxy="", **kw: {'ok': True, 'tokens': {}, 'elapsed': 1.0})
    monkeypatch.setattr(codex_auth, '_truthy_setting', spy)
    codex_auth._complete_cli('a@example.test', dict(creds), proxy='http://p:1',
                             job_id='job', on_stage=None)
    assert seen['sayhi_direct'] == webui_app._INVITE_DEFAULTS['sayhi_direct'] == '1'


def test_admin_default_and_runtime_default_for_the_model_agree():
    """同一类漂移的第二处：执行模型默认值必须三处一致（后台显示/权限基线/运行时）。"""
    from smsinvit.invitation import router as invitation_router
    from webui import app as webui_app, codex_turn

    assert webui_app._INVITE_DEFAULTS['sayhi_model'] == codex_turn.DEFAULT_SAYHI_MODEL
    assert invitation_router.CLI_CONTROL_DEFAULTS['sayhi_model'] == codex_turn.DEFAULT_SAYHI_MODEL
    # 兜底模型必须是一个 ChatGPT 账号一定能跑的模型，且与主模型不同时才有意义
    assert codex_turn.DEFAULT_MODEL == 'gpt-5.5'


def test_unsupported_model_falls_back_to_the_known_good_model(monkeypatch):
    from webui import codex_cli, codex_turn

    calls = []

    def fake_run(job_id, creds, *, proxy, cfg):
        calls.append((cfg.get('sayhi_model'), creds.get('access_token')))
        if len(calls) == 1:
            return {'ok': False,
                    'reason': "The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.",
                    'tokens': {'access_token': 'rotated-access'}, 'elapsed': 13.7}
        return {'ok': True, 'reason': '', 'reply': 'Hi!', 'tokens': {}, 'elapsed': 11.0}

    monkeypatch.setattr(codex_cli, '_settings', lambda settings=None: {'sayhi_model': 'gpt-5.6-sol'})
    monkeypatch.setattr(codex_cli, '_run_turn', fake_run)
    result = codex_cli.run_say_hi('job', {'email': 'a@example.test', 'access_token': 'orig',
                                          'refresh_token': 'r'})
    assert result['ok'] is True and result['reply'] == 'Hi!'
    assert result['attempts'] == ['gpt-5.6-sol', codex_turn.DEFAULT_MODEL]
    assert result['fallback_model'] == codex_turn.DEFAULT_MODEL
    # 第二次必须带上第一次轮换过的 access_token，否则拿旧凭据再跑一轮只会白烧。
    assert calls == [('gpt-5.6-sol', 'orig'), (codex_turn.DEFAULT_MODEL, 'rotated-access')]
    # 出口也必须是累计账本：第二轮没轮换（tokens={}），第一轮的结果不能被盖掉。
    assert result['tokens'] == {'access_token': 'rotated-access'}


def test_other_failures_never_retry_another_model(monkeypatch):
    from webui import codex_cli

    calls = []

    def fake_run(job_id, creds, *, proxy, cfg):
        calls.append(cfg.get('sayhi_model'))
        return {'ok': False, 'reason': 'codex 没有产出回复（退出码 1，20s）',
                'tokens': {}, 'elapsed': 20.0}

    monkeypatch.setattr(codex_cli, '_settings', lambda settings=None: {'sayhi_model': 'gpt-5.6-sol'})
    monkeypatch.setattr(codex_cli, '_run_turn', fake_run)
    result = codex_cli.run_say_hi('job', {'email': 'a@example.test', 'access_token': 'x',
                                          'refresh_token': 'r'})
    assert calls == ['gpt-5.6-sol']
    assert result['attempts'] == ['gpt-5.6-sol'] and result['fallback_model'] == ''


def test_the_known_good_model_is_never_retried_against_itself(monkeypatch):
    from webui import codex_cli, codex_turn

    calls = []

    def fake_run(job_id, creds, *, proxy, cfg):
        calls.append(cfg.get('sayhi_model'))
        return {'ok': False, 'reason': 'model is not supported', 'tokens': {}, 'elapsed': 5.0}

    monkeypatch.setattr(codex_cli, '_settings',
                        lambda settings=None: {'sayhi_model': codex_turn.DEFAULT_MODEL})
    monkeypatch.setattr(codex_cli, '_run_turn', fake_run)
    codex_cli.run_say_hi('job', {'email': 'a@example.test', 'access_token': 'x',
                                 'refresh_token': 'r'})
    assert calls == [codex_turn.DEFAULT_MODEL]


def test_primary_default_comes_from_the_shared_constant(monkeypatch):
    """未配置 `sayhi_model` 时的主模型 = `codex_turn.DEFAULT_SAYHI_MODEL`（换它要有实测）。"""
    from webui import codex_cli, codex_turn

    monkeypatch.setattr(codex_cli, '_settings', lambda settings=None: {})
    monkeypatch.setattr(codex_cli, '_run_turn',
                        lambda job_id, creds, *, proxy, cfg: {'ok': True, 'tokens': {}, 'elapsed': 1.0})
    result = codex_cli.run_say_hi('job', {'email': 'a@example.test', 'access_token': 'x',
                                          'refresh_token': 'r'})
    assert result['attempts'] == [codex_turn.DEFAULT_SAYHI_MODEL]


def test_the_model_fallback_shares_one_cli_budget(monkeypatch):
    """两次尝试共用同一份 CLI 预算：第一次花掉的时间不能白送第二轮。"""
    from webui import codex_cli

    seen = []

    def fake_run(job_id, creds, *, proxy, cfg):
        seen.append(cfg.get('cli_timeout'))
        if len(seen) == 1:
            return {'ok': False, 'reason': "The 'gpt-5.6-sol' model is not supported.",
                    'tokens': {}, 'elapsed': 150.0}
        return {'ok': True, 'reply': 'Hi!', 'tokens': {}, 'elapsed': 1.0}

    monkeypatch.setattr(codex_cli, '_settings',
                        lambda settings=None: {'sayhi_model': 'gpt-5.6-sol', 'cli_timeout': 180})
    monkeypatch.setattr(codex_cli, '_run_turn', fake_run)
    codex_cli.run_say_hi('job', {'access_token': 'a', 'refresh_token': 'r'})
    # 180 - 150 = 30，而不是再给一整套 180。
    assert seen == [180, 30]

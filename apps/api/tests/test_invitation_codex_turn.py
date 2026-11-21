"""Offline CLI event evidence, safe errors, and rotation on unsuccessful exits."""
import importlib.util
import base64
import json
from pathlib import Path
import subprocess
import tomllib

import pytest

from smsinvit.invitation import process
from smsinvit.invitation import safety
from test_invitation_process import ROOT, child


def events(*items):
    return '\n'.join(json.dumps(item) for item in items) + '\n'


def reply(text='你好'):
    return {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}


COMPLETE = [{'type': 'turn.started'}, reply(), {'type': 'turn.completed', 'usage': {'input_tokens': 2}}]
ROTATED = {'access_token': 'rotated-access-fixture', 'refresh_token': 'rotated-refresh-fixture',
           'id_token': 'rotated-id-fixture', 'account_id': 'rotated-account-fixture'}

# Set by the `turn` fixture so install_child can patch the same isolated module.
turn_module = None


@pytest.fixture
def turn(child, monkeypatch):
    binary, homes, calls = child
    spec = importlib.util.spec_from_file_location('isolated_evidence_codex_turn', ROOT / 'vendor/codex_invit/webui/codex_turn.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'find_codex_binary', lambda: str(binary))
    monkeypatch.setattr(safety, 'require_proxy', lambda proxy: ('proxy.example.test', 8080, ('8.8.8.8',)))
    # install_child patches the launch seam of the same isolated module.
    global turn_module
    turn_module = module
    return module


@pytest.mark.parametrize('items,complete', [
    (COMPLETE, True),
    ([reply(), {'type': 'turn.completed'}], False),
    ([{'type': 'turn.started'}, reply()], False),
    ([{'type': 'turn.started'}, reply('  '), {'type': 'turn.completed'}], False),
    ([{'type': 'turn.started'}, reply({'message': 'not a reply'}), {'type': 'turn.completed'}], False),
    (COMPLETE + [{'type': 'error', 'message': 'fatal'}], False),
    (COMPLETE + [{'type': 'turn.failed', 'error': {'message': 'failed'}}], False),
    (COMPLETE + [{'type': 'turn.started'}, {'type': 'turn.completed'}], False),
])
def test_turn_requires_complete_assistant_evidence_without_failure(turn, items, complete):
    parsed = turn._parse_turn('not JSON\n{malformed}\n' + events(*items))
    assert parsed['completed'] is complete


def test_terminal_turn_failure_message_takes_priority_over_generic_error(turn):
    parsed = turn._parse_turn(events(
        {'type': 'error', 'message': 'first generic error'},
        {'type': 'turn.failed', 'error': {'message': 'definitive failure'}},
        {'type': 'error', 'message': 'later incidental error'},
    ))
    assert parsed['failed'] and parsed['error'] == 'definitive failure'


def install_child(monkeypatch, output, returncode=0, *, stderr='', fail=None):
    def fake(cmd, *, env, cwd, timeout, **kwargs):
        home = Path(env['CODEX_HOME'])
        config = tomllib.loads((home / 'config.toml').read_text())
        assert config['features'] == {'apps': False, 'plugins': False, 'remote_plugin': False}
        assert config['sandbox_mode'] == 'read-only' and config['approval_policy'] == 'never'
        assert env['HTTP_PROXY'] == 'http://fixture-user:fixture-password@proxy.example.test:8080'
        assert cmd[-2:] == ['--', '你好']
        assert '--sandbox' in cmd and 'danger-full-access' not in cmd
        assert Path(cwd).is_dir() and env['TMPDIR'] == str(home / 'tmp')
        path = home / 'auth.json'
        data = json.loads(path.read_text())
        data['tokens'].update(ROTATED)
        path.write_text(json.dumps(data))
        if fail == 'timeout':
            return output, stderr, None, True, {}
        if fail == 'launch':
            raise RuntimeError('access_token=rotated-access-fixture')
        return output, stderr, returncode, False, {}

    monkeypatch.setattr(turn_module, '_run_codex_process', fake)


def run(turn):
    return turn.run_codex_turn(email='fixture@example.test', access_token='access-fixture',
                               refresh_token='refresh-fixture',
                               proxy='http://fixture-user:fixture-password@proxy.example.test:8080')


@pytest.mark.parametrize('returncode,items,ok', [
    (0, COMPLETE, True),
    (1, COMPLETE, True),
    (0, COMPLETE + [{'type': 'error', 'message': 'request failed'}], True),
    (0, COMPLETE + [{'type': 'turn.failed', 'error': {'message': 'request failed'}}], True),
    (0, [{'type': 'turn.started'}, reply()], True),
    (0, [{'type': 'turn.started'}, {'type': 'turn.completed'}], False),
])
def test_run_keeps_rotated_credentials_and_uses_reply_criterion(turn, child, monkeypatch, returncode, items, ok):
    """LocalFlow 副本判据：拿到非空 agent_message 回复即兑现。退出码与附加的
    error/turn.failed 事件只用于诊断，不否决已经产生的回复。"""
    install_child(monkeypatch, events(*items), returncode)
    result = run(turn)
    assert result['ok'] is ok
    assert {key: result[key] for key in ROTATED} == ROTATED
    assert not any(child[1].iterdir())


@pytest.mark.parametrize('fail', ['timeout', 'launch'])
def test_timeout_and_launch_errors_read_rotation_before_cleanup(turn, child, monkeypatch, fail):
    install_child(monkeypatch, events(*COMPLETE), fail=fail)
    result = run(turn)
    assert not result['ok']
    assert {key: result[key] for key in ROTATED} == ROTATED
    assert 'rotated-access-fixture' not in result['error']
    assert not any(child[1].iterdir())


def test_jsonl_failure_is_safe_and_not_replaced_by_mcp_stderr(turn, child, monkeypatch, caplog):
    output = events({'type': 'error', 'message': 'generic'}, {'type': 'turn.failed', 'error': {
        'message': 'quota exhausted refresh_token=rotated-refresh-fixture at https://fixture-user:fixture-password@proxy.example.test:8080/?token=secret-fixture'
    }})
    install_child(monkeypatch, output, 1, stderr='chatgpt.com/backend-api/ps/mcp Transport channel closed\n')
    result = run(turn)
    assert not result['ok'] and 'quota exhausted' in result['error']
    assert 'Transport channel closed' not in result['error']
    for value in ['rotated-refresh-fixture', 'fixture-user', 'fixture-password', 'proxy.example.test', 'secret-fixture']:
        assert value not in result['error'] and value not in caplog.text
    assert result['refresh_token'] == 'rotated-refresh-fixture'
    assert not any(child[1].iterdir())


def test_mcp_only_stderr_is_not_reported_as_final_cause(turn, monkeypatch):
    install_child(monkeypatch, '', 1, stderr='rmcp: Transport channel closed\nchatgpt.com/backend-api/ps/mcp failed\n')
    result = run(turn)
    assert not result['ok'] and '没有产出回复' in result['error']
    assert 'mcp' not in result['error'] and 'Transport channel' not in result['error']


@pytest.mark.parametrize('fail', [None, 'timeout'])
def test_say_hi_transmits_all_four_rotated_fields_on_failure_and_timeout(turn, child, monkeypatch, fail):
    from webui import codex_cli, codex_turn
    install_child(monkeypatch, events({'type': 'turn.failed', 'error': {'message': 'test failure'}}), 1, fail=fail)
    monkeypatch.setattr(codex_cli, '_settings', lambda settings=None: {})
    monkeypatch.setattr(codex_turn, 'run_codex_turn', turn.run_codex_turn)
    result = codex_cli.run_say_hi('fixture-job', {
        'email': 'fixture@example.test', 'access_token': 'access-fixture', 'refresh_token': 'refresh-fixture',
    }, proxy='http://fixture-user:fixture-password@proxy.example.test:8080')
    assert not result['ok'] and result['tokens'] == ROTATED
    assert result['timed_out'] is (fail == 'timeout')
    assert not any(child[1].iterdir())


@pytest.mark.parametrize('claim', ['chatgpt_account_id', 'account_id'])
def test_missing_id_token_writes_the_current_access_token_account(turn, child, claim):
    payload = base64.urlsafe_b64encode(json.dumps({
        'https://api.openai.com/auth': {claim: 'current-account-fixture'},
    }).encode()).decode().rstrip('=')
    access_token = 'header.' + payload + '.signature'
    home = turn._write_home('fixture@example.test', access_token, 'refresh-fixture', '')
    auth = json.loads((home / 'auth.json').read_text())
    assert auth['tokens']['account_id'] == 'current-account-fixture'
    assert auth['tokens']['access_token'] == access_token and auth['tokens']['id_token'] == ''
    turn._cleanup_home(home)
    assert not any(child[1].iterdir())

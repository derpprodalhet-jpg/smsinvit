"""回退重试时必须把每一轮轮换过的凭据累计下来，不能只留最后一轮。

单轮契约是"只返回这一轮**变化过**的非空字段"，所以返回空 ≠ 没轮换。模型回退与
代理回退各自都可能刷新 refresh_token；旧 RT 在轮换那一刻就作废，丢掉它再写回旧值
相当于把这个号弄废。
"""
import os
import tempfile

import pytest

# registrar/codex_cli 在模块级读这两个目录，必须在导入 webui.* 之前就存在。
os.environ.setdefault('SMSINVIT_INVITATION_LOG_DIR', tempfile.mkdtemp(prefix='invitation-logs-'))
os.environ.setdefault('CODEX_HOMES_ROOT', tempfile.mkdtemp(prefix='codex-homes-'))

from webui import codex_cli, codex_turn  # noqa: E402

# 真实 creds 只有这四个字段（见 _saved_codex_creds）：邮箱是单独传的参数，
# 混进 creds 会让 save_codex_auth(email, **creds) 变成重复关键字。
CREDS = {'access_token': 'orig-access', 'refresh_token': 'orig-refresh',
         'id_token': 'orig-id', 'account_id': 'orig-account'}
UNSUPPORTED = "The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account."


@pytest.fixture
def model_fallback(monkeypatch):
    """把 run_say_hi 的两轮 CLI 换成可控替身，主模型固定为会触发兜底的名字。"""
    monkeypatch.setattr(codex_cli, '_settings',
                        lambda settings=None: {'sayhi_model': 'gpt-5.6-sol'})
    calls = []

    def install(*turns):
        def fake_run(job_id, creds, *, proxy, cfg):
            index = len(calls)
            calls.append({'model': cfg.get('sayhi_model'), 'creds': dict(creds)})
            return dict(turns[min(index, len(turns) - 1)])
        monkeypatch.setattr(codex_cli, '_run_turn', fake_run)
        return calls
    return install


def test_model_fallback_keeps_credentials_rotated_by_the_first_attempt(model_fallback):
    calls = model_fallback(
        {'ok': False, 'reason': UNSUPPORTED, 'elapsed': 13.7,
         'tokens': {'access_token': 'round1-access', 'refresh_token': 'round1-refresh'}},
        {'ok': True, 'reason': '', 'reply': 'Hi!', 'elapsed': 11.0, 'tokens': {}},
    )
    result = codex_cli.run_say_hi('job', dict(CREDS))
    assert result['ok'] is True
    # 第二轮没轮换：账本必须保住第一轮的刷新结果，而不是变成 {}。
    assert result['tokens'] == {'access_token': 'round1-access', 'refresh_token': 'round1-refresh'}
    # 第二轮仍然要带上第一轮的值当输入（原有行为）。
    assert calls[1]['creds']['refresh_token'] == 'round1-refresh'


def test_model_fallback_merges_partial_rotations_from_both_attempts(model_fallback):
    model_fallback(
        {'ok': False, 'reason': UNSUPPORTED, 'elapsed': 13.7,
         'tokens': {'refresh_token': 'round1-refresh'}},
        {'ok': True, 'reason': '', 'reply': 'Hi!', 'elapsed': 11.0,
         'tokens': {'access_token': 'round2-access'}},
    )
    result = codex_cli.run_say_hi('job', dict(CREDS))
    # 两轮各换了一半：两个都要留下，否则 refresh 会退回已作废的旧值。
    assert result['tokens'] == {'refresh_token': 'round1-refresh', 'access_token': 'round2-access'}


def test_the_same_key_takes_the_newer_rotation(model_fallback):
    model_fallback(
        {'ok': False, 'reason': UNSUPPORTED, 'elapsed': 1.0, 'tokens': {'refresh_token': 'round1'}},
        {'ok': True, 'reason': '', 'reply': 'Hi!', 'elapsed': 1.0, 'tokens': {'refresh_token': 'round2'}},
    )
    assert codex_cli.run_say_hi('job', dict(CREDS))['tokens'] == {'refresh_token': 'round2'}


def test_no_fallback_leaves_the_single_turn_result_unchanged(model_fallback):
    calls = model_fallback({'ok': True, 'reason': '', 'reply': 'Hi!', 'elapsed': 9.0,
                            'tokens': {'access_token': 'only-access'}})
    result = codex_cli.run_say_hi('job', dict(CREDS))
    assert len(calls) == 1
    assert result['tokens'] == {'access_token': 'only-access'}
    assert result['attempts'] == ['gpt-5.6-sol'] and result['fallback_model'] == ''


def test_a_failed_fallback_still_reports_the_first_rotation(model_fallback):
    model_fallback(
        {'ok': False, 'reason': UNSUPPORTED, 'elapsed': 1.0, 'tokens': {'refresh_token': 'round1'}},
        {'ok': False, 'reason': 'codex 没有产出回复（退出码 1，20s）', 'elapsed': 2.0, 'tokens': {}},
    )
    result = codex_cli.run_say_hi('job', dict(CREDS))
    assert result['ok'] is False
    assert result['tokens'] == {'refresh_token': 'round1'}


class DbRecorder:
    def __init__(self):
        self.saved = []
        self.refresh_writes = []

    def save_codex_auth(self, email, **fields):
        self.saved.append({'email': email, **fields})

    def set_registered_refresh_token(self, email, token):
        self.refresh_writes.append((email, token))


@pytest.fixture
def auth(monkeypatch):
    from webui import codex_auth
    record = DbRecorder()
    monkeypatch.setattr(codex_auth, 'db', record)
    monkeypatch.setattr(codex_auth, 'proxy_pool',
                        type('P', (), {'is_transport_error': staticmethod(
                            lambda reason: 'timeout' in str(reason))}))
    monkeypatch.setattr(codex_auth, '_int_setting', lambda key, default: 180)
    return record


def run_fallback(monkeypatch, direct_result, retry_result, *, proxy='http://p:1'):
    from webui import codex_auth
    calls = []

    def fake_run(job_id, creds, *, proxy='', settings=None, keep_home=False):
        calls.append({'creds': dict(creds), 'proxy': proxy, 'settings': settings})
        return dict(direct_result if len(calls) == 1 else retry_result)

    monkeypatch.setattr(codex_auth.codex_cli, 'run_say_hi', fake_run)
    merged = codex_auth._run_cli_with_fallback(
        email='rotate@example.test', creds=dict(CREDS), proxy=proxy, job_id='job', direct=True)
    return merged, calls


def test_proxy_fallback_merges_direct_and_retry_rotations(auth, monkeypatch):
    merged, calls = run_fallback(
        monkeypatch,
        {'ok': False, 'reason': 'codex 超时 timeout（60s，代理太慢或不可用）', 'elapsed': 60.0,
         'tokens': {'refresh_token': 'direct-refresh'}},
        {'ok': True, 'reason': '', 'elapsed': 20.0, 'tokens': {'access_token': 'proxy-access'}},
    )
    assert merged['ok'] is True and merged['fallback_from_direct'] is True
    assert merged['tokens'] == {'refresh_token': 'direct-refresh', 'access_token': 'proxy-access'}
    # 回退轮必须拿直连轮的刷新结果当输入。
    assert calls[1]['creds']['refresh_token'] == 'direct-refresh'


def test_proxy_fallback_without_new_rotation_keeps_the_direct_rotation(auth, monkeypatch):
    merged, _ = run_fallback(
        monkeypatch,
        {'ok': False, 'reason': 'codex 超时 timeout（60s）', 'elapsed': 60.0,
         'tokens': {'refresh_token': 'direct-refresh'}},
        {'ok': True, 'reason': '', 'elapsed': 20.0, 'tokens': {}},
    )
    assert merged['tokens'] == {'refresh_token': 'direct-refresh'}


def test_direct_rotation_is_persisted_before_the_proxy_retry_runs(auth, monkeypatch):
    """直连轮换掉 RT 之后、回退轮跑完之前进程若挂掉，账号不能留在废钥匙上。"""
    from webui import codex_auth

    def explode(*args, **kwargs):
        raise RuntimeError('process died mid-retry')

    calls = []

    def fake_run(job_id, creds, *, proxy='', settings=None, keep_home=False):
        calls.append(dict(creds))
        if len(calls) == 1:
            return {'ok': False, 'reason': 'timeout', 'elapsed': 1.0,
                    'tokens': {'refresh_token': 'direct-refresh'}}
        return explode()

    monkeypatch.setattr(codex_auth.codex_cli, 'run_say_hi', fake_run)
    with pytest.raises(RuntimeError):
        codex_auth._run_cli_with_fallback(email='rotate@example.test', creds=dict(CREDS),
                                         proxy='http://p:1', job_id='job', direct=True)
    assert auth.saved and auth.saved[0]['refresh_token'] == 'direct-refresh'
    assert auth.refresh_writes == [('rotate@example.test', 'direct-refresh')]


def test_complete_cli_persists_the_accumulated_ledger(auth, monkeypatch):
    from webui import codex_auth
    monkeypatch.setattr(codex_auth, '_ensure_fresh_codex_creds', lambda email, creds, *, proxy: dict(creds))
    monkeypatch.setattr(codex_auth, '_truthy_setting', lambda key, default='': True)
    monkeypatch.setattr(codex_auth, '_print_stage_log', lambda *args: None)
    monkeypatch.setattr(codex_auth, 'emit_stage', lambda *args: None)
    monkeypatch.setattr(codex_auth, '_export_sub2api', lambda email, cred: {})
    monkeypatch.setattr(codex_auth, '_run_cli_with_fallback', lambda **kwargs: {
        'ok': True, 'elapsed': 12.0, 'reason': '',
        # 累计账本：refresh 来自直连轮，access/id 来自代理轮，account 从未轮换。
        'tokens': {'refresh_token': 'ledger-refresh', 'access_token': 'ledger-access',
                   'id_token': 'ledger-id'},
    })
    result = codex_auth._complete_cli('rotate@example.test', dict(CREDS), proxy='http://p:1', job_id='job')
    final = [entry for entry in auth.saved if 'access_token' in entry][-1]
    assert final['access_token'] == 'ledger-access'
    assert final['refresh_token'] == 'ledger-refresh'
    assert final['id_token'] == 'ledger-id'
    # 账本里没有的字段回落到 CLI 之前的凭据，而不是空值。
    assert final['account_id'] == 'orig-account'
    assert result['account_id'] == 'orig-account'
    assert auth.refresh_writes[-1] == ('rotate@example.test', 'ledger-refresh')


def test_complete_cli_keeps_previous_credentials_when_nothing_rotated(auth, monkeypatch):
    from webui import codex_auth
    monkeypatch.setattr(codex_auth, '_ensure_fresh_codex_creds', lambda email, creds, *, proxy: dict(creds))
    monkeypatch.setattr(codex_auth, '_truthy_setting', lambda key, default='': True)
    monkeypatch.setattr(codex_auth, '_print_stage_log', lambda *args: None)
    monkeypatch.setattr(codex_auth, 'emit_stage', lambda *args: None)
    monkeypatch.setattr(codex_auth, '_export_sub2api', lambda email, cred: {})
    monkeypatch.setattr(codex_auth, '_run_cli_with_fallback', lambda **kwargs: {
        'ok': True, 'elapsed': 12.0, 'reason': '', 'tokens': {}})
    codex_auth._complete_cli('rotate@example.test', dict(CREDS), proxy='http://p:1', job_id='job')
    final = [entry for entry in auth.saved if 'access_token' in entry][-1]
    assert (final['access_token'], final['refresh_token']) == ('orig-access', 'orig-refresh')


def test_the_proxy_retry_budget_never_exceeds_the_configured_cli_timeout(auth, monkeypatch):
    """回退的兜底下限不能超过总预算本身。

    旧写法 max(30, 预算-已用) 在预算小于 30 秒时反而会把总耗时顶到 预算+30。
    """
    from webui import codex_auth

    clock = {'t': 1000.0}
    monkeypatch.setattr(codex_auth.time, 'time', lambda: clock['t'])
    monkeypatch.setattr(codex_auth, '_int_setting', lambda key, default: 20)
    settings_seen = []

    def fake_run(job_id, creds, *, proxy='', settings=None, keep_home=False):
        settings_seen.append(settings)
        if len(settings_seen) == 1:
            clock['t'] += 19.0          # 第一轮把 20 秒预算用掉 19 秒
            return {'ok': False, 'reason': 'timeout', 'tokens': {}, 'elapsed': 19.0}
        return {'ok': True, 'tokens': {}, 'elapsed': 1.0}

    monkeypatch.setattr(codex_auth.codex_cli, 'run_say_hi', fake_run)
    codex_auth._run_cli_with_fallback(email='rotate@example.test', creds=dict(CREDS),
                                     proxy='http://p:1', job_id='job', direct=True)
    # 剩余 1 秒，但下限取 min(30, 预算) = 20：整段 CLI 不超过 20 秒的预算。
    assert settings_seen[1] == {'cli_timeout': '20'}

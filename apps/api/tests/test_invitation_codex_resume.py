"""Authorization stage boundaries: all credentials and external actions are fixtures."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


EMAIL = 'cli-stage@example.test'
ACCOUNT = 'fixture-chatgpt-account'
PROXY = 'http://fixture-proxy.example.test:8080'
LINK = 'https://chatgpt.com/invite/fixture-only'


def token(email=EMAIL, account=ACCOUNT):
    payload = {'email': email, 'https://api.openai.com/profile': {'email': email},
               'https://api.openai.com/auth': {'chatgpt_account_id': account}}
    return 'fixture.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') + '.unsigned'


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv('CODEX_HOMES_ROOT', str(tmp_path / 'homes'))
    monkeypatch.setenv('SMSINVIT_INVITATION_LOG_DIR', str(tmp_path / 'logs'))
    from webui import codex_auth as auth
    saved, registered, writes, events, stages = {}, {'email': EMAIL, 'password': 'fixture-password'}, [], [], []
    card = {'email': EMAIL, 'invite_link': LINK, 'invite_processed_at': 1}
    def save(email, **fields):
        assert email == EMAIL
        writes.append(dict(fields))
        saved.update(email=email, **fields)
    monkeypatch.setattr(auth.db, 'get_codex_auth', lambda email: dict(saved) if saved else None)
    monkeypatch.setattr(auth.db, 'save_codex_auth', save)
    monkeypatch.setattr(auth.db, 'set_registered_refresh_token', lambda email, value: registered.update(refresh_token=value))
    monkeypatch.setattr(auth.db, 'get_registered', lambda email: dict(registered))
    monkeypatch.setattr(auth.db, 'save_registered', lambda value: registered.update(value))
    monkeypatch.setattr(auth.db, 'get_setting', lambda key, default='': '0' if key == 'invite_want_2fa' else default)
    monkeypatch.setattr(auth.db, 'get_cdk_code', lambda code: dict(card))
    monkeypatch.setattr(auth.db, 'get_cdk_job', lambda job: {'job_id': job})
    monkeypatch.setattr(auth.db, 'add_cdk_event', lambda *args: events.append(args))
    for name in ('set_cdk_status', 'set_cdk_job', 'update_cdk_fields', 'set_account_proxy', 'mark_done'):
        monkeypatch.setattr(auth.db, name, lambda *args, **kw: None)
    monkeypatch.setattr(auth, 'Config', lambda: SimpleNamespace(proxy=None))
    monkeypatch.setattr(auth.proxy_pool, 'attempts_limit', lambda: 3)
    pick = Mock(return_value={'url': PROXY})
    monkeypatch.setattr(auth.proxy_pool, 'pick', pick)
    monkeypatch.setattr(auth.proxy_pool, 'record_success', Mock())
    monkeypatch.setattr(auth.proxy_pool, 'record_fail', Mock())
    monkeypatch.setattr(auth.proxy_pool, 'record_409', Mock(return_value={}))
    monkeypatch.setattr(auth.proxy_pool, 'record_transport_failure', Mock(return_value={}))
    monkeypatch.setattr(auth.proxy_pool, 'is_transport_error',
                        lambda value: 'connect tunnel' in str(value).lower() or 'curl: (7)' in str(value))
    monkeypatch.setattr(auth.proxy_pool, 'is_409_error', lambda value: False)
    mail = Mock(return_value=(object(), 'fixture', {}))
    monkeypatch.setattr(auth.mail_alloc, 'build_provider_for_account', mail)
    monkeypatch.setattr(auth.invite_mail, 'read_invitation', Mock(side_effect=AssertionError('mail read forbidden')))
    monkeypatch.setattr(auth.invite_mail, 'visit_invite_link', Mock(return_value={'ok': True}))
    result = SimpleNamespace(email=EMAIL, access_token=token(), session_token='fixture-session', refresh_token='', totp_secret='')
    result.to_dict = lambda: {'email': EMAIL, 'password': 'fixture-password', 'access_token': result.access_token}
    flow = SimpleNamespace(result=result)
    flow.run_register = Mock(return_value=result)
    flow.run_protocol_login = Mock()
    flow.oauth_codex_rt_exchange = Mock(side_effect=lambda **kw: setattr(result, 'refresh_token', 'fixture-oauth-rt'))
    # 池中会话恢复：拆分后这里**不该**再出现 CSRF / signin / OAuth 初始化。
    # 探测信号 _session_refreshed 由 from_existing_credentials 设置。
    flow._session_refreshed = False

    def restore(session_token='', access_token='', device_id=''):
        flow._session_refreshed = True
        result.access_token = result.access_token or token()
        result.session_token = session_token or 'fixture-session'
        return result

    flow.from_existing_credentials = Mock(side_effect=restore)
    # 只有会话失效的兜底路径才会碰这三个；正常路径断言它们没被调用。
    flow._build_codex_authorize = Mock(side_effect=AssertionError('session rebuild forbidden'))
    flow._follow_authorize_for_callback = Mock(side_effect=AssertionError('session rebuild forbidden'))
    flow._codex_drive_login_from_log_in = Mock(side_effect=AssertionError('session rebuild forbidden'))
    # 授权阶段必须彻底摆脱 chatgpt.com 的登录初始化链 —— 碰一下就炸。
    for forbidden in ('warmup', 'get_csrf_token', 'get_auth_url', 'auth_oauth_init'):
        setattr(flow, forbidden, Mock(side_effect=AssertionError(f'{forbidden} forbidden in the authorization stage')))
    new_flow = Mock(return_value=flow)
    monkeypatch.setattr(auth, '_new_flow', new_flow)
    refresh = Mock(return_value={'access_token': token(), 'refresh_token': 'fixture-fresh-rt', 'id_token': token()})
    monkeypatch.setattr(auth.exporter, 'refresh_codex_token', refresh)
    cli = Mock(return_value={'ok': False, 'reason': 'fixture CLI exited 1',
                             'tokens': {'access_token': token(), 'refresh_token': 'fixture-rotated-rt'}})
    monkeypatch.setattr(auth.codex_cli, 'run_say_hi', cli)
    export = Mock(return_value={'attempted': True, 'ok': True})
    monkeypatch.setattr(auth, '_export_sub2api', export)
    # Do not let an accidental new path reach any upstream request adapter.
    import sms_provider
    monkeypatch.setattr(sms_provider.requests, 'get', lambda *a, **kw: pytest.fail('live network forbidden'))
    return SimpleNamespace(auth=auth, saved=saved, registered=registered, writes=writes, events=events,
                           stages=stages, card=card, pick=pick, mail=mail, new_flow=new_flow,
                           flow=flow, refresh=refresh, cli=cli, export=export)


def invoke(app, manual):
    callback = lambda stage, *_: app.stages.append(stage)
    if manual:
        return app.auth.authorize_registered_email(EMAIL, run_id='fixture-run', on_stage=callback)
    return app.auth.run_invite_job('fixture-card', run_id='fixture-run', job_id='fixture-job', on_stage=callback)


@pytest.mark.parametrize('manual', [True, False])
def test_authorization_persists_validated_phone_and_code(app, monkeypatch, manual):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    mark = Mock()
    monkeypatch.setattr(app.auth.db, 'mark_phone_bound', mark)

    def exchange(**kwargs):
        app.flow._phone_bound_ok = True
        app.flow._phone_number = '+12025550123'
        app.flow._phone_code = '123456'
        app.flow.result.refresh_token = 'fixture-oauth-rt'

    app.flow.oauth_codex_rt_exchange.side_effect = exchange
    invoke(app, manual)
    mark.assert_called_once_with(EMAIL, True, phone_number='+12025550123', phone_code='123456')


@pytest.mark.parametrize('manual', [True, False])
def test_cli_failure_never_restarts_login_oauth_or_picks_another_proxy(app, manual):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    def cli(job, creds, *, proxy):
        assert app.saved['access_token'] == creds['access_token']
        assert app.saved['refresh_token'] == app.registered['refresh_token'] == 'fixture-fresh-rt'
        # 默认直连（库里没有 sayhi_direct 这一行）；要锁代理得显式设 0。
        assert creds['email'] == EMAIL and proxy == ''
        return {'ok': False, 'reason': 'exit 1, no reply', 'tokens': {'refresh_token': 'fixture-rotated-rt'}}
    app.cli.side_effect = cli
    out = invoke(app, manual)
    assert not out['ok'] and out['attempts'] == 1
    assert app.pick.call_count == app.new_flow.call_count == app.flow.oauth_codex_rt_exchange.call_count == 1
    assert app.cli.call_count == 1
    assert app.saved['refresh_token'] == app.registered['refresh_token'] == 'fixture-rotated-rt'
    assert 'cli_completed_at' not in app.saved
    assert 'done' not in app.stages
    app.export.assert_not_called()
    app.refresh.assert_called_once_with('fixture-oauth-rt', proxy=PROXY)


@pytest.mark.parametrize('manual', [True, False])
def test_next_explicit_retry_resumes_saved_cli_without_registration_oauth_or_refresh(app, manual):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK,
                     access_token=token(), refresh_token='fixture-saved-rt', id_token=token(), account_id=ACCOUNT)
    app.cli.return_value = {'ok': True, 'elapsed': 1, 'tokens': {'refresh_token': 'fixture-success-rt'}}
    out = invoke(app, manual)
    assert out['ok'] and out['result']['account_id'] == ACCOUNT
    app.new_flow.assert_not_called()
    app.flow.run_register.assert_not_called()
    app.flow.run_protocol_login.assert_not_called()
    app.flow.oauth_codex_rt_exchange.assert_not_called()
    app.refresh.assert_not_called()
    assert app.cli.call_count == app.export.call_count == 1
    assert app.saved['refresh_token'] == app.registered['refresh_token'] == 'fixture-success-rt'
    assert app.saved['cli_completed_at'] > 0 and app.saved['cli_completed_account_id'] == ACCOUNT
    assert 'codex_oauth' not in app.stages and 'registering' not in app.stages and 'done' in app.stages


@pytest.mark.parametrize('manual', [True, False])
@pytest.mark.parametrize('failure', ['runtime', 'job_failure'])
def test_cli_exception_preserves_credentials_before_process_and_stops_outer_retry(app, manual, failure):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.cli.side_effect = (RuntimeError('fixture process raised') if failure == 'runtime'
                           else app.auth.JobFailure('fixture CLI failure', is_409=True))
    out = invoke(app, manual)
    assert not out['ok'] and out['attempts'] == 1
    assert app.saved['access_token'] == token() and app.saved['refresh_token'] == 'fixture-fresh-rt'
    assert app.cli.call_count == app.flow.oauth_codex_rt_exchange.call_count == app.pick.call_count == 1
    assert 'cli_completed_at' not in app.saved
    app.export.assert_not_called()


@pytest.mark.parametrize('manual', [True, False])
def test_bookkeeping_error_after_cli_success_cannot_automatically_replay_the_message(app, manual):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK,
                     access_token=token(), refresh_token='fixture-saved-rt', id_token=token(), account_id=ACCOUNT)
    app.cli.return_value = {'ok': True, 'tokens': {}}
    app.auth.proxy_pool.record_success.side_effect = RuntimeError('fixture proxy stats failed')
    out = invoke(app, manual)
    assert not out['ok'] and out['attempts'] == 1
    assert app.saved['cli_completed_at'] > 0
    assert app.cli.call_count == app.pick.call_count == 1
    app.new_flow.assert_not_called()


@pytest.mark.parametrize('manual', [True, False])
def test_refresh_failure_retains_oauth_rt_and_never_rents_via_another_attempt(app, manual):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.refresh.side_effect = TimeoutError('fixture refresh timeout')
    out = invoke(app, manual)
    assert not out['ok'] and out['attempts'] == 1
    assert app.saved['refresh_token'] == app.registered['refresh_token'] == 'fixture-oauth-rt'
    assert app.flow.oauth_codex_rt_exchange.call_count == app.pick.call_count == 1
    app.cli.assert_not_called()
    app.export.assert_not_called()


@pytest.mark.parametrize('bad', [
    {'email': 'another@example.test'}, {'invite_processed_at': None}, {'refresh_token': ''},
    {'access_token': ''}, {'account_id': 'other-account'}, {'id_token': token('other@example.test')},
    {'access_token': token('other@example.test')},
])
def test_saved_credentials_are_scoped_to_email_invitation_and_token_account(app, bad):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK,
                     access_token=token(), refresh_token='fixture-saved-rt', id_token=token(), account_id=ACCOUNT)
    app.saved.update(bad)
    assert app.auth._saved_codex_creds(EMAIL) is None
    app.cli.assert_not_called()


def test_different_invitation_link_cannot_skip_a_new_acceptance(app):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK,
                     access_token=token(), refresh_token='fixture-saved-rt', id_token=token(), account_id=ACCOUNT)
    assert app.auth._saved_codex_creds(EMAIL, invite_link='https://chatgpt.com/invite/other') is None


@pytest.mark.parametrize('when', ['rt', 'before_cli', 'after_cli'])
def test_persistence_failure_stops_external_retry_and_does_not_mark_done(app, monkeypatch, when):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    original = app.auth.db.save_codex_auth
    def save(email, **fields):
        if (when == 'rt' and fields.get('refresh_token') == 'fixture-oauth-rt'
                or when == 'before_cli' and fields.get('refresh_token') == 'fixture-fresh-rt'
                or when == 'after_cli' and fields.get('refresh_token') == 'fixture-rotated-rt'):
            raise RuntimeError('fixture persistence refused')
        return original(email, **fields)
    monkeypatch.setattr(app.auth.db, 'save_codex_auth', save)
    out = invoke(app, True)
    assert not out['ok'] and out['attempts'] == 1 and app.pick.call_count == 1
    assert app.cli.call_count == (1 if when == 'after_cli' else 0)
    assert 'cli_completed_at' not in app.saved
    app.export.assert_not_called()


def test_export_failure_remains_best_effort_after_proven_cli_success(app):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK,
                     access_token=token(), refresh_token='fixture-saved-rt', id_token=token(), account_id=ACCOUNT)
    app.cli.return_value = {'ok': True, 'tokens': {}}
    app.export.return_value = {'attempted': True, 'ok': False, 'message': 'fixture export refused'}
    out = invoke(app, True)
    assert out['ok'] and out['result']['export']['ok'] is False
    assert app.saved['cli_completed_at'] > 0
    assert app.cli.call_count == 1


def test_bound_account_restores_the_pool_session_without_registering_or_409(app):
    """拆分后的核心断言：授权阶段=恢复池中会话 + 直连换 RT，没有注册/初始化/409。"""
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.cli.return_value = {'ok': True, 'elapsed': 1, 'tokens': {'refresh_token': 'fixture-success-rt'}}
    out = invoke(app, False)
    assert out['ok']
    app.flow.from_existing_credentials.assert_called_once()
    assert app.flow.oauth_codex_rt_exchange.call_count == 1
    assert app.flow.run_register.call_count == 0
    assert app.flow.run_protocol_login.call_count == 0
    # 一次尝试就成功 → 不换第二个代理
    assert app.pick.call_count == 1
    app.auth.proxy_pool.record_409.assert_not_called()


def test_dead_pool_session_uses_protocol_login_only_and_never_the_web_init_chain(app):
    """池中会话失效：允许补一次纯协议登录，仍然不许碰 CSRF / signin / OAuth 初始化。"""
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.flow._session_refreshed = False
    app.flow.from_existing_credentials = Mock()  # 恢复失败：不置活会话标记
    app.flow._build_codex_authorize = Mock(
        return_value=('https://auth.openai.com/oauth/authorize?x=1', 'state', 'verifier',
                      'http://localhost:1455/auth/callback', 'fixture-client'))
    app.flow._follow_authorize_for_callback = Mock(
        return_value=('', 'https://auth.openai.com/log-in'))

    def drive(**kwargs):
        app.flow._session_refreshed = True  # 登录推进成功，会话可用
        return 'https://auth.openai.com/authorize/continue'

    app.flow._codex_drive_login_from_log_in = Mock(side_effect=drive)
    app.cli.return_value = {'ok': True, 'elapsed': 1, 'tokens': {'refresh_token': 'fixture-success-rt'}}
    out = invoke(app, False)
    assert out['ok']
    app.flow._codex_drive_login_from_log_in.assert_called_once()
    app.flow._build_codex_authorize.assert_called_once()
    assert app.flow.run_register.call_count == 0
    assert app.flow.run_protocol_login.call_count == 0
    for forbidden in ('warmup', 'get_csrf_token', 'get_auth_url', 'auth_oauth_init'):
        assert getattr(app.flow, forbidden).call_count == 0


def test_unrecoverable_pool_session_stops_without_restarting_registration(app):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.flow._session_refreshed = False
    app.flow.from_existing_credentials = Mock()
    app.flow._build_codex_authorize = Mock(side_effect=RuntimeError('synthetic no authorize session'))
    out = invoke(app, False)
    assert not out['ok'] and out['attempts'] == 1
    assert app.flow.run_register.call_count == 0
    assert app.flow.run_protocol_login.call_count == 0
    assert app.pick.call_count == 1
    app.auth.proxy_pool.record_409.assert_not_called()


def test_bound_mailbox_without_password_fails_fast_without_registering(app):
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.registered['password'] = ''
    out = invoke(app, False)
    assert not out['ok'] and out['attempts'] == 1
    # 缺密码在构造 flow 之前就失败：不建 flow、不注册、不登录、不跑上游。
    assert app.new_flow.call_count == 0
    assert app.flow.run_register.call_count == 0
    assert app.flow.run_protocol_login.call_count == 0
    assert app.cli.call_count == 0 and app.refresh.call_count == 0


# ──────────────────────── 激活锁记账（codex 维度） ────────────────────────


def test_invite_409_is_recorded_on_the_codex_lock(monkeypatch, tmp_path):
    """授权阶段不做 409 预检，但真的撞上 409 要计入**激活锁**。

    与注册锁完全独立：注册锁冻结的代理，激活维度照样能用。
    """
    # webui.registrar 导入时会读日志目录环境变量；本用例直接引用内部函数、
    # 不经过 app fixture，所以要先自己设好。
    monkeypatch.setenv('SMSINVIT_INVITATION_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('CODEX_HOMES_ROOT', str(tmp_path / 'homes'))
    from webui import codex_auth, proxy_pool

    calls = []
    monkeypatch.setattr(proxy_pool, 'is_409_error',
                        lambda text: '409' in str(text) or 'invalid_state' in str(text))
    monkeypatch.setattr(proxy_pool, 'record_409',
                        lambda ident, purpose='codex': (calls.append(('409', ident, purpose))
                                                        or {'action': 'freeze'}))
    monkeypatch.setattr(proxy_pool, 'record_fail',
                        lambda ident, error='', purpose='codex': calls.append(('fail', ident, purpose)))
    monkeypatch.setattr(proxy_pool, 'record_transport_failure',
                        lambda ident: calls.append(('transport', ident, 'health')))

    codex_auth._record_codex_proxy('http://p:1', 'warmup 失败：必然 409 invalid_state')
    assert calls == [('409', 'http://p:1', 'codex')]

    calls.clear()
    codex_auth._record_codex_proxy('http://p:1', 'curl: (7) CONNECT tunnel failed, response 403.')
    assert calls == [('transport', 'http://p:1', 'health')]

    calls.clear()
    codex_auth._record_codex_proxy('http://p:1', 'synthetic business failure')
    assert calls == [('fail', 'http://p:1', 'codex')]


def test_proxy_bookkeeping_failure_never_breaks_the_job(monkeypatch, tmp_path):
    """记账本身出错不能把作业带崩（代理账是旁路信息）。"""
    monkeypatch.setenv('SMSINVIT_INVITATION_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('CODEX_HOMES_ROOT', str(tmp_path / 'homes'))
    from webui import codex_auth, proxy_pool

    def boom(*args, **kwargs):
        raise RuntimeError('synthetic proxy pool failure')

    monkeypatch.setattr(proxy_pool, 'is_transport_error', boom)
    codex_auth._record_codex_proxy('http://p:1', 'whatever')   # 不抛


# ──────────────────────── 静默 SSO（不带 prompt=login） ────────────────────────


def test_codex_authorize_can_omit_prompt_login_for_silent_sso():
    """授权 URL 默认带 prompt=login（会强制重登、被打回 /log-in）；
    传空串时必须**不带 prompt**，让有效会话直接过（静默 SSO）。"""
    from auth_flow import AuthFlow

    # 直接建实例：_build_codex_authorize 只依赖两个纯函数助手，
    # 走 _new_flow 需要已配置的邀请仓库，这里没必要。
    flow = AuthFlow.__new__(AuthFlow)
    default_url, *_ = flow._build_codex_authorize()
    assert 'prompt=login' in default_url

    silent_url, *_ = flow._build_codex_authorize(prompt_override="")
    assert 'prompt=' not in silent_url
    assert 'code_challenge_method=S256' in silent_url and 'client_id=' in silent_url


def test_authorization_stage_asks_for_silent_sso(app):
    """邀请授权阶段必须请求静默 SSO —— 这是"直接开始接码拿 RT"的关键。"""
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.cli.return_value = {'ok': True, 'elapsed': 1, 'tokens': {'refresh_token': 'fixture-success-rt'}}
    out = invoke(app, False)
    assert out['ok']
    assert app.flow.oauth_codex_rt_exchange.call_count == 1
    assert app.flow.oauth_codex_rt_exchange.call_args.kwargs.get('prompt_override') == ''


# ──────────────────────── 代理传输错误 ≠ 账号不可用 ────────────────────────


def test_transport_error_is_recognized_and_never_treated_as_business_409():
    from webui import proxy_pool

    for text in ('Failed to perform, curl: (7) CONNECT tunnel failed, response 403.',
                 'curl: (28) Operation timed out', 'curl: (56) Recv failure',
                 'proxy error: 407 Proxy Authentication Required'):
        assert proxy_pool.is_transport_error(text), text
        assert not proxy_pool.is_409_error(text), text
    assert proxy_pool.is_transport_error('warmup 失败：必然 409 invalid_state') is False
    assert proxy_pool.is_transport_error('') is False


def test_rebuild_transport_failure_is_retryable_not_a_dead_account(app):
    """代理拒绝隧道时必须换代理重试，绝不能报"请重新注册该账号"。"""
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.flow._session_refreshed = False
    app.flow.from_existing_credentials = Mock()          # 刷新失败（同一个 403）
    app.flow._build_codex_authorize = Mock(
        return_value=('https://auth.openai.com/oauth/authorize?x=1', 'state', 'verifier',
                      'http://localhost:1455/auth/callback', 'fixture-client'))
    app.flow._follow_authorize_for_callback = Mock(
        side_effect=RuntimeError('Failed to perform, curl: (7) CONNECT tunnel failed, response 403.'))
    out = invoke(app, False)
    assert not out['ok']
    # 关键：它必须换代理重试（尝试次数 > 1），而不是 1 次就判死
    assert out['attempts'] > 1, out
    # 代理不可用记的是健康维度（不是业务 409，也不是普通 fail）
    app.auth.proxy_pool.record_transport_failure.assert_called()
    app.auth.proxy_pool.record_409.assert_not_called()


def test_broken_proxy_is_recorded_even_when_the_job_stops(app):
    """非重试的传输错误也必须记到代理账上（以前 break 在记账之前，坏代理永远不被记）。"""
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.flow._session_refreshed = False
    app.flow.from_existing_credentials = Mock()
    app.flow._build_codex_authorize = Mock(
        side_effect=RuntimeError('curl: (7) CONNECT tunnel failed, response 403.'))
    out = invoke(app, False)
    assert not out['ok']
    app.auth.proxy_pool.record_transport_failure.assert_called()


# ──────────────────────── 进度阶梯（里程碑） ────────────────────────


def test_progress_ladder_matches_the_agreed_milestones():
    """进度必须落在约定里程碑上：10 接受邀请 / 30 接受成功 / 40 PoW /
    45 接码 / 55 收到码 / 60 结算 / 70 取授权 / 80 授权成功 / 90 / 95 / 100。"""
    from webui import codex_auth

    expected = {
        'accepting_invite': ('正在接受邀请', 10),
        'invite_accepted': ('接受邀请成功', 30),
        'sentinel_pow': ('获取 Sentinel Token (PoW)', 40),
        'sms_phone': ('命中手机验证，正在短信接码', 45),
        'sms_code': ('已收到验证码，正在验证', 55),
        'sms_settled': ('业务验证成功，平台结算已确认', 60),
        'codex_authorizing': ('正在获取 Codex 授权', 70),
        'codex_oauth': ('Codex 授权成功', 80),
        'cli_say_hi': ('正在发送消息', 90),
        'export': ('正在生成凭证', 95),
        'done': ('完成', 100),
    }
    for key, value in expected.items():
        assert codex_auth.STAGES[key] == value, key


def test_accept_success_is_emitted_at_30_percent(app):
    """接受邀请成功后必须推到 30%（跳过接受邀请时也要推）。"""
    app.saved.update(email=EMAIL, invite_processed_at=1, invite_link=LINK)
    app.cli.return_value = {'ok': True, 'elapsed': 1, 'tokens': {'refresh_token': 'fixture-success-rt'}}
    out = invoke(app, False)
    assert out['ok']
    progress = [pct for name, pct in
                [(stage, app.auth.USER_STAGE_PCT.get(stage)) for stage in app.stages]]
    assert 30 in progress, app.stages
    assert 80 in progress and 90 in progress and 95 in progress and 100 in progress, app.stages


def test_add_phone_is_explicitly_logged():
    """真的要走 add-phone 时，日志必须点明（否则只能看到"TOTP 通过"后突然租号）。"""
    import inspect

    from auth_flow import AuthFlow

    source = inspect.getsource(AuthFlow._handle_add_phone_verification)
    assert '命中手机号验证（add-phone）' in source
    assert 'emit_task_stage("sms_phone")' in source or '_emit_stage("sms_phone")' in source

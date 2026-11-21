"""Manual proxy diagnostics; transports, clock, and DNS are simulated."""
import json
import socket
from types import SimpleNamespace

import pytest
from webui import proxy_test as checks


PROXY = 'http://synthetic-user:synthetic-secret@proxy.example.test:8080'


def response(status=200, data=None):
    return SimpleNamespace(status_code=status, json=lambda: data if data is not None else {'ip': '203.0.113.42'})


@pytest.fixture
def sessions(monkeypatch):
    seen = []
    outcomes = []
    def create(proxy):
        adapter = SimpleNamespace(max_retries=3)
        session = SimpleNamespace(proxy=proxy, closed=False, adapters={'https://': adapter})
        def request(method, target, **kwargs):
            seen.append((proxy, method, target, kwargs, adapter.max_retries))
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        session.request = request
        session.get = lambda *a, **k: pytest.fail('The nested TLS retry wrapper must not be used')
        session.close = lambda: setattr(session, 'closed', True)
        made.append(session)
        return session
    made = []
    monkeypatch.setattr(checks, 'create_http_session', create)
    return SimpleNamespace(seen=seen, made=made, outcomes=outcomes)


def test_success_uses_one_guarded_get_without_redirects_or_nested_retries(sessions):
    sessions.outcomes.append(response())
    result = checks.test_proxy(PROXY, timeout=12)
    assert result['ok'] and result['status'] == 'success' and result['reachable']
    assert result['error'] == '' and result['ip'] == '203.0.113.42'
    assert result['attempts'] == 1
    _, method, target, options, retries = sessions.seen[0]
    assert method == 'GET' and target == checks.DEFAULT_TARGET
    assert options == {'timeout': pytest.approx(6), 'allow_redirects': False}
    assert retries == 0 and sessions.made[0].closed


@pytest.mark.parametrize('exception, category', [
    (ValueError('private-fixture-token forbidden destination'), 'policy'),
    (socket.gaierror('private-fixture-token DNS failed'), 'dns'),
    (RuntimeError('curl: (6) Could not resolve host private-fixture-token'), 'dns'),
    (RuntimeError('CONNECT tunnel failed, response 407 private-fixture-token'), 'proxy_auth'),
    (RuntimeError('curl: (35) SSL connect error private-fixture-token'), 'tls'),
    (RuntimeError('curl: (60) certificate error private-fixture-token'), 'tls'),
])
def test_non_protocol_failures_are_safe_and_do_not_retry_or_change_scheme(sessions, exception, category):
    sessions.outcomes.append(exception)
    result = checks.test_proxy(PROXY)
    assert not result['ok'] and result['status'] == 'failed'
    assert result['category'] == category and result['attempts'] == 1
    assert 'private-fixture-token' not in json.dumps(result)
    assert 'synthetic-secret' not in json.dumps(result)
    assert len(sessions.seen) == 1 and sessions.made[0].closed


def test_alternate_success_has_no_stale_error_and_closes_both_sessions(sessions):
    sessions.outcomes.extend([RuntimeError('curl: (56) Proxy CONNECT aborted'), response()])
    result = checks.test_proxy(PROXY)
    assert result['ok'] and result['category'] == 'none' and result['error'] == ''
    assert result['alt_scheme'].startswith('socks5h://')
    assert result['attempts'] == 2 and result['tried'] == [session.proxy for session in sessions.made]
    assert all(session.closed for session in sessions.made)


@pytest.mark.parametrize('status', [301, 403, 429, 503])
def test_target_http_failure_is_reachable_without_claiming_full_success(sessions, status):
    sessions.outcomes.append(response(status))
    result = checks.test_proxy(PROXY)
    assert not result['ok'] and result['reachable'] and result['status'] == 'reachable'
    assert result['category'] == 'target_http' and result['http_status'] == status
    assert result['attempts'] == 1 and sessions.made[0].closed


def test_proxy_auth_http_status_is_not_target_reachability(sessions):
    sessions.outcomes.append(response(407))
    result = checks.test_proxy(PROXY)
    assert result['category'] == 'proxy_auth' and not result['reachable']
    assert result['attempts'] == 1


@pytest.mark.parametrize('data', [{}, {'ip': 'not-an-IP'}, ['203.0.113.1'], None])
def test_http200_invalid_ipify_response_is_not_success(sessions, data):
    if data is None:
        def malformed(): raise ValueError('HTML response')
        sessions.outcomes.append(SimpleNamespace(status_code=200, json=malformed))
    else:
        sessions.outcomes.append(response(data=data))
    result = checks.test_proxy(PROXY)
    assert not result['ok'] and result['reachable']
    assert result['category'] == 'invalid_response' and result['attempts'] == 1


def test_both_attempts_share_one_total_budget(monkeypatch):
    clock = [1000.]
    calls = []
    monkeypatch.setattr(checks.time, 'monotonic', lambda: clock[0])
    def probe(proxy, target, timeout):
        calls.append(timeout)
        clock[0] += timeout
        return checks._failure('timeout', 'safe timeout')
    monkeypatch.setattr(checks, '_probe', probe)
    result = checks.test_proxy(PROXY, timeout=8)
    assert calls == pytest.approx([4, 2, 2])
    assert result['attempts'] == 3 and result['latency_ms'] == 8000


def test_https_alternate_success_clears_http_and_socks_errors(sessions):
    sessions.outcomes.extend([RuntimeError('curl: (56) CONNECT aborted'),
                             RuntimeError('curl: (97) SOCKS closed'), response()])
    result = checks.test_proxy(PROXY)
    assert result['ok'] and result['error'] == '' and result['attempts'] == 3
    assert result['alt_scheme'].startswith('https://')
    assert all(session.closed for session in sessions.made)


def test_exhausted_budget_does_not_start_alternate(monkeypatch):
    clock = [1000.]
    calls = []
    monkeypatch.setattr(checks.time, 'monotonic', lambda: clock[0])
    def probe(proxy, target, timeout):
        calls.append(proxy)
        clock[0] += 9
        return checks._failure('timeout', 'safe timeout')
    monkeypatch.setattr(checks, '_probe', probe)
    assert checks.test_proxy(PROXY, timeout=8)['attempts'] == 1
    assert len(calls) == 1


def test_socks5_normalizes_to_socks5h_and_alternate_http(sessions):
    sessions.outcomes.extend([RuntimeError('SOCKS handshake error'), response()])
    proxy = PROXY.replace('http://', 'socks5://')
    result = checks.test_proxy(proxy)
    assert [session.proxy.split('://')[0] for session in sessions.made] == ['socks5h', 'http']
    assert result['ok'] and result['alt_scheme'] == PROXY


def test_probe_retains_outbound_guard_and_tls(monkeypatch):
    from smsinvit.invitation import safety
    from curl_cffi import CurlOpt
    guards = []
    def url_guard(url):
        guards.append('target')
        return 'api.ipify.org', 443, ('203.0.113.10',)
    def proxy_guard(proxy):
        guards.append('proxy')
        return 'proxy.example.test', 8080, ('203.0.113.11',)
    def transfer(session, method, url, **kwargs):
        assert method == 'GET' and kwargs['verify'] is True
        assert kwargs['allow_redirects'] is False
        assert kwargs['proxies'] == {'https': PROXY, 'http': PROXY}
        assert session.trust_env is False
        assert session.curl_options[CurlOpt.SSL_VERIFYPEER] == 1
        assert session.curl_options[CurlOpt.SSL_VERIFYHOST] == 2
        assert session.curl_options[CurlOpt.PROXY_SSL_VERIFYPEER] == 1
        kwargs['content_callback'](b'{"ip":"203.0.113.42"}')
        return response()
    monkeypatch.setenv('HTTPS_PROXY', 'http://untrusted-env.example.test:8080')
    monkeypatch.setattr(safety, 'require_url', url_guard)
    monkeypatch.setattr(safety, 'require_proxy', proxy_guard)
    monkeypatch.setattr(safety._cffi.Session, 'request', transfer)
    assert checks.test_proxy(PROXY)['ok']
    assert guards == ['target', 'proxy', 'proxy']

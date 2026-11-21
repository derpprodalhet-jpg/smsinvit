from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from smsinvit.app import common_rate
from smsinvit.errors import Error
from test_common_security import config, app_for_boundary


@pytest.mark.parametrize('method,path,bucket,limit', [
    ('GET', '/api/v1/admin/auth/captcha', 'admin.auth.captcha', 30),
    ('POST', '/api/v1/admin/auth/refresh', 'admin.auth.refresh', 60),
    ('GET', '/api/v1/sms/sessions/synthetic', 'public.v2.sms.read.ip', 6000),
    ('POST', '/api/v1/sms/webhook/synthetic', 'sms.webhook', 300),
])
def test_new_rate_buckets_do_not_include_secret_paths(method, path, bucket, limit):
    calls = []
    request = SimpleNamespace(method=method, url=SimpleNamespace(path=path), state=SimpleNamespace(principal=None, client_ip='203.0.113.5'),client=None)
    common_rate(request, SimpleNamespace(limiter=SimpleNamespace(check=lambda *args: calls.append(args))))
    assert (bucket + ':203.0.113.5', limit, 60) in calls
    assert all('synthetic' not in call[0] for call in calls)


def test_sensitive_success_error_and_unexpected_error_headers(config):
    client, _, _ = app_for_boundary(config)
    @client.app.get('/api/v1/test/error')
    def fail(): raise RuntimeError('private credential must not leak')
    for path in ['/api/v1/admin/auth/me', '/api/v1/test/error']:
        result = client.get(path)
        assert result.status_code in (401, 500)
        assert result.headers['cache-control'] == 'no-store'
        assert result.headers['referrer-policy'] == 'no-referrer'
        assert "script-src 'self'" in result.headers['content-security-policy']
        assert 'private credential' not in result.text


def test_limited_poll_returns_retry_after_before_business(config):
    client, _, services = app_for_boundary(config)
    calls = []
    @client.app.get('/api/v1/sms/sessions/synthetic')
    def poll(): calls.append('business'); return {'ok': True}
    def limited(*_):
        error = Error('RATE_LIMIT_EXCEEDED', '稍后重试', 429)
        error.retry_after = 60
        raise error
    services.limiter.check = limited
    response = client.get('/api/v1/sms/sessions/synthetic')
    assert response.status_code == 429 and not calls
    assert response.headers['retry-after'] == '60' and response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('chunked', [False, True])
def test_oversized_webhook_rejected_before_limiter_or_business(config, chunked):
    client, db, services = app_for_boundary(config)
    called = []
    @client.app.post('/api/v1/sms/webhook/synthetic')
    def callback(request: Request): called.append(True); return {'ok': True}
    content = iter([b'x'*32768, b'x'*32769]) if chunked else b'x'*65537
    response = client.post('/api/v1/sms/webhook/synthetic', content=content)
    assert response.status_code == 413
    assert not called and not db.calls and not services.limiter.calls

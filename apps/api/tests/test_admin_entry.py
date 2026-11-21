"""Exercise real routing, including static-file bypasses of a custom entry."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from smsinvit.app import Services, create_app
from smsinvit.config import Config
from smsinvit.frontend import admin_path


def client_for(tmp_path, entry='/admin-v1/'):
    static = tmp_path / 'static'
    (static / 'admin/assets').mkdir(parents=True)
    (static / 'assets').mkdir()
    (static / 'index.html').write_text('<html><head></head><body>public app</body></html>')
    (static / 'admin/index.html').write_text('<html><head><script type="module" src="./assets/main.js"></script></head><body>admin app</body></html>')
    (static / 'admin/assets/main.js').write_text('const adminBundle = true;')
    (static / 'assets/public.js').write_text('const publicBundle = true;')
    config = Config({'smsinvit.inventory-encryption-key': 'a1'*32,
                     'smsinvit.inventory-fingerprint-key': 'synthetic-fingerprint',
                     'smsinvit.jwt-secret': 'synthetic-jwt-secret-at-least-32-characters',
                     'SMSINVIT_STATIC_DIR': str(static), 'runtime.secure_cookies': False,
                     'smsinvit.admin-path': entry}, environ={})
    db = SimpleNamespace(one=lambda *args: None, all=lambda *args: [], execute=lambda *args: None)
    services = Services(db, config)
    services.limiter = SimpleNamespace(check=lambda *args: None)
    return TestClient(create_app(config, db, services=services, include_domains=False, load_runtime=False))


@pytest.mark.parametrize('entry', ['/admin/', '/admin-v1/', '/console_2026/'])
def test_entry_assets_deep_links_and_security_headers(tmp_path, entry):
    client = client_for(tmp_path, entry)
    result = client.get(entry[:-1] + '?from=bookmark', follow_redirects=False)
    assert result.status_code == 308 and result.headers['location'] == entry + '?from=bookmark'
    for suffix in ('', 'login', 'security', 'invitation/runs?status=done', 'index.html'):
        response = client.get(entry + suffix)
        assert response.status_code == 200 and 'admin app' in response.text
        assert response.text.index('<base href="' + entry) < response.text.index('<script')
        assert response.headers['cache-control'] == 'no-store'
        assert "base-uri 'self'" in response.headers['content-security-policy']
        assert "script-src 'self';" in response.headers['content-security-policy']
    bundle = client.get(entry + 'assets/main.js')
    assert bundle.status_code == 200 and 'adminBundle' in bundle.text
    assert 'immutable' in bundle.headers['cache-control']
    assert client.get(entry + 'assets/missing.js').status_code == 404
    assert client.get('/supprot_codex').status_code == 200
    assert client.get('/api/v1/admin/auth/me').status_code == 401


@pytest.mark.parametrize('path', ['/admin', '/admin/', '/admin/login', '/admin/index.html',
    '/admin/assets/main.js', '/admin/invitation/native', '/admin-v10/login', '/old-console/login',
    '/admin-v1/assets/%2e%2e/index.html', '/admin-v1/%2e%2e/admin/index.html',
    '/admin-v1/assets/%252e%252e/main.js', '/admin-v1/assets/%5cmain.js'])
def test_old_entry_and_encoded_or_direct_file_bypass_return_404(tmp_path, path):
    response = client_for(tmp_path).get(path, follow_redirects=False)
    assert response.status_code == 404
    assert 'admin app' not in response.text and 'adminBundle' not in response.text
    assert '/admin-v1/' not in response.text


def test_retired_home_moves_only_under_active_prefix(tmp_path):
    client = client_for(tmp_path)
    response = client.get('/admin-v1/invitation/native', follow_redirects=False)
    assert response.status_code == 308 and response.headers['location'] == '/admin-v1/invitation'
    assert client.get('/admin-v1/invitation/native/assets/old.js').status_code == 404


@pytest.mark.parametrize('value', ['', '/', '//host/path', '/api/', '/assets', '/health', '/supprot_sim/',
    '/invitation', '/two/parts', '/admin?x=1', '/admin#hash', '/%61dmin', '/a..b', '/a\\b',
    '/x\n', '/x'*40, '/api/v1/admin'])
def test_invalid_entry_fails_before_serving(value):
    with pytest.raises(ValueError):
        admin_path(Config({'smsinvit.admin-path': value}, environ={}))


def test_environment_entry_overrides_file_without_runtime_setting():
    config = Config({'smsinvit.admin-path': '/admin/'}, environ={'SMSINVIT_ADMIN_PATH': '/admin-v1'})
    config.runtime['smsinvit.admin-path'] = '/ignored/'
    assert admin_path(config) == '/admin-v1/'

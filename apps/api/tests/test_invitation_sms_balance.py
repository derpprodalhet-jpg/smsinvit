"""Exercise the saved-config balance endpoint and real provider request adapters."""
import ast
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from requests.exceptions import Timeout

from smsinvit.invitation import mysql
import sms_provider


ROOT = Path(__file__).resolve().parents[3]
PLATFORMS = ('smsbower', 'herosms', 'smspool')
LABELS = dict(zip(PLATFORMS, ('SmsBower', 'HeroSMS', 'SMSPool')))
HOSTS = {'smsbower.page': 'smsbower', 'hero-sms.com': 'herosms', 'api.smspool.net': 'smspool'}


def load_balance_endpoint(config):
    # Compile the actual route body without importing app.py's executor startup.
    source = ROOT / 'vendor/codex_invit/webui/app.py'
    tree = ast.parse(source.read_text(), filename=str(source))
    route = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'api_test_sms')
    route.decorator_list = []
    prices = ast.parse((ROOT / 'vendor/codex_invit/webui/sms_prices.py').read_text())
    configured = next(node for node in prices.body if isinstance(node, ast.FunctionDef) and node.name == 'platform_key_configured')
    globals_ = {'time': time, 'db': SimpleNamespace(get_sms_internal_config=lambda: config)}
    exec(compile(ast.Module(body=[configured], type_ignores=[]), str(source), 'exec'), globals_)
    globals_['sms_prices'] = SimpleNamespace(PLATFORMS=PLATFORMS, PLATFORM_LABELS=LABELS,
                                           platform_key_configured=globals_['platform_key_configured'])
    exec(compile(ast.Module(body=[route], type_ignores=[]), str(source), 'exec'), globals_)
    return globals_['api_test_sms']


@pytest.fixture
def config(monkeypatch):
    saved = {'sms_api_key_' + platform: 'private-fixture-key-' + platform for platform in PLATFORMS}
    saved.update(sms_platform_mode='auto', sms_provider='herosms', sms_country='6')
    monkeypatch.setattr(mysql, '_redaction_values', set(saved.values()))
    return saved


def response(balance):
    return SimpleNamespace(text='ACCESS_BALANCE:' + str(balance), raise_for_status=lambda: None)


def platform_for_url(url):
    from urllib.parse import urlsplit
    return HOSTS[urlsplit(url).hostname]


def test_all_three_saved_keys_are_queried_concurrently_and_zero_balance_is_success(config, monkeypatch):
    barrier = threading.Barrier(3)
    calls = []
    balances = {'smsbower': 0, 'herosms': 2.5, 'smspool': 9}
    def get(url, *, params, timeout, proxies):
        platform = platform_for_url(url)
        calls.append((platform, dict(params), timeout))
        barrier.wait(timeout=3)
        return response(balances[platform])
    monkeypatch.setattr(sms_provider.requests, 'get', get)
    before = dict(config)
    result = load_balance_endpoint(config)()
    assert result['ok'] is result['all_ok'] is True
    assert list(result['platforms']) == list(PLATFORMS)
    assert len(calls) == 3 and config == before
    for platform, params, timeout in calls:
        assert params == {'action': 'getBalance', 'api_key': config['sms_api_key_' + platform]}
        assert timeout == 10
        assert result['platforms'][platform] == {
            'label': LABELS[platform], 'configured': True, 'status': 'success',
            'ok': True, 'balance': float(balances[platform]),
            'elapsed_ms': result['platforms'][platform]['elapsed_ms'], 'error': '',
        }
        assert result['platforms'][platform]['elapsed_ms'] >= 0
    assert all(config['sms_api_key_' + platform] not in json.dumps(result) for platform in PLATFORMS)


def test_one_platform_timeout_keeps_other_balances_and_redacts_private_key(config, monkeypatch):
    calls = []
    def get(url, *, params, timeout, proxies):
        platform = platform_for_url(url)
        calls.append((platform, timeout))
        if platform == 'herosms':
            raise Timeout('timeout querying https://hero-sms.com/?api_key=' + params['api_key'])
        return response(1 if platform == 'smsbower' else 3)
    monkeypatch.setattr(sms_provider.requests, 'get', get)
    result = load_balance_endpoint(config)()
    assert result['ok'] is True and result['all_ok'] is False
    assert sorted(calls) == sorted((platform, 10) for platform in PLATFORMS)
    assert result['platforms']['herosms']['status'] == 'failed'
    assert result['platforms']['herosms']['balance'] is None
    assert 'timeout' in result['platforms']['herosms']['error']
    assert '[REDACTED]' in result['platforms']['herosms']['error']
    assert result['platforms']['smsbower']['balance'] == 1
    assert result['platforms']['smspool']['balance'] == 3
    assert all(config['sms_api_key_' + platform] not in json.dumps(result) for platform in PLATFORMS)


def test_unconfigured_platform_has_no_request_and_legacy_shared_key_is_never_used(config, monkeypatch):
    config.update(sms_api_key_herosms=' ', sms_api_key_smspool='', sms_api_key='unsafe-legacy-key')
    calls = []
    def get(url, *, params, timeout, proxies):
        calls.append(platform_for_url(url))
        return response(4)
    monkeypatch.setattr(sms_provider.requests, 'get', get)
    result = load_balance_endpoint(config)()
    assert calls == ['smsbower']
    assert result['all_ok'] is False
    for platform in ('herosms', 'smspool'):
        entry = result['platforms'][platform]
        assert entry['configured'] is False and entry['ok'] is False
        assert entry['status'] == 'unconfigured' and entry['balance'] is None
        assert entry['elapsed_ms'] == 0 and entry['error'] == '未配置 API Key'


@pytest.mark.parametrize('invalid', ['nan', 'inf', '-1', 'not-a-number'])
def test_invalid_supplier_balance_is_failed_without_affecting_other_platforms(config, monkeypatch, invalid):
    def get(url, *, params, timeout, proxies):
        return response(invalid if platform_for_url(url) == 'smspool' else 5)
    monkeypatch.setattr(sms_provider.requests, 'get', get)
    result = load_balance_endpoint(config)()
    assert result['all_ok'] is False
    assert result['platforms']['smspool']['status'] == 'failed'
    assert result['platforms']['smspool']['balance'] is None
    assert result['platforms']['smsbower']['status'] == 'success'
    assert result['platforms']['herosms']['status'] == 'success'

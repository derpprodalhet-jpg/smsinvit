"""Fresh SMS quote refreshes: mocked suppliers and in-memory cache only."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
import threading
from types import SimpleNamespace

import pytest
import sms_provider
from smsinvit.db import Database, TransactionState, owner_identity
from smsinvit.invitation.runtime import TASK_SETTINGS
from webui import sms_prices as prices


@pytest.fixture
def cache(monkeypatch):
    cfg = {'sms_service': 'dr', 'sms_platform_mode': 'auto',
           'sms_auto_min_stock': 20, 'sms_auto_top_n': 60,
           'sms_price_poll_seconds': 5, 'sms_use_monitor_best': True,
           **{'sms_api_key_' + pk: 'offline-key' for pk in prices.PLATFORMS}}
    saved, states, balances = {}, {}, {}
    lock = threading.Lock()
    def upsert(pk, service, rows, ok=True, error=''):
        with lock:
            if ok:
                saved[pk] = [dict(row, platform=pk, service=service,
                                  updated_at=prices.time.time()) for row in rows]
            states[pk] = {'platform': pk, 'last_error': error}
    monkeypatch.setattr(prices.db, 'get_sms_internal_config', lambda: dict(cfg))
    monkeypatch.setattr(prices.db, 'sms_price_upsert', upsert)
    monkeypatch.setattr(prices.db, 'sms_price_state_set', lambda pk, **kw: balances.update({pk: kw}))
    monkeypatch.setattr(prices.db, 'sms_price_list', lambda **kw: [r for group in saved.values() for r in group])
    monkeypatch.setattr(prices.db, 'sms_price_state_list', lambda: list(states.values()))
    monkeypatch.setattr(prices.db, 'sms_stats_list', lambda: [])
    monkeypatch.setattr(sms_provider.requests, 'get', lambda *a, **kw: pytest.fail('Live network forbidden'))
    return SimpleNamespace(cfg=cfg, saved=saved, states=states, balances=balances)


def response(data=None, text=''):
    return SimpleNamespace(json=lambda: data, text=text, raise_for_status=lambda: None)


def test_quotes_run_in_parallel_with_ten_second_bound_and_no_balance(cache, monkeypatch):
    barrier = threading.Barrier(3)
    calls = []
    marker = ContextVar('quote_test_context', default=None)
    database = Database.__new__(Database)
    database._active = ContextVar('quote_test_database_transaction', default=None)
    transaction = TransactionState(object(), owner_identity())
    cache.cfg['sms_service'] = 'global-service'
    monkeypatch.setattr(prices.db, 'get_sms_internal_config',
                        lambda: {**cache.cfg, **(TASK_SETTINGS.get() or {})})
    def get(url, *, params, timeout, proxies):
        calls.append((params['action'], timeout, params['service'], marker.get(),
                      database._state(), TASK_SETTINGS.get()))
        barrier.wait(timeout=3)
        return response({'52': {'dr': {'cost': .08, 'count': 50}}})
    monkeypatch.setattr(sms_provider.requests, 'get', get)
    token = marker.set('original-task')
    db_token = database._active.set(transaction)
    task_token = TASK_SETTINGS.set({'sms_service': 'dr'})
    try:
        out = prices.SmsPriceMonitor().refresh_once()
        assert database._state() is transaction
    finally:
        TASK_SETTINGS.reset(task_token)
        database._active.reset(db_token)
        marker.reset(token)
    assert list(out) == list(prices.PLATFORMS)
    assert all(row['ok'] for row in out.values())
    assert calls == [('getPrices', 10, 'dr', None, None, None)] * 3
    assert all(row['service'] == 'dr' for group in cache.saved.values() for row in group)
    assert not cache.balances
    assert len(prices.effective_params({**cache.cfg, 'sms_service': 'dr'})['rental_candidates']) == 3


def test_full_snapshot_preserves_allowed_country_after_cheapest_sixty(cache, monkeypatch):
    cache.cfg.update(sms_allowed_countries='187', sms_min_price=.05, sms_max_price=.10)
    cache.cfg['sms_api_key_herosms'] = cache.cfg['sms_api_key_smspool'] = ''
    data = {str(i): {'dr': {'cost': .05, 'count': 0}} for i in range(60)}
    data['187'] = {'dr': {'cost': .08, 'count': 50}}
    monkeypatch.setattr(sms_provider.requests, 'get', lambda *a, **kw: response(data))
    result = prices.SmsPriceMonitor().refresh_once()
    assert result['smsbower']['rows'] == 61
    assert len(cache.saved['smsbower']) == 61
    assert prices.effective_params(cache.cfg)['rental_candidates'] == [
        {'platform': 'smsbower', 'country': '187', 'max_price': .08}]
    # Existing hard limits remain authoritative.
    cache.cfg['sms_auto_min_stock'] = 51
    assert prices.effective_params(cache.cfg)['rental_candidates'] == []
    cache.cfg.update(sms_auto_min_stock=20, sms_allowed_countries='52')
    assert prices.effective_params(cache.cfg)['rental_candidates'] == []
    cache.cfg['sms_allowed_countries'] = '187'
    cache.cfg['sms_max_price'] = .07
    assert prices.effective_params(cache.cfg)['rental_candidates'] == []


def test_failed_platform_does_not_invalidate_other_platform_quotes(cache, monkeypatch):
    def get(url, *, params, timeout, proxies):
        if 'hero-sms.com' in url:
            raise TimeoutError('mocked quote timeout')
        return response({'52': {'dr': {'cost': .08, 'count': 50}}})
    monkeypatch.setattr(sms_provider.requests, 'get', get)
    out = prices.SmsPriceMonitor().refresh_once()
    assert not out['herosms']['ok']
    assert {r['platform'] for r in prices.evaluate(cache.cfg)['candidates']} == {'smsbower', 'smspool'}


def test_overlapping_same_config_calls_join_one_quote_round(cache, monkeypatch):
    entered = threading.Event()
    follower_waiting = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(3, action=entered.set)
    calls = []
    class ObservedFuture(Future):
        def result(self, timeout=None):
            follower_waiting.set()
            return super().result(timeout)
    monkeypatch.setattr(prices, 'Future', ObservedFuture)
    def get(url, *, params, timeout, proxies):
        calls.append(params['action'])
        barrier.wait(timeout=3)
        assert release.wait(3)
        return response({'52': {'dr': {'cost': .08, 'count': 50}}})
    monkeypatch.setattr(sms_provider.requests, 'get', get)
    monitor = prices.SmsPriceMonitor()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(monitor.refresh_once)
        assert entered.wait(3)
        second = pool.submit(monitor.refresh_once)
        assert follower_waiting.wait(3)
        release.set()
        assert first.result(3) == second.result(3)
    assert calls == ['getPrices'] * 3
    assert monitor._refresh_future is None


def test_balances_have_separate_readonly_parallel_refresh(cache, monkeypatch):
    barrier = threading.Barrier(3)
    calls = []
    def get(url, *, params, timeout, proxies):
        calls.append((params['action'], timeout))
        barrier.wait(timeout=3)
        return response(text='ACCESS_BALANCE:4.5')
    monkeypatch.setattr(sms_provider.requests, 'get', get)
    assert all(prices.SmsPriceMonitor().refresh_balances().values())
    assert calls == [('getBalance', 10)] * 3
    assert all(row['balance'] == 4.5 for row in cache.balances.values())
    assert not cache.saved


def test_quote_refresh_rejects_supplier_write_actions(cache):
    provider = prices.SmsPriceMonitor._readonly_provider('smsbower', cache.cfg, 'getPrices')
    with pytest.raises(RuntimeError, match='只读'):
        provider._request({'action': 'getNumber'})


def test_monitor_updates_prices_before_independent_balances(cache, monkeypatch):
    monitor = prices.SmsPriceMonitor()
    calls = []
    monkeypatch.setattr(monitor, 'refresh_once', lambda: calls.append('prices'))
    monkeypatch.setattr(monitor, 'interval', lambda: 5)
    def balances():
        calls.append('balances')
        monitor._stop.set()
    monkeypatch.setattr(monitor, 'refresh_balances', balances)
    monitor._loop()
    assert calls == ['prices', 'balances']


@pytest.mark.parametrize('mode, expected', [
    ('smsbower,herosms', [('herosms', '52', .06), ('smsbower', '52', .07),
                         ('herosms', '187', .08), ('smsbower', '187', .09)]),
    ('herosms', [('herosms', '52', .06), ('herosms', '187', .08)]),
    ('auto', [('smspool', '52', .01), ('herosms', '52', .06), ('smsbower', '52', .07),
              ('herosms', '187', .08), ('smsbower', '187', .09)]),
])
def test_selected_platforms_share_one_ranking_and_rental_order(cache, mode, expected):
    cache.cfg.update(sms_platform_mode=mode, sms_price_weight=1,
                     sms_use_monitor_best=False, sms_provider='smspool',
                     sms_country='187', sms_max_price=.10)
    for pk, rows in {'smsbower': [('52', .07), ('187', .09)],
                     'herosms': [('52', .06), ('187', .08)],
                     'smspool': [('52', .01)]}.items():
        cache.saved[pk] = [dict(platform=pk, country=cid, price=price, count=50,
                                physical_count=None, updated_at=prices.time.time())
                           for cid, price in rows]
    evaluated = prices.evaluate(cache.cfg)['candidates']
    assert [(r['platform'], r['country'], r['price']) for r in evaluated] == expected
    effective = prices.effective_params(cache.cfg)
    assert [(r['platform'], r['country'], r['max_price'])
            for r in effective['rental_candidates']] == expected
    assert effective['platform_candidates'] == list(dict.fromkeys(pk for pk, _, _ in expected))
    assert effective['best']['platform'] == expected[0][0]


def test_csv_mode_normalizes_duplicates_without_expanding_unknown_scope(cache):
    cache.cfg['sms_platform_mode'] = ' HeroSMS , smsbower,HEROSMS '
    assert prices.platform_mode(cache.cfg) == 'herosms,smsbower'
    assert prices.selected_platforms(cache.cfg) == ('herosms', 'smsbower')
    cache.cfg['sms_platform_mode'] = 'unknown-platform'
    cache.saved['smsbower'] = [dict(platform='smsbower', country='52', price=.01,
                                  count=100, physical_count=None, updated_at=prices.time.time())]
    assert prices.selected_platforms(cache.cfg) == ()
    assert prices.effective_params(cache.cfg)['rental_candidates'] == []

"""统计端点：业务时区日界、按币种分组的成本、诚实计数与冻结契约字段。

SQL 在隔离 SQLite 上真实执行（stats_sqlite 只翻译 NOW(3)），
所以这里验证的是线上同一批 mapper SQL。
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stats_sqlite import isolated_stats_database
from smsinvit.app import Services, create_app
from smsinvit.common import ops as ops_module
from smsinvit.common.ops import Ops
from smsinvit.config import Config
from smsinvit.errors import Error, error_body
from smsinvit.security import PermissionService, Principal

SHANGHAI = ZoneInfo('Asia/Shanghai')
# 业务日 2026-10-11（Asia/Shanghai）= UTC 2026-10-10 16:00 → 2026-10-11 16:00
START = datetime(2026, 10, 10, 16, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 11, 16, 0, 0, tzinfo=timezone.utc)
START_EPOCH = START.timestamp()
END_EPOCH = END.timestamp()

MODULES = ['sms', 'invitation']
COST_KEYS = ['sms', 'codex_phone']
PENDING_KEYS = ['sms_uncertain', 'codex_settlement_review']
COST_FIELDS = {'key', 'label', 'currency', 'totalMinor', 'todayMinor', 'basis', 'available',
               'availableSince', 'unpricedCount', 'detailPath'}
MODULE_FIELDS = {'module', 'label', 'generatedTotal', 'generatedToday', 'redeemedTotal',
                 'redeemedToday', 'expired', 'expiredApplicable'}


def make_ops(db):
    config = SimpleNamespace(get=lambda key, default=None: 'Asia/Shanghai' if key == 'runtime.business_time_zone' else default)
    services = SimpleNamespace(db=db, config=config)
    return raw_ops(services)


def raw_ops(services):
    """真实 Ops（window 走配置时区），路由测试与日界测试都用它。"""
    instance = Ops(services)
    instance.s.ops = instance
    return instance


def frozen_ops(db):
    """固定业务日窗口的 Ops，供各项统计断言使用。"""
    instance = make_ops(db)
    instance.window = lambda: (START, END, SHANGHAI)
    return instance


@pytest.fixture
def stats(tmp_path):
    db = isolated_stats_database(tmp_path / 'stats.sqlite3')
    instance = frozen_ops(db)
    yield SimpleNamespace(db=db, ops=instance, services=instance.s)
    db.close()


def cost(out, key, currency=None):
    rows = [row for row in out['costs'] if row['key'] == key and (currency is None or row['currency'] == currency)]
    return rows


# ──────────────────────── 业务时区日界 ────────────────────────


def test_window_converts_the_configured_zone_to_a_utc_business_day(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 10, 16, 1, 0, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(ops_module, 'datetime', FrozenDatetime)
    instance = make_ops(SimpleNamespace())
    start, end, zone = instance.window()
    assert str(zone) == 'Asia/Shanghai'
    assert start == datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)


def test_shanghai_midnight_boundary_splits_yesterday_from_today(stats):
    # 2026-10-10 23:59 Asia/Shanghai = 15:59Z（昨天）；00:01 = 16:01Z（今天）
    stats.db.execute("INSERT INTO sms_redeem_keys(id,status,created_at,expires_at) VALUES('y','UNUSED',?,?)",
                     datetime(2026, 10, 10, 15, 59), datetime(2030, 1, 1))
    stats.db.execute("INSERT INTO sms_redeem_keys(id,status,created_at,expires_at) VALUES('t','UNUSED',?,?)",
                     datetime(2026, 10, 10, 16, 1), datetime(2030, 1, 1))
    # 邀请卡密用 epoch 秒存 created_at/redeemed_at：同一条日界。
    stats.db.execute("INSERT INTO invitation_v2_cdk_codes(code,status,created_at) VALUES('c-y','unused',?)", START_EPOCH - 60)
    stats.db.execute("INSERT INTO invitation_v2_cdk_codes(code,status,created_at,redeemed_at) VALUES('c-t','used',?,?)", START_EPOCH + 60, START_EPOCH + 120)
    out = stats.ops.stats_overview()
    sms = next(m for m in out['cards']['modules'] if m['module'] == 'sms')
    invitation = next(m for m in out['cards']['modules'] if m['module'] == 'invitation')
    assert (sms['generatedTotal'], sms['generatedToday']) == (2, 1)
    assert (invitation['generatedTotal'], invitation['generatedToday']) == (2, 1)
    assert (invitation['redeemedTotal'], invitation['redeemedToday']) == (1, 1)


def test_empty_tables_report_zero_and_keep_one_row_per_fixed_currency(stats):
    out = stats.ops.stats_overview()
    assert [m['module'] for m in out['cards']['modules']] == MODULES
    assert all(row[key] == 0 for row in out['cards']['modules']
               for key in ('generatedTotal', 'generatedToday', 'redeemedTotal', 'redeemedToday', 'expired'))
    assert out['cards']['totals'] == dict.fromkeys(('generatedTotal', 'generatedToday', 'redeemedTotal', 'redeemedToday', 'expired'), 0)
    assert {(row['key'], row['currency']) for row in out['costs']} == {
        ('sms', 'USD'), ('codex_phone', 'USD')}
    assert all(row['totalMinor'] == 0 and row['todayMinor'] == 0 and row['unpricedCount'] == 0 for row in out['costs'])
    assert [row['key'] for row in out['pending']] == PENDING_KEYS
    assert all(row['count'] == 0 for row in out['pending'])
    assert out['businessTimeZone'] == 'Asia/Shanghai'
    assert out['generatedAt'].endswith('Z')


def test_invitation_has_no_expiry_metric(stats):
    out = stats.ops.stats_overview()
    invitation = next(m for m in out['cards']['modules'] if m['module'] == 'invitation')
    assert invitation['expiredApplicable'] is False and invitation['expired'] == 0
    # 过期只对真正有 expires_at 的模块适用，且只数未核销的。
    stats.db.execute("INSERT INTO sms_redeem_keys(id,status,created_at,expires_at) VALUES('e1','UNUSED',?,?)",
                     datetime(2026, 10, 1), datetime(2020, 1, 1))
    stats.db.execute("INSERT INTO sms_redeem_keys(id,status,created_at,expires_at) VALUES('e2','USED',?,?)",
                     datetime(2026, 10, 1), datetime(2020, 1, 1))
    out = stats.ops.stats_overview()
    sms = next(m for m in out['cards']['modules'] if m['module'] == 'sms')
    assert sms['expired'] == 1


# ──────────────────────── 多币种绝不求和 ────────────────────────


def test_currencies_are_reported_as_separate_rows_and_never_summed(stats):
    stats.db.execute("INSERT INTO invitation_v2_sms_orders(rental_id,run_id,platform,status,created_at,updated_at,activation_cost,currency) VALUES('u','r','smsbower','completed',?,?,0.25,'USD')", START_EPOCH + 1, START_EPOCH + 1)
    stats.db.execute("INSERT INTO invitation_v2_sms_orders(rental_id,run_id,platform,status,created_at,updated_at,activation_cost,currency) VALUES('e','r','smsbower','completed',?,?,0.10,'EUR')", START_EPOCH + 2, START_EPOCH + 2)
    out = stats.ops.stats_overview()
    codex = cost(out, 'codex_phone')
    assert {(row['currency'], row['totalMinor']) for row in codex} == {('USD', 25), ('EUR', 10)}
    assert sum(row['totalMinor'] for row in codex) == 35  # 两个币种各自成行，没有合并成一行


# ──────────────────────── 成本只算业务成功 ────────────────────────


@pytest.mark.parametrize('status,counted', [
    ('reserved', False), ('created', False), ('cancel_pending', False), ('cancelled', False),
    ('failed', False), ('rental_unknown', False), ('no_order', False),
    ('finish_pending', True), ('finish_processing', True), ('completed', True), ('finish_review', True)])
def test_codex_cost_only_counts_orders_from_business_success(stats, status, counted):
    stats.db.execute("INSERT INTO invitation_v2_sms_orders(rental_id,run_id,platform,status,created_at,updated_at,activation_cost,currency) VALUES(?,?,?,?,?,?,0.5,'USD')",
                     'r-' + status, 'r', 'smsbower', status, START_EPOCH + 5, START_EPOCH + 5)
    out = stats.ops.stats_overview()
    assert cost(out, 'codex_phone', 'USD')[0]['totalMinor'] == (50 if counted else 0)


def test_finish_review_is_costed_and_listed_once_in_pending(stats):
    for index, status in enumerate(['finish_pending', 'finish_processing', 'completed', 'finish_review', 'finish_review']):
        stats.db.execute("INSERT INTO invitation_v2_sms_orders(rental_id,run_id,platform,status,created_at,updated_at,activation_cost,currency) VALUES(?,?,?,?,?,?,0.25,'USD')",
                         'r%d' % index, 'r', 'smsbower', status, START_EPOCH + index, START_EPOCH + index)
    out = stats.ops.stats_overview()
    assert cost(out, 'codex_phone', 'USD')[0]['totalMinor'] == 125
    review = next(row for row in out['pending'] if row['key'] == 'codex_settlement_review')
    assert review['count'] == 2 and review['detailPath'] == '/invitation/runs'


def test_unpriced_codex_orders_are_counted_not_guessed(stats):
    stats.db.execute("INSERT INTO invitation_v2_sms_orders(rental_id,run_id,platform,status,created_at,updated_at,activation_cost,currency) VALUES('r1','r','smsbower','created',?,?,NULL,NULL)", START_EPOCH + 1, START_EPOCH + 1)
    stats.db.execute("INSERT INTO invitation_v2_sms_orders(rental_id,run_id,platform,status,created_at,updated_at,activation_cost,currency) VALUES('r2','r','smsbower','finish_pending',?,?,NULL,'USD')", START_EPOCH + 2, START_EPOCH + 2)
    row = cost(stats.ops.stats_overview(), 'codex_phone', 'USD')[0]
    assert row['totalMinor'] == 0 and row['unpricedCount'] == 1


def test_sms_cost_uses_minor_integers_and_only_success_sessions(stats):
    stats.db.execute("INSERT INTO sms_sessions(id,status,activation_cost,completed_at) VALUES('s1','SUCCESS',0.1234,?)", datetime(2026, 10, 10, 17, 0))
    stats.db.execute("INSERT INTO sms_sessions(id,status,activation_cost,completed_at) VALUES('s2','FAILED',9.9999,?)", datetime(2026, 10, 10, 17, 1))
    stats.db.execute("INSERT INTO sms_sessions(id,status,activation_cost,completed_at) VALUES('s3','WAITING_CODE',1.0,?)", datetime(2026, 10, 10, 17, 2))
    row = cost(stats.ops.stats_overview(), 'sms', 'USD')[0]
    assert (row['totalMinor'], row['todayMinor'], row['unpricedCount']) == (12, 12, 0)



def test_pending_counts_read_the_same_tables_the_contract_names(stats):
    stats.db.execute("INSERT INTO sms_sessions(id,status) VALUES('p1','ACQUIRE_UNKNOWN')")
    stats.db.execute("INSERT INTO sms_sessions(id,status) VALUES('p2','CANCEL_PENDING')")
    stats.db.execute("INSERT INTO invitation_v2_sms_orders(rental_id,run_id,platform,status,created_at,updated_at,activation_cost,currency) VALUES('rev','r','smsbower','finish_review',?,?,0.1,'USD')", START_EPOCH + 3, START_EPOCH + 3)
    out = stats.ops.stats_overview()
    counts = {row['key']: row['count'] for row in out['pending']}
    assert counts == {'sms_uncertain': 2, 'codex_settlement_review': 1}


# ──────────────────────── 冻结契约 + 权限 + 缓存 ────────────────────────


class Grants:
    def __init__(self, allowed):
        self.allowed = set(allowed)

    def all(self, sql, *args):
        return [{'permission_scope': 'ALL'}] if args[-1] in self.allowed else []


def client_for(stats, role=None, grants=('inventory.read',)):
    app = FastAPI()
    services = stats.services
    services.permissions = PermissionService(Grants(grants))
    app.state.services = services

    @app.middleware('http')
    async def principal_fixture(request, call_next):
        if role:
            request.state.principal = Principal('fixture-user', 'fixture-session', role, False)
        return await call_next(request)

    @app.exception_handler(Error)
    async def error_handler(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse(error_body(error), status_code=error.status)

    app.include_router(ops_module.router)
    return TestClient(app)


def test_stats_overview_route_is_permission_and_contract_frozen(stats):
    with client_for(stats, role=None) as anonymous:
        response = anonymous.get('/api/v1/admin/stats/overview')
        assert response.status_code == 401 and response.json()['error']['code'] == 'AUTH_REQUIRED'
    with client_for(stats, role=('ADMIN',), grants=()) as denied:
        response = denied.get('/api/v1/admin/stats/overview')
        assert response.status_code == 403 and response.json()['error']['code'] == 'PERMISSION_DENIED'
    with client_for(stats, role=('ADMIN',)) as allowed:
        response = allowed.get('/api/v1/admin/stats/overview')
        assert response.status_code == 200
        body = response.json()
        assert body['success'] is True
        data = body['data']
        assert set(data) == {'businessTimeZone', 'generatedAt', 'cards', 'costs', 'pending'}
        assert set(data['cards']) == {'modules', 'totals'}
        assert set(data['cards']['totals']) == {'generatedTotal', 'generatedToday', 'redeemedTotal', 'redeemedToday', 'expired'}
        assert [row['module'] for row in data['cards']['modules']] == MODULES
        for row in data['cards']['modules']:
            assert set(row) == MODULE_FIELDS
        assert [row['key'] for row in data['costs']] == COST_KEYS
        for row in data['costs']:
            assert set(row) == COST_FIELDS
        assert [row['key'] for row in data['pending']] == PENDING_KEYS
        for row in data['pending']:
            assert set(row) == {'key', 'label', 'count', 'detailPath'}


def test_stats_overview_is_served_no_store_through_the_real_app(tmp_path, stats):
    """真实 create_app：路由 + 权限 + 全局 Cache-Control 都按契约生效。"""
    static = tmp_path / 'static'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<html></html>')
    config = Config({'smsinvit.inventory-encryption-key': 'a1' * 32,
                     'smsinvit.inventory-fingerprint-key': 'synthetic-fingerprint',
                     'smsinvit.jwt-secret': 'synthetic-jwt-secret-at-least-32-characters',
                     'SMSINVIT_STATIC_DIR': str(static), 'runtime.secure_cookies': False,
                     'runtime.totp_enabled': False, 'runtime.cors_origins': 'http://localhost:3001'}, environ={})
    services = Services(stats.db, config)
    instance = Ops(services)
    instance.window = lambda: (START, END, SHANGHAI)
    services.ops = instance
    services.permissions = PermissionService(Grants(('inventory.read',)))
    services.auth = SimpleNamespace(authenticate=lambda token: Principal('fixture-user', 'fixture-session', ('ADMIN',), False))
    services.limiter = SimpleNamespace(check=lambda *args: None)
    app = create_app(config, stats.db, services=services, include_domains=False, load_runtime=False)
    with TestClient(app) as client:
        assert client.get('/api/v1/admin/stats/overview').status_code == 401
        response = client.get('/api/v1/admin/stats/overview', headers={'Authorization': 'Bearer fixture'})
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['data']['businessTimeZone'] == 'Asia/Shanghai'
        assert response.json()['data']['pending'][0]['key'] == 'sms_uncertain'


def test_cost_rows_carry_the_frozen_labels_paths_and_honesty_flags(stats):
    out = stats.ops.stats_overview()
    rows = {row['key']: row for row in out['costs']}
    assert rows['sms']['label'] == '接码（美区 GPT）' and rows['sms']['currency'] == 'USD'
    assert rows['sms']['detailPath'] == '/sms/redeems'
    assert rows['codex_phone']['label'] == 'codex 邮箱接码' and rows['codex_phone']['availableSince'] == '2026-10-11'
    assert rows['codex_phone']['detailPath'] == '/invitation/runs'
    assert all(row['available'] is True for row in out['costs'])
    assert all(row['basis'] for row in out['costs'])


# ──────────────────────── 与 dashboard/summary 一致 ────────────────────────


def test_dashboard_summary_reuses_the_same_sql_and_agrees_with_the_overview(stats):
    stats.db.execute("INSERT INTO sms_redeem_keys(id,status,created_at,used_at,expires_at) VALUES('s1','USED',?,?,?)",
                     datetime(2026, 10, 10, 17, 0), datetime(2026, 10, 10, 18, 0), datetime(2030, 1, 1))
    stats.db.execute("INSERT INTO sms_sessions(id,status,activation_cost,completed_at) VALUES('ss1','SUCCESS',0.25,?)", datetime(2026, 10, 10, 17, 0))
    stats.db.execute("INSERT INTO sms_sessions(id,status) VALUES('ss2','ACQUIRE_UNKNOWN')")
    summary = stats.ops.summary()
    overview = stats.ops.stats_overview()
    sms = next(row for row in overview['cards']['modules'] if row['module'] == 'sms')
    rows = {row['key']: row for row in overview['costs']}
    pending = {row['key']: row['count'] for row in overview['pending']}
    assert summary['smsUsedToday'] == sms['redeemedToday'] == 1
    assert summary['smsSupplierCostToday'] == rows['sms']['todayMinor'] / 100 == 0.25
    assert summary['smsStickySessions'] == pending['sms_uncertain'] == 1
    assert summary['businessTimeZone'] == overview['businessTimeZone'] == 'Asia/Shanghai'


def test_summary_keeps_only_the_sms_and_invitation_fields(stats):
    summary = stats.ops.summary()
    # GPT 直充整块删除后，summary 的字段集合就是这份白名单：多一个少一个都算契约变更。
    assert set(summary) == {'businessTimeZone', 'generatedTotal', 'generatedToday', 'smsKeyCounts',
                            'invitationRunCounts', 'invitationConfirmedToday', 'smsUsedToday', 'smsFailedToday',
                            'smsSupplierCostToday', 'smsStickySessions', 'jobCounts'}
    assert summary['smsSupplierCostToday'] == 0
    assert summary['jobCounts'] == {}

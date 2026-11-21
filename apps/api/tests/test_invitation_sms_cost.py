"""codex 邮箱接码成本：金额取自供应商响应，缺失时回退行情价，并随租号一起落库。

全部离线：供应商 HTTP 用替身，数据库用隔离 SQLite（V9 + V19 列）。
"""
from types import SimpleNamespace

import pytest

import sms_provider as sp
from invitation_sms_sqlite import isolated_sms_database
from smsinvit.invitation import mysql, sms_state
from smsinvit.invitation.runtime import STAGE_EMITTER
from smsinvit.security import SecretCodec


class State:
    """sms_state 的最小替身：只记录租号登记参数。"""

    def __init__(self):
        self.created = []
        self.slots = {}
        self.claims = set()

    def reserve_rental(self, platform, action):
        ident = 'r%d' % len(self.slots)
        self.slots[ident] = 'reserved'
        return ident

    def rental_no_order(self, ident):
        self.slots[ident] = 'no_order'

    def rental_unknown(self, ident, reason):
        self.slots[ident] = 'unknown'

    def rental_created(self, ident, aid, phone, *, activation_cost=None, currency=None):
        self.slots[ident] = 'created'
        self.created.append((ident, aid, phone, activation_cost, currency))

    def claim_phone(self, phone):
        self.claims.add(phone)
        return True

    def order_state(self, *args, **kwargs):
        return None


class Response:
    status_code = 200

    def __init__(self, text='', data=None):
        self.text = text
        self.data = data

    def json(self):
        import json
        return self.data if self.data is not None else json.loads(self.text)

    def raise_for_status(self):
        pass


def provider(platform='smsbower', max_price=0, state=None):
    state = state or State()
    config = {'sms_api_key_' + platform: 'offline-test-key', 'sms_service': 'dr',
              'sms_country': '52', 'sms_max_price': max_price, '_sms_state': state}
    return sp.create_sms_provider(platform, config), state, config


def with_supplier(p, monkeypatch, action_response):
    calls = []

    def request(params, **kwargs):
        calls.append(dict(params))
        answer = action_response.get(params['action'])
        if answer is None:
            pytest.fail('Unexpected supplier action ' + params['action'])
        return answer

    monkeypatch.setattr(p, '_request', request)
    return calls


V2 = lambda data: {'getNumberV2': Response(data=data)}  # noqa: E731
V1 = lambda text: {'getNumber': Response(text=text)}  # noqa: E731


# ──────────────────────── 取金额与回退 ────────────────────────


def test_get_number_v2_uses_the_supplier_activation_cost(monkeypatch):
    p, state, _ = provider()
    with_supplier(p, monkeypatch, V2({'activationId': '1', 'phoneNumber': '66812345678', 'activationCost': '0.1234'}))
    assert p.get_number(service='dr', country='52').phone_number == '+66812345678'
    assert state.created == [('r0', '1', '+66812345678', 0.1234, 'USD')]


def test_get_number_v2_missing_cost_falls_back_to_the_country_quote(monkeypatch):
    p, state, _ = provider()
    with_supplier(p, monkeypatch, V2({'activationId': '1', 'phoneNumber': '66812345678'}))
    p.get_number(service='dr', country='52', extra={'maxPrice': 0.30, 'maxPriceByCountry': {'52': 0.36}})
    assert state.created[0][3] == 0.36


def test_get_number_v2_falls_back_to_the_global_quote_then_min_price(monkeypatch):
    p, state, _ = provider()
    with_supplier(p, monkeypatch, V2({'activationId': '1', 'phoneNumber': '66812345678'}))
    p.get_number(service='dr', country='52', extra={'maxPrice': 0.30, 'minPrice': 0.10})
    assert state.created[0][3] == 0.30
    p2, state2, _ = provider()
    with_supplier(p2, monkeypatch, V2({'activationId': '2', 'phoneNumber': '66812345679'}))
    p2.get_number(service='dr', country='52', extra={'minPrice': 0.10})
    assert state2.created[0][3] == 0.10


def test_get_number_without_an_amount_uses_the_pre_order_quote(monkeypatch):
    """getNumber 只回 ACCESS_NUMBER:aid:phone，没有金额，只能回退行情价。"""
    p, state, _ = provider()
    with_supplier(p, monkeypatch, {'getNumberV2': Response('BAD_ACTION'), 'getNumber': Response('ACCESS_NUMBER:7:66812345678')})
    p.get_number(service='dr', country='52', extra={'maxPrice': 0.30, 'maxPriceByCountry': {'52': 0.36}})
    assert state.created[0][1:] == ('7', '+66812345678', 0.36, 'USD')


@pytest.mark.parametrize('reported', ['', '  ', 'abc', None, -1, float('nan'), float('inf'), float('-inf'), {'amount': 1}, [1]])
def test_invalid_supplier_amounts_never_become_a_cost(monkeypatch, reported):
    p, state, _ = provider()
    with_supplier(p, monkeypatch, V2({'activationId': '1', 'phoneNumber': '66812345678', 'activationCost': reported}))
    p.get_number(service='dr', country='52', extra={'maxPrice': 0.30})
    assert state.created[0][3] == 0.30  # 回退报价，绝不把非法金额当成本


def test_unknown_amount_stays_none_instead_of_zero(monkeypatch):
    p, state, _ = provider()
    with_supplier(p, monkeypatch, V2({'activationId': '1', 'phoneNumber': '66812345678'}))
    p.get_number(service='dr', country='52')
    assert state.created[0][3] is None and state.created[0][4] == 'USD'


# ──────────────────────── 日志与阶段事件 ────────────────────────


def controller(p, config, monkeypatch, logs, **kwargs):
    monkeypatch.setattr(sp, 'create_sms_provider', lambda *args: p)
    instance = sp.PhoneCallbackController(p.platform, config, service='dr', country='52', log_fn=lambda message: None, **kwargs)
    instance.log = logs.append
    return instance


def test_rental_and_code_logs_carry_the_amount_and_the_stage_events_do_too(monkeypatch):
    p, state, config = provider()
    with_supplier(p, monkeypatch, {**V2({'activationId': '1', 'phoneNumber': '66812345678', 'activationCost': '0.1234'}),
                                   'getStatus': Response('STATUS_OK:123456')})
    logs, stages = [], []
    token = STAGE_EMITTER.set(lambda name, **fields: stages.append((name, fields)))
    try:
        instance = controller(p, config, monkeypatch, logs)
        assert instance.get_phone() == '+66812345678'
        assert instance.get_code() == '123456'
    finally:
        STAGE_EMITTER.reset(token)
    assert logs[0] == '已租到新号码，平台=smsbower 国家=52 泰国 号码=+66812345678，金额=0.1234 USD'
    assert logs[1] == '已收到验证码：123456（号码=+66812345678，金额=0.1234 USD）'
    assert ('sms_phone', {'amount': 0.1234, 'currency': 'USD'}) in stages
    assert ('sms_code', {'amount': 0.1234, 'currency': 'USD'}) in stages
    assert state.created[0][3] == 0.1234


def test_legacy_one_argument_stage_emitters_still_receive_the_stage_name(monkeypatch):
    """历史发射器只接受阶段名：带金额的调用必须退回单参数，而不是丢事件。"""
    p, _, config = provider()
    with_supplier(p, monkeypatch, {**V2({'activationId': '1', 'phoneNumber': '66812345678', 'activationCost': 1}),
                                   'getStatus': Response('STATUS_OK:123456')})
    names = []
    token = STAGE_EMITTER.set(names.append)
    try:
        instance = controller(p, config, monkeypatch, [])
        instance.get_phone()
        instance.get_code()
    finally:
        STAGE_EMITTER.reset(token)
    assert names == ['sms_phone', 'sms_code']


# ──────────────────────── 落库（V19 列） ────────────────────────


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    db = isolated_sms_database(tmp_path / 'sms-cost.sqlite3')
    monkeypatch.setattr(mysql, '_services', None)
    monkeypatch.setattr(mysql, '_runtime_owner', None)
    monkeypatch.setattr(mysql, '_cache', {})
    monkeypatch.setattr(mysql, '_redaction_values', set())
    mysql.configure(SimpleNamespace(db=db, codec=SecretCodec('c1' * 32, 'isolated-sms-cost-fingerprints')))
    yield db
    db.close()


def test_rental_created_persists_cost_and_currency_with_the_order(state_db):
    with sms_state.task_run('run-cost'):
        rental = sms_state.reserve_rental('smsbower', 'getNumberV2')
    sms_state.rental_created(rental, 'activation-cost', '+12025550101', activation_cost=0.25, currency='usd')
    row = state_db.one('SELECT status,activation_cost,currency FROM invitation_v2_sms_orders WHERE rental_id=?', rental)
    assert row['status'] == 'created' and float(row['activation_cost']) == 0.25 and row['currency'] == 'USD'


@pytest.mark.parametrize('cost', [None, -1, float('nan'), float('inf'), 'nope', True])
def test_rental_created_never_invents_a_cost(state_db, cost):
    with sms_state.task_run('run-cost'):
        rental = sms_state.reserve_rental('smsbower', 'getNumber')
    sms_state.rental_created(rental, 'activation-' + str(cost), '+12025550102', activation_cost=cost, currency='')
    row = state_db.one('SELECT activation_cost,currency FROM invitation_v2_sms_orders WHERE rental_id=?', rental)
    assert row['activation_cost'] is None and row['currency'] is None

"""Invitation SMS contracts: entirely simulated suppliers, no network or DB writes."""
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pytest
import sms_provider as sp
from smsinvit.invitation.sms_state import SmsBudgetExhausted, RentalUncertainError


class State:
    def __init__(self):
        self.slots = {}; self.calls = []; self.claims = set(); self.states = []; self.fail_persist = False
    def reserve_rental(self, platform, action):
        self.calls.append((platform, action))
        if sum(status != 'no_order' for status in self.slots.values()) >= 3:
            raise SmsBudgetExhausted('budget')
        ident = str(len(self.slots) + 1); self.slots[ident] = 'reserved'; return ident
    def rental_no_order(self, ident): self.slots[ident] = 'no_order'
    def rental_unknown(self, ident, reason): self.slots[ident] = 'unknown'
    def rental_created(self, ident, aid, phone, **cost): self.slots[ident] = 'created'
    def claim_phone(self, phone):
        if phone in self.claims: return False
        self.claims.add(phone); return True
    def order_state(self, platform, aid, status, error=''):
        if self.fail_persist: raise RuntimeError('storage unavailable')
        self.states.append((platform, aid, status))


class Response:
    status_code = 200
    def __init__(self, text='', data=None): self.text = text; self.data = data
    def json(self):
        if self.data is not None: return self.data
        return json.loads(self.text)
    def raise_for_status(self): pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(sp.requests, 'get', lambda *a, **k: pytest.fail('live network forbidden'))


def provider(platform, state=None, max_price=0):
    state = state or State()
    cfg = {'sms_api_key_' + platform: 'offline-test-key', 'sms_service': 'dr',
           'sms_country': '52', 'sms_max_price': max_price, '_sms_state': state}
    return sp.create_sms_provider(platform, cfg), cfg, state


def supplier(p, monkeypatch, *, numbers=None, cancel='ACCESS_CANCEL', finish='ACCESS_ACTIVATION'):
    calls = []; number_list = list(numbers or ['66812345678']); counter = 0
    def request(params, **kwargs):
        nonlocal counter
        calls.append(dict(params)); action = params['action']
        if action == 'getBalance': return Response('ACCESS_BALANCE:4.75')
        if action == 'getPrices': return Response(data={'52': {'dr': {'cost': .2, 'count': 30}}})
        if action in ('getNumber', 'getNumberV2'):
            counter += 1; phone = number_list[min(counter - 1, len(number_list) - 1)]
            if action == 'getNumberV2': return Response(data={'activationId': str(counter), 'phoneNumber': phone})
            return Response(f'ACCESS_NUMBER:{counter}:{phone}')
        if action == 'getStatus': return Response('STATUS_OK:123456')
        if action == 'setStatus':
            return Response({1: 'ACCESS_READY', 6: finish, 8: cancel}[params['status']])
        pytest.fail('Unexpected supplier action ' + action)
    monkeypatch.setattr(p, '_request', request)
    return calls


@pytest.mark.parametrize('platform', ['smsbower', 'herosms', 'smspool'])
def test_three_provider_protocols_one_code_no_resend(platform, monkeypatch):
    p, _, state = provider(platform); calls = supplier(p, monkeypatch)
    assert p.get_balance() == 4.75
    assert p.get_top_countries('dr')[0]['country'] == '52'
    assert not state.calls
    act = p.get_number(service='dr', country='52')
    assert act.phone_number == '+66812345678'
    p.mark_send_succeeded(act.activation_id)
    assert p.get_code(act.activation_id, timeout=1) == '123456'
    assert p.get_code(act.activation_id, timeout=1) == ''
    p.mark_code_failed(act.activation_id); p.request_resend_sms(act.activation_id)
    assert p.report_success(act.activation_id)
    assert p.cancel(act.activation_id)
    assert len(state.calls) == 1
    assert not any(row.get('status') == 3 for row in calls)
    assert not any(row['action'] in ('finishActivation', 'cancelActivation') for row in calls)
    assert calls[2]['action'] == ('getNumberV2' if platform == 'smsbower' else 'getNumber')


@pytest.mark.parametrize('platform', ['smsbower', 'herosms', 'smspool'])
def test_hard_price_ceiling_and_country_zero(platform, monkeypatch):
    p, _, _ = provider(platform, max_price=.10); calls = supplier(p, monkeypatch)
    p.get_number(service='dr', country='0', extra={'maxPrice': .30, 'maxPriceByCountry': {'0': .36}})
    assert calls[0]['maxPrice'] == .10
    assert calls[0]['country'] == '0'


@pytest.mark.parametrize('platform', ['smsbower', 'herosms', 'smspool'])
def test_uncertain_purchase_never_falls_back(platform, monkeypatch):
    p, _, state = provider(platform); request = Mock(side_effect=TimeoutError('timeout'))
    monkeypatch.setattr(p, '_request', request)
    with pytest.raises(RentalUncertainError):
        p.get_number(service='dr', country_candidates=['52', '7'])
    assert request.call_count == 1
    assert list(state.slots.values()) == ['unknown']


def test_explicit_no_order_releases_budget_and_allows_v1(monkeypatch):
    p, _, state = provider('smsbower')
    request = Mock(side_effect=[Response('BAD_ACTION'), Response('ACCESS_NUMBER:one:66812345678')])
    monkeypatch.setattr(p, '_request', request)
    assert p.get_number(service='dr', country='52').activation_id == 'one'
    assert list(state.slots.values()) == ['no_order', 'created']


def test_invalid_number_and_reservation_failure_never_calls_next_supplier(monkeypatch):
    p, _, state = provider('smsbower')
    monkeypatch.setattr(p, '_request', Mock(return_value=Response('unparseable')))
    with pytest.raises(RentalUncertainError): p.get_number(service='dr', country='52')
    assert list(state.slots.values()) == ['unknown']
    state.slots = {'1': 'created', '2': 'created', '3': 'created'}
    p._request.reset_mock()
    with pytest.raises(SmsBudgetExhausted): p.get_number(service='dr', country='52')
    p._request.assert_not_called()


@pytest.mark.parametrize('platform', ['smsbower', 'herosms'])
def test_http200_is_not_finish_confirmation(platform, monkeypatch):
    p, _, _ = provider(platform)
    monkeypatch.setattr(p, '_request', Mock(return_value=Response('BAD_ACTION')))
    assert p.report_success('one') is False
    assert p._request.call_args.args[0] == {'action': 'setStatus', 'id': 'one', 'status': 6}


def controller(p, cfg, monkeypatch, **kwargs):
    monkeypatch.setattr(sp, 'create_sms_provider', lambda *args: p)
    return sp.PhoneCallbackController(p.platform, cfg, service='dr', country='52', log_fn=lambda msg: None, **kwargs)


@pytest.mark.parametrize('platform', ['smsbower', 'herosms', 'smspool'])
def test_all_cancel_pending_can_continue_only_after_durable_record(platform, monkeypatch):
    from webui import sms_refund
    p, cfg, state = provider(platform); supplier(p, monkeypatch, numbers=['66812345678', '66812345679'])
    monkeypatch.setattr(p, 'cancel', lambda aid: False)
    monkeypatch.setattr(sms_refund, 'enqueue', lambda *a: 1)
    ctrl = controller(p, cfg, monkeypatch)
    first = ctrl.get_phone(); assert ctrl.cleanup()
    assert ctrl.activation is None and ctrl.provider is None and not ctrl.completed
    assert state.states[-1][-1] == 'cancel_pending'
    assert ctrl.get_phone() != first
    assert len(state.claims) == 2
    state.fail_persist = True
    assert ctrl.cleanup() is False
    with pytest.raises(RentalUncertainError): ctrl.get_phone()


def test_prefix_rejection_budget_never_returns_cancelled_number(monkeypatch):
    p, cfg, state = provider('smsbower'); supplier(p, monkeypatch, numbers=['52661234567'])
    ctrl = controller(p, cfg, monkeypatch, blocked_prefixes=['5266'], prefix_max_retry=99)
    with pytest.raises(SmsBudgetExhausted): ctrl.get_phone()
    assert len(state.calls) == 3 and ctrl.activation is None
    assert [row[-1] for row in state.states] == ['cancelled'] * 3


def test_finished_business_and_pending_platform_are_independent(monkeypatch):
    p, cfg, state = provider('smsbower'); supplier(p, monkeypatch, finish='BAD_ACTION')
    ctrl = controller(p, cfg, monkeypatch); ctrl.get_phone(); ctrl.report_success()
    assert ctrl.completed and state.states[-1][-1] == 'finish_pending'
    cancel = Mock(); monkeypatch.setattr(p, 'cancel', cancel)
    assert ctrl.cleanup(); cancel.assert_not_called()
    assert ctrl.activation is None


def test_finish_record_failure_keeps_business_success(monkeypatch):
    p, cfg, state = provider('smsbower'); supplier(p, monkeypatch)
    messages = []
    ctrl = controller(p, cfg, monkeypatch); ctrl.log = messages.append; ctrl.get_phone()
    state.fail_persist = True; ctrl.report_success()
    assert ctrl.completed and any('人工核对' in message for message in messages)
    assert ctrl.cleanup()


def test_physical_inventory_zero_is_preserved():
    assert sp._norm_price_row('52', {'cost': .2, 'count': 50, 'physicalCount': 0})['physical_count'] == 0
    assert sp._parse_sms_status_text("STATUS_OK:'123456'")['code'] == '123456'


def test_effective_rentals_share_filtered_fresh_candidates(monkeypatch):
    from webui import sms_prices as prices
    now = 1000
    cfg = {'sms_service': 'dr', 'sms_platform_mode': 'auto', 'sms_strict_whitelist': True,
        'sms_max_price': .25, 'sms_auto_max_price': .30, 'sms_price_margin': .10,
        'sms_auto_min_stock': 20, 'sms_use_monitor_best': True,
        **{'sms_api_key_' + p: 'fake' for p in prices.PLATFORMS}}
    def row(platform, country, price, **extra):
        return {'platform': platform, 'country': country, 'price': price, 'count': 30,
                'physical_count': None, 'updated_at': now, **extra}
    rows = [row('smsbower', '7', .01), row('smsbower', '52', .20), row('herosms', '52', .10),
            row('smspool', '52', .05, updated_at=now-31)]
    monkeypatch.setattr(prices.time, 'time', lambda: now)
    monkeypatch.setattr(prices.db, 'sms_price_list', lambda **kw: rows)
    monkeypatch.setattr(prices.db, 'sms_stats_list', lambda: [])
    monkeypatch.setattr(prices.db, 'sms_price_state_list', lambda: [
        {'platform': 'herosms', 'last_error': 'upstream unavailable'}])
    effective = prices.effective_params(cfg)
    assert effective['rental_candidates'] == [{'platform': 'smsbower', 'country': '52', 'max_price': .25}]
    assert effective['best']['platform'] == 'smsbower'
    assert effective['country_candidates_by_platform'] == {'smsbower': ['52']}
    cfg['sms_api_key_smsbower'] = ''
    assert prices.effective_params(cfg)['rental_candidates'] == []


def test_sms_auth_uses_first_code_once_and_moves_to_new_number():
    from auth_flow import AuthFlow
    flow = AuthFlow.__new__(AuthFlow)
    flow._add_phone_send = Mock(return_value={'page': {'type': 'phone_otp_verification'}})
    flow._extract_page_type = lambda data: 'phone_otp_verification'
    flow._extract_continue_url_from_step = lambda data: data.get('continue_url', '')
    flow._normalize_continue_url = lambda value: value
    flow._phone_otp_validate = Mock(side_effect=[RuntimeError('invalid code'), {'continue_url': 'next'}])
    ctrl = SimpleNamespace(get_phone=Mock(side_effect=['+66812345678', '+66812345679']),
        get_code=Mock(side_effect=['123456', '654321']), mark_send_succeeded=Mock(),
        mark_code_failed=Mock(), cleanup=Mock(return_value=True), report_success=Mock())
    assert flow._do_sms_loop(ctrl) == 'next'
    assert [call.args[0] for call in flow._add_phone_send.call_args_list] == ['+66812345678', '+66812345679']
    assert [call.args[0] for call in flow._phone_otp_validate.call_args_list] == ['123456', '654321']
    assert ctrl.get_code.call_count == 2 and ctrl.cleanup.call_count == 1


def test_sms_budget_does_not_fall_back_to_environment():
    from auth_flow import AuthFlow
    flow = AuthFlow.__new__(AuthFlow); flow._sms_callback = object()
    flow._handle_add_phone_via_sms = Mock(side_effect=SmsBudgetExhausted('budget'))
    flow._handle_add_phone_via_env = Mock()
    with pytest.raises(SmsBudgetExhausted): flow._handle_add_phone_verification()
    flow._handle_add_phone_via_env.assert_not_called()


def test_unknown_record_failure_is_still_fatal(monkeypatch):
    p, _, state = provider('smsbower')
    monkeypatch.setattr(state, 'rental_unknown', Mock(side_effect=RuntimeError('DB failure')))
    request = Mock(side_effect=TimeoutError('timeout')); monkeypatch.setattr(p, '_request', request)
    with pytest.raises(RentalUncertainError): p.get_number(service='dr', country_candidates=['52', '7'])
    assert request.call_count == 1


def test_failed_cancel_enqueue_blocks_replacement(monkeypatch):
    from webui import sms_refund
    p, cfg, state = provider('smsbower'); supplier(p, monkeypatch)
    monkeypatch.setattr(p, 'cancel', lambda aid: False)
    monkeypatch.setattr(sms_refund, 'enqueue', lambda *a: 0)
    ctrl = controller(p, cfg, monkeypatch); ctrl.get_phone()
    assert ctrl.cleanup() is False and ctrl.activation is not None
    with pytest.raises(RentalUncertainError): ctrl.get_phone()
    assert len(state.calls) == 1


def test_budget_shared_across_controllers(monkeypatch):
    p, cfg, state = provider('smsbower'); supplier(p, monkeypatch, numbers=['66812345671', '66812345672', '66812345673'])
    for _ in range(3):
        ctrl = controller(p, cfg, monkeypatch); assert ctrl.get_phone(); assert ctrl.cleanup()
    with pytest.raises(SmsBudgetExhausted): controller(p, cfg, monkeypatch).get_phone()
    assert len(state.claims) == 3


def test_settlement_worker_uses_claim_token_without_business_retry(monkeypatch):
    from webui import sms_refund
    monkeypatch.setattr(sms_refund.sms_state, 'settlement_due', lambda **kw: [
        {'id': 'order', 'platform': 'herosms', 'activation_id': 'one'}])
    monkeypatch.setattr(sms_refund.sms_state, 'settlement_claim', lambda ident: 'lease')
    complete = Mock(return_value={'status': 'completed'})
    monkeypatch.setattr(sms_refund.sms_state, 'settlement_result', complete)
    finish = Mock(return_value=True)
    monkeypatch.setattr(sms_refund, '_provider_for', lambda pk: SimpleNamespace(report_success=finish))
    assert sms_refund.run_settlements_once() == {'tried': 1, 'ok': 1, 'review': 0}
    finish.assert_called_once_with('one')
    complete.assert_called_once_with('order', True, '', claim='lease')


def test_smspool_already_settled_is_not_promised_as_refunded(monkeypatch):
    from webui import sms_refund
    p, cfg, state = provider('smspool'); supplier(p, monkeypatch, cancel='BAD_STATUS')
    enqueue = Mock(); monkeypatch.setattr(sms_refund, 'enqueue', enqueue)
    ctrl = controller(p, cfg, monkeypatch); messages = []; ctrl.log = messages.append
    ctrl.get_phone(); assert ctrl.get_code() == '123456'
    assert ctrl.cleanup()
    assert state.states[-1][-1] == 'completed'
    assert any('退款未成立' in message for message in messages)
    enqueue.assert_not_called()


def test_missing_candidates_refresh_only_once_and_do_not_rent(monkeypatch):
    from webui.sms_prices import MONITOR
    refresh = Mock(); monkeypatch.setattr(MONITOR, 'refresh_once', refresh)
    p, cfg, state = provider('smsbower')
    ctrl = controller(p, cfg, monkeypatch, effective_params_fn=lambda: {'rental_candidates': []})
    for _ in range(2):
        with pytest.raises(RuntimeError, match='新鲜'): ctrl.get_phone()
    refresh.assert_called_once_with()
    assert not state.calls


def test_readonly_probe_checks_three_platforms_without_orders_or_db_mutation(monkeypatch):
    from webui import sms_prices as prices
    states = {}; requests = []
    cfg = {'sms_service': 'dr', **{'sms_api_key_' + p: 'offline-key' for p in prices.PLATFORMS}}
    def factory(pk, cfg):
        p, _, state = provider(pk); states[pk] = state
        return p
    def http(url, params=None, **kwargs):
        requests.append(params['action'])
        assert kwargs['timeout'] == 10
        if params['action'] == 'getBalance': return Response('ACCESS_BALANCE:4.75')
        if params['action'] == 'getPrices': return Response(data={'52': {'dr': {'cost': .2, 'count': 30}}})
        pytest.fail('mutation attempted')
    monkeypatch.setattr(prices, 'create_sms_provider', factory)
    monkeypatch.setattr(sp.requests, 'get', http)
    monkeypatch.setattr(prices.db, 'sms_price_upsert', lambda *a, **kw: pytest.fail('DB mutation forbidden'))
    result = prices.probe_platforms(cfg)
    assert result['ok'] and set(result['platforms']) == set(prices.PLATFORMS)
    assert all(value['balance'] == 4.75 and value['price_rows'] == 1 and value['stock'] == 30
               for value in result['platforms'].values())
    assert sorted(requests) == ['getBalance'] * 3 + ['getPrices'] * 3
    assert all(not state.calls for state in states.values())

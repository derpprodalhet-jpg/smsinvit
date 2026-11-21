"""Real isolated transactions verify one-use phones and shared rental budgets."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import threading
from types import ModuleType, SimpleNamespace
import sys

import pytest

from invitation_sms_sqlite import isolated_sms_database
from smsinvit.invitation import mysql, sms_state
from smsinvit.invitation.runtime import ACTIVE_TASK, TASK_SETTINGS, TaskGate
from smsinvit.security import SecretCodec


@pytest.fixture
def state(tmp_path, monkeypatch):
    db = isolated_sms_database(tmp_path / 'sms-state.sqlite3')
    services = SimpleNamespace(db=db, codec=SecretCodec('a1' * 32, 'isolated-sms-fingerprints'))
    monkeypatch.setattr(mysql, '_services', None)
    monkeypatch.setattr(mysql, '_runtime_owner', None)
    monkeypatch.setattr(mysql, '_cache', {})
    monkeypatch.setattr(mysql, '_redaction_values', set())
    mysql.configure(services)
    yield services
    db.close()


def reserve(run, platform='smsbower', action='getNumber'):
    with sms_state.task_run(run):
        return sms_state.reserve_rental(platform, action)


def test_concurrent_controllers_platforms_and_proxy_attempts_share_three_rental_slots(state):
    run = 'shared-run-private-value'
    platforms = ('smsbower', 'herosms', 'smspool')
    def attempt(index):
        try:
            return reserve(run, platforms[index % 3])
        except sms_state.SmsBudgetExhausted:
            return None
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(80)))
    assert len([value for value in results if value]) == 3
    assert state.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets') == 3
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_orders') == 3
    # Reconstructed controllers and another task cannot reset this run's budget.
    with pytest.raises(sms_state.SmsBudgetExhausted):
        reserve(run, 'smspool')
    assert reserve('another-run')
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_budgets') == 2


def test_only_explicit_no_order_releases_one_slot_and_repeated_release_is_idempotent(state):
    one, two, three = [reserve('run') for _ in range(3)]
    sms_state.rental_created(one, 'activation-one', '+12025550101')
    sms_state.rental_unknown(two, 'response lost api_key=synthetic-secret')
    sms_state.rental_no_order(one)
    sms_state.rental_no_order(two)
    assert state.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets') == 3
    sms_state.rental_no_order(three)
    sms_state.rental_no_order(three)
    assert state.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets') == 2
    four = reserve('run', 'herosms')
    sms_state.rental_created(four, 'activation-four', '+12025550104')
    sms_state.order_state('herosms', 'activation-four', 'cancelled')
    with pytest.raises(sms_state.SmsBudgetExhausted):
        reserve('run')


def test_reservation_failure_rolls_back_counter_order_and_private_identifier(state, monkeypatch):
    execute = state.db.execute
    def fail_insert(sql, *args):
        if sql.startswith('INSERT INTO invitation_v2_sms_orders'):
            raise RuntimeError('synthetic order-storage failure')
        return execute(sql, *args)
    monkeypatch.setattr(state.db, 'execute', fail_insert)
    with pytest.raises(RuntimeError, match='order-storage failure'):
        reserve('rolled-back-private-run')
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_budgets') == 0
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_orders') == 0
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_secret_values') == 0


def test_phone_claim_is_global_atomic_and_permanent_after_failed_or_cancelled_orders(state):
    formats = ('+1 (202) 555-0109', '0012025550109', '12025550109')
    def claim(index):
        with sms_state.task_run('run-' + str(index)):
            return sms_state.claim_phone(formats[index % 3])
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(claim, range(60)))
    assert results.count(True) == 1
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_phone_uses') == 1
    rental = reserve('failed-run')
    sms_state.rental_created(rental, 'activation-private-value', formats[0])
    sms_state.order_state('smsbower', 'activation-private-value', 'cancelled')
    with sms_state.task_run('new-run-after-restart'):
        assert sms_state.claim_phone(formats[1]) is False
    assert sms_state.import_phone(formats[2], 'duplicate-history') is False


def test_persisted_phone_and_order_metadata_use_hmac_and_encryption(state):
    phone, run, activation = '+12025550125', 'private-budget-run', 'private-supplier-activation'
    rental = reserve(run)
    sms_state.rental_created(rental, activation, phone)
    with sms_state.task_run(run):
        assert sms_state.claim_phone(phone)
    expected = hmac.new(state.codec.fingerprint_key,
                        ('invitation:phone-use:v1:' + phone).encode(), hashlib.sha256).hexdigest()
    row = state.db.one('SELECT * FROM invitation_v2_sms_phone_uses')
    assert row['phone_fingerprint'] == expected
    stored = json.dumps([state.db.all('SELECT * FROM ' + table) for table in (
        'invitation_v2_sms_phone_uses', 'invitation_v2_sms_orders',
        'invitation_v2_sms_budgets', 'invitation_v2_secret_values')])
    assert all(value not in stored for value in (phone, run, activation))
    assert mysql.revealed(row['first_run_id']) == run


@pytest.mark.parametrize('phone', ['', '+0001234567', '+12345', '+1234567890123456',
                                  'user@1234567.test', 'code=1234567', '+1234567abc'])
def test_malformed_phone_never_enters_history_or_runtime_ledger(state, phone):
    with sms_state.task_run('run'), pytest.raises(ValueError):
        sms_state.claim_phone(phone)
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_phone_uses') == 0


def test_rental_and_phone_claim_require_task_context_and_nested_context_restores_on_error(state):
    assert sms_state.ACTIVE_SMS_RUN.get() is None
    with pytest.raises(RuntimeError, match='执行任务'):
        sms_state.reserve_rental('smsbower', 'getNumber')
    with pytest.raises(RuntimeError, match='执行任务'):
        sms_state.claim_phone('+12025550150')
    with sms_state.task_run('outer'):
        with pytest.raises(ValueError):
            with sms_state.task_run('inner'):
                assert sms_state.ACTIVE_SMS_RUN.get() == 'inner'
                raise ValueError('synthetic')
        assert sms_state.ACTIVE_SMS_RUN.get() == 'outer'
    assert sms_state.ACTIVE_SMS_RUN.get() is None
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_orders') == 0


def test_task_gate_keeps_run_context_across_nested_flows_and_cleans_up_exceptions(state):
    repo = SimpleNamespace(get_setting=lambda *args: '3',
                           get_cdk_code=lambda *args: {'email': 'gate@example.test'})
    gate = TaskGate(repo)
    seen = []
    def authorize_registered_email(email, *, run_id):
        seen.append((sms_state.ACTIVE_SMS_RUN.get(), ACTIVE_TASK.get()))
        reserve_id = sms_state.reserve_rental('herosms', 'getNumber')
        return reserve_id
    nested = gate.decorate(authorize_registered_email)
    def run_invite_job(code, *, run_id):
        assert sms_state.ACTIVE_SMS_RUN.get() == run_id
        nested('gate@example.test', run_id='nested-would-reset-budget')
        sms_state.reserve_rental('smsbower', 'getNumberV2')
        raise RuntimeError('synthetic task failure')
    with pytest.raises(RuntimeError, match='task failure'):
        gate.decorate(run_invite_job)('card-fixture', run_id='shared-invite-run')
    assert seen == [('shared-invite-run', True)]
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_budgets') == 1
    assert state.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets') == 2
    assert sms_state.ACTIVE_SMS_RUN.get() is None and ACTIVE_TASK.get() is False
    assert TASK_SETTINGS.get() is None
    assert gate.active == gate.inflight == 0 and not gate.emails


def test_registration_entrypoint_uses_positional_run_and_restores_thread_logging_state(state, monkeypatch):
    current = threading.local()
    current.run_id = 'previous-log-run'
    module = ModuleType('webui')
    module.registrar = SimpleNamespace(_current_run=current)
    monkeypatch.setitem(sys.modules, 'webui', module)
    gate = TaskGate(SimpleNamespace(get_setting=lambda *args: '3'))
    def _do_register(run_id, account):
        assert sms_state.ACTIVE_SMS_RUN.get() == current.run_id == run_id
        return sms_state.reserve_rental('smspool', 'getNumber')
    assert gate.decorate(_do_register)('registration-run', {'email': 'account@example.test'})
    assert current.run_id == 'previous-log-run'
    assert sms_state.ACTIVE_SMS_RUN.get() is None


def test_completed_and_cancelled_order_states_cannot_be_reopened(state):
    rental = reserve('run')
    sms_state.rental_created(rental, 'activation', '+12025550177')
    sms_state.order_state('smsbower', 'activation', 'completed')
    sms_state.order_state('smsbower', 'activation', 'cancel_pending', 'late response')
    assert state.db.scalar('SELECT status FROM invitation_v2_sms_orders WHERE rental_id=?', rental) == 'completed'


def test_settlement_claim_is_unique_and_stale_worker_cannot_overwrite_a_new_lease(state, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sms_state, 'time', SimpleNamespace(time=lambda: clock[0]))
    rental = reserve('settlement-run')
    sms_state.rental_created(rental, 'settlement-activation', '+12025550190')
    sms_state.order_state('smsbower', 'settlement-activation', 'finish_pending')
    clock[0] += 61
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: sms_state.settlement_claim(rental), range(20)))
    claims = [value for value in results if value]
    assert len(claims) == 1
    old_claim = claims[0]
    clock[0] += 121
    new_claim = sms_state.settlement_claim(rental)
    assert new_claim and new_claim != old_claim
    sms_state.settlement_result(rental, True, claim=old_claim)
    assert state.db.scalar('SELECT status FROM invitation_v2_sms_orders WHERE rental_id=?', rental) == 'finish_processing'
    sms_state.settlement_result(rental, False, 'not confirmed', claim=new_claim)
    assert state.db.scalar('SELECT status FROM invitation_v2_sms_orders WHERE rental_id=?', rental) == 'finish_pending'


def test_third_crashed_settlement_worker_becomes_review_without_more_external_attempts(state, monkeypatch):
    clock = [2000.0]
    monkeypatch.setattr(sms_state, 'time', SimpleNamespace(time=lambda: clock[0]))
    rental = reserve('crashed-settlement')
    sms_state.rental_created(rental, 'crashed-activation', '+12025550191')
    sms_state.order_state('smsbower', 'crashed-activation', 'finish_pending')
    for _ in range(3):
        clock[0] += 121
        assert sms_state.settlement_claim(rental)
    clock[0] += 121
    assert sms_state.settlement_due() == []
    row = state.db.one('SELECT status,attempts FROM invitation_v2_sms_orders WHERE rental_id=?', rental)
    assert row == {'status': 'finish_review', 'attempts': 3}
    assert sms_state.settlement_claim(rental) is None


def test_concurrent_pool_registrations_keep_separate_sms_budgets(state, tmp_path, monkeypatch):
    # 门卫对 _do_register 会 import webui.registrar（模块级读日志目录）
    monkeypatch.setenv('SMSINVIT_INVITATION_LOG_DIR', str(tmp_path / 'logs'))
    """多个账号**并发注册**时，各自必须有独立的接码预算与租号归属。

    注册阶段现在会顺手验手机（`_bind_phone_best_effort`），所以这条必须成立：
    门卫按任务设置 ACTIVE_SMS_RUN，预算表按 run_id 记，手机指纹去重是全局唯一的。
    """
    repo = SimpleNamespace(get_setting=lambda *args: '3',
                           get_cdk_code=lambda *args: {'email': 'pool@example.test'})
    gate = TaskGate(repo)
    seen = {}
    releases = threading.Barrier(2, timeout=10)

    # 函数名必须是 _do_register：门卫据此判定"这是账号工厂注册任务"并设置短信上下文
    def _do_register(run_id, account, options):
        marker = sms_state.ACTIVE_SMS_RUN.get()
        seen[run_id] = marker
        if str((options or {}).get('pool_only')):
            # 两个任务在"注册中途"同时租号：必须各自落在自己的 run 预算里
            releases.wait()
            sms_state.reserve_rental('smsbower', 'getNumber')
        return marker

    decorated = gate.decorate(_do_register)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(decorated, f'run-{i}', {'email': f'a{i}@example.test'},
                               {'pool_only': True}) for i in (1, 2)]
        markers = [f.result() for f in futures]
    assert len(set(markers)) == 2, markers                       # 每个任务自己的 run 标记
    assert seen['run-1'] == 'run-1' and seen['run-2'] == 'run-2'
    rows = state.db.all('SELECT run_id, reserved_count FROM invitation_v2_sms_budgets')
    assert len(rows) == 2 and all(int(r['reserved_count']) == 1 for r in rows), rows
    # 清理：任务结束后上下文不能残留（否则下一个任务会用错预算）
    assert sms_state.ACTIVE_SMS_RUN.get() is None


def test_concurrent_phone_claims_never_share_a_number(state):
    """并发注册时同一个号码只能被一个任务认领（全局唯一指纹）。"""
    results = []

    def claim(i):
        with sms_state.task_run(f'run-{i}'):
            results.append(sms_state.claim_phone('+12025550199'))
        return bool(results[-1])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(claim, range(4)))
    assert results.count(True) == 1, results
    assert state.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_phone_uses') == 1

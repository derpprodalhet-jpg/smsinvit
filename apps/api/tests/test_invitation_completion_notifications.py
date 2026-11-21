"""Completion/outbox transaction regressions; OAuth, CLI and Telegram are fixtures."""
import json

import pytest

from smsinvit.invitation import mysql
from test_invitation_codex_resume import app, invoke
from test_invitation_manual_sync import inv, success


def outbox(inv):return inv.db.all('SELECT * FROM completion_outbox')


@pytest.mark.parametrize('status',['unused','bound','queued','running','failed','review','void'])
def test_noncompletion_states_and_explicit_notifier_cannot_enqueue(inv,status):
    inv.repo.set_cdk_status(inv.code,status)
    with inv.db.transaction():inv.notify_card_redeemed(inv.code)
    assert outbox(inv) == []


def test_done_outbox_requires_same_transaction_and_repeated_done_is_idempotent(inv):
    with pytest.raises(RuntimeError):inv.notify_card_redeemed(inv.code)
    inv.repo.set_cdk_status(code=inv.code,status='done')
    inv.repo.set_cdk_status(inv.code,'done')
    inv.repo.set_cdk_status('NONEXISTENT-CARD','done')
    rows = outbox(inv)
    assert len(rows) == 1
    row = rows[0]
    marker = mysql.marker(inv.code)
    assert row['event_key'] == 'invitation.card-redeemed:'+marker
    assert row['reference_id'] is None and row['channel'] == 'TELEGRAM' and row['kind'] == 'CARD_REDEEMED'
    assert row['expires_at'] is None
    payload = json.loads(row['payload'])
    assert payload['chat'] == '-100123' and inv.email in payload['body']
    assert '【Codex 邀请】卡密核销完成' in payload['body'] and marker in payload['body']
    assert inv.code not in row['payload']


def test_outer_rollback_leaves_neither_done_nor_outbox_row(inv):
    with pytest.raises(ConnectionError):
        with inv.db.transaction():
            inv.repo.set_cdk_status(inv.code,'done')
            assert len(outbox(inv)) == 1
            raise ConnectionError('failure after completion but before commit')
    assert inv.repo.get_cdk_code(inv.code)['status'] == 'failed'
    assert outbox(inv) == []


def test_outbox_failure_rolls_back_done_and_manual_sync_preserves_upstream_success(inv):
    inv.s.outbox.failure = True
    result = inv.managed_manual_authorizer(lambda *a,**kw:success())(inv.email,run_id=inv.run_id)
    assert result['ok'] and result['cdk_sync_status'] == 'failed'
    assert inv.repo.get_cdk_code(inv.code)['status'] == 'failed'
    assert inv.db.scalar('SELECT status FROM invitation_v2_runs') == 'done'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs') == 1
    assert outbox(inv) == []


def test_unconfigured_channel_keeps_done_commit_without_enqueue(inv):
    inv.s.outbox.chat = ''
    inv.repo.set_cdk_status(inv.code,'done')
    assert inv.repo.get_cdk_code(inv.code)['status'] == 'done'
    assert outbox(inv) == []


def test_manual_success_and_reconciliation_enqueue_only_once(inv):
    result = inv.managed_manual_authorizer(lambda *a,**kw:success())(inv.email,run_id=inv.run_id)
    assert result['ok'] and result['cdk_sync_status'] == 'done'
    assert len(outbox(inv)) == 1
    inv.finalize_manual_authorization(inv.email,inv.run_id,success())
    inv.reconcile_manual_completion(inv.email,inv.run_id,apply=True)
    assert len(outbox(inv)) == 1


@pytest.mark.parametrize('cli_success',[True,False])
def test_automatic_authorization_emits_only_after_cli_success_and_done_write(inv,app,monkeypatch,cli_success):
    app.cli.return_value['ok'] = cli_success
    original_cli = app.cli.side_effect
    def cli(*args,**kwargs):
        assert outbox(inv) == []
        return original_cli(*args,**kwargs) if callable(original_cli) else app.cli.return_value
    app.cli.side_effect = cli
    # Exercise the real vendor automatic completion branch while retaining the
    # local repository transaction wrapper and an actual UNIQUE(event_key) table.
    monkeypatch.setattr(app.auth.db,'set_cdk_status',
                        lambda code,status,**fields:inv.repo.set_cdk_status(inv.code,status,**fields))
    result = invoke(app,manual=False)
    assert result['ok'] is cli_success
    assert inv.repo.get_cdk_code(inv.code)['status'] == ('done' if cli_success else 'failed')
    assert len(outbox(inv)) == int(cli_success)


def test_automatic_outbox_failure_does_not_retry_successful_authorization(inv,app,monkeypatch):
    app.cli.return_value['ok'] = True
    inv.s.outbox.failure = True
    monkeypatch.setattr(app.auth.db,'set_cdk_status',
                        lambda code,status,**fields:inv.repo.set_cdk_status(inv.code,status,**fields))
    result = invoke(app,manual=False)
    assert result['ok'] is False and app.cli.call_count == 1
    assert outbox(inv) == []
    assert inv.repo.get_cdk_code(inv.code)['status'] == 'failed'

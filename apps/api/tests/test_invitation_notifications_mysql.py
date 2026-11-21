"""Real MySQL completion/outbox atomicity; no notification worker or Telegram send."""
import json
from pathlib import Path
from uuid import uuid4

import pytest

from smsinvit.invitation import mysql
from test_invitation_mysql import card,inv,manual_completion_card,platform_card


@pytest.fixture
def notifications(inv,monkeypatch):
    events = set()
    monkeypatch.setattr(inv.s.outbox,'value',lambda name:'-100123' if name == 'telegram_ops_chat_id' else '')
    monkeypatch.setattr(inv.s.outbox,'telegram',lambda *a:pytest.fail('real Telegram send forbidden'))
    def event(code,prefix='invitation.card-redeemed:'):
        key = prefix+mysql.marker(code)
        events.add(key)
        return key
    try:yield event
    finally:
        for key in events:inv.db.execute('DELETE FROM notification_outbox WHERE event_key=?',key)


def count(inv,event):return inv.db.scalar('SELECT COUNT(*) FROM notification_outbox WHERE event_key=?',event)


def test_done_and_outbox_commit_together_and_repeat_is_idempotent(inv,notifications,monkeypatch):
    code = card(inv,mail=False)
    event = notifications(code)
    for state in ('bound','queued','running','failed'):
        inv.repo.set_cdk_status(code,state,email='completion@example.test')
        assert count(inv,event) == 0
    with pytest.raises(ConnectionError):
        with inv.db.transaction():
            inv.repo.set_cdk_status(code,'done')
            assert count(inv,event) == 1
            raise ConnectionError('failure after done before outer commit')
    assert inv.repo.get_cdk_code(code)['status'] == 'failed' and count(inv,event) == 0
    enqueue = inv.s.outbox.enqueue
    def failed_outbox(*args,**kwargs):
        enqueue(*args,**kwargs)
        raise ConnectionError('outbox failure after insert')
    with monkeypatch.context() as patch:
        patch.setattr(inv.s.outbox,'enqueue',failed_outbox)
        with pytest.raises(ConnectionError):inv.repo.set_cdk_status(code,'done')
    assert inv.repo.get_cdk_code(code)['status'] == 'failed' and count(inv,event) == 0
    with inv.db.transaction():
        inv.repo.set_cdk_status(code,'done',finished_at=123,error=None)
        assert count(inv,event) == 1
        # The worker's independent connection sees neither uncommitted record.
        with inv.db.transaction(requires_new=True):
            assert count(inv,event) == 0
            assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_codes WHERE code=?',mysql.marker(code)) == 'failed'
    assert inv.repo.get_cdk_code(code)['status'] == 'done' and count(inv,event) == 1
    inv.repo.set_cdk_status(code,'done')
    inv.repo.update_cdk_fields(code,status='done')
    assert count(inv,event) == 1
    row = inv.db.one('SELECT * FROM notification_outbox WHERE event_key=?',event)
    assert row['status'] == 'PENDING' and row['reference_id'] is None
    payload = json.loads(inv.s.codec.decrypt(row['payload_encrypted']))
    assert payload['chat'] == '-100123' and 'completion@example.test' in payload['body']
    assert mysql.marker(code) in payload['body'] and code not in payload['body']


def test_manual_completion_rollback_and_repeated_sync_preserve_one_notification(inv,manual_completion_card,notifications,monkeypatch):
    x = manual_completion_card
    event = notifications(x['code'])
    inv.begin_manual_authorization(x['email'],x['run'])
    original = inv.repo.add_cdk_event
    def failed_event(*args,**kwargs):
        assert count(inv,event) == 1  # outbox is written before the final sync step
        raise ConnectionError('event failure after card/outbox write')
    with monkeypatch.context() as patch:
        patch.setattr(inv.repo,'add_cdk_event',failed_event)
        result = inv.finalize_manual_authorization(x['email'],x['run'],{'ok':True,'result':{}})
    assert result['ok'] and result['cdk_sync_status'] == 'failed'
    assert inv.repo.get_cdk_code(x['code'])['status'] == 'failed' and count(inv,event) == 0
    assert inv.db.scalar('SELECT status FROM invitation_v2_runs WHERE run_id=?',mysql.marker(x['run'])) == 'done'
    assert inv.repo.add_cdk_event is original
    result = inv.finalize_manual_authorization(x['email'],x['run'],{'ok':True,'result':{}})
    assert result['ok'] and result['cdk_sync_status'] == 'done' and count(inv,event) == 1
    inv.finalize_manual_authorization(x['email'],x['run'],{'ok':True,'result':{}})
    inv.reconcile_manual_completion(x['email'],x['run'],apply=True)
    assert count(inv,event) == 1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x['code'])) == 2


def test_legacy_cleanup_removes_only_premature_pending_invitation_events(inv,notifications):
    bound = card(inv,mail=False)
    done = card(inv,mail=False)
    inv.repo.set_cdk_status(bound,'bound')
    inv.repo.set_cdk_status(done,'done')
    done_event = notifications(done)
    missing = notifications('NONEXISTENT-'+uuid4().hex)
    old = notifications(bound)
    states = ('SENDING','SENT','FAILED','UNCERTAIN','SKIPPED')
    retained = []
    sql_path = Path(__file__).resolve().parents[3]/'docs/validation/2026-10-13-invitation-premature-telegram-cleanup.sql'
    cleanup = 'DELETE n'+sql_path.read_text().split('DELETE n',1)[1].split(';',1)[0]
    # Test the actual deploy DELETE, then roll it back so pre-existing fixture
    # events outside this test are preserved even if they meet the predicate.
    with pytest.raises(RuntimeError,match='fixture rollback'):
        with inv.db.transaction():
            for event,status in [(old,'PENDING'),(missing,'PENDING')]:
                inv.s.outbox.enqueue(event,'TELEGRAM','CARD_REDEEMED',None,{'chat':'-100123','body':'legacy'},None)
            for status in states:
                event = old+':'+status
                retained.append(event)
                inv.s.outbox.enqueue(event,'TELEGRAM','CARD_REDEEMED',None,{'chat':'-100123','body':'legacy'},None)
                inv.db.execute('UPDATE notification_outbox SET status=? WHERE event_key=?',status,event)
            other_product = notifications(bound,'sms.card-redeemed:')
            inv.s.outbox.enqueue(other_product,'TELEGRAM','CARD_REDEEMED',None,{'chat':'-100123','body':'sms'},None)
            other_kind = old+':OTHER_KIND'
            inv.s.outbox.enqueue(other_kind,'TELEGRAM','TELEGRAM',None,{'chat':'-100123','body':'other'},None)
            inv.db.execute(cleanup)
            assert count(inv,old) == count(inv,missing) == 0
            assert count(inv,done_event) == count(inv,other_product) == count(inv,other_kind) == 1
            assert all(count(inv,event) == 1 for event in retained)
            raise RuntimeError('fixture rollback')

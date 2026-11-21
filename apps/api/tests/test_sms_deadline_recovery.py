"""A rate-limit cooldown must not discard a code on the original paid activation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

import pytest

from smsinvit.errors import Error
from smsinvit.sms.client import StatusResult
from smsinvit.sms.service import parse_codes
from smsinvit.sms.support import now
from test_sms_gpt_alignment import sms, session, waiting_code


def next_window(s, p, expires_in=-1):
    sid=waiting_code(s,p,expires_in=600)
    session(s,sid,status='SUCCESS',smsCode='111111',smsCodes='["111111"]',
            nextListenUntil=now()+timedelta(seconds=expires_in))
    s.m.key['status']='USED'
    def append(sid,code,sms_text,codes):
        session(s,sid,smsCode=code,smsText=sms_text,smsCodes=codes,nextListenUntil=None)
    s.m.appendSmsCode=append
    s.m.markProviderFinalized=lambda sid:session(s,sid,providerFinalizedAt=now(),nextListenUntil=None)
    original=p.set_status
    def set_status(aid,status):
        if status==6:
            p.set_status_calls.append((aid,status));return 'ACCESS_ACTIVATION'
        return original(aid,status)
    p.set_status=set_status
    return sid


@pytest.mark.parametrize('attempt',[1,3])
def test_code_delivered_during_cooldown_is_saved_before_expired_number_is_closed(attempt):
    s,p=sms();sid=waiting_code(s,p,attempt=attempt)
    p.status=StatusResult('OK','445566','STATUS_OK:445566')
    view=s.poll(sid)
    assert view['status']=='SUCCESS' and view['smsCode']=='445566'
    assert s.m.key['status']=='USED' and s.m.key['attemptCount']==attempt
    assert p.get_status_calls==1 and p.calls==1
    assert not any(status==8 for _,status in p.set_status_calls)


def test_final_query_failure_keeps_original_number_and_recovers_on_later_read(monkeypatch):
    s,p=sms();sid=waiting_code(s,p)
    clock=[100.0];monkeypatch.setattr('smsinvit.sms.service.time.monotonic',lambda:clock[0])
    original=p.get_status
    def fail(aid):
        p.get_status_calls+=1;raise Error('SMS_PROVIDER_ERROR','query unavailable',502)
    p.get_status=fail
    assert s.poll(sid)['status']=='WAITING_CODE'
    assert not p.set_status_calls and s.m.key['attemptCount']==1
    p.get_status=original;p.status=StatusResult('OK','445566','STATUS_OK:445566');clock[0]+=5
    assert s.poll(sid)['smsCode']=='445566'
    assert p.calls==1 and s.m.key['status']=='USED'


def test_unknown_final_query_does_not_cancel_or_buy_again():
    s,p=sms();sid=waiting_code(s,p)
    p.status=StatusResult('ERROR',None,'unexpected response')
    assert s.poll(sid)['status']=='WAITING_CODE'
    assert not p.set_status_calls and p.calls==1 and s.m.key['attemptCount']==1


def test_code_delivered_while_cancellation_is_unconfirmed_is_not_discarded():
    s,p=sms();sid=waiting_code(s,p)
    responses=iter([StatusResult('WAIT'),StatusResult('OK','445566','STATUS_OK:445566')])
    p.cancel='CODE_ALREADY_RECEIVED'
    def status(aid):
        p.get_status_calls+=1;return next(responses)
    p.get_status=status
    view=s.poll(sid)
    assert view['status']=='SUCCESS' and view['smsCode']=='445566'
    assert p.get_status_calls==2 and s.m.key['status']=='USED' and p.calls==1


def test_webhook_wins_between_final_read_and_cancel_intent():
    s,p=sms();sid=waiting_code(s,p)
    def status(aid):
        p.get_status_calls+=1
        s.on_webhook(aid,'445566','SMS text')
        return StatusResult('WAIT')
    p.get_status=status
    view=s.poll(sid)
    assert view['smsCode']=='445566' and s.m.key['status']=='USED'
    assert not p.set_status_calls and p.calls==1


def test_final_next_sms_query_saves_new_code_after_original_listen_deadline():
    s,p=sms();sid=next_window(s,p)
    p.status=StatusResult('OK','222222','STATUS_OK:222222')
    view=s.poll(sid)
    assert view['smsCodes']==['111111','222222'] and view['providerFinalized'] is True
    assert p.get_status_calls==1 and p.set_status_calls==[('activation-1',6)]
    assert s.m.key['status']=='USED' and s.m.key['attemptCount']==1 and p.calls==1


def test_final_next_sms_query_keeps_deduplication_and_normal_webhook_deadline():
    s,p=sms();sid=next_window(s,p)
    assert s.on_webhook('activation-1','222222','late callback')['smsCodes']==['111111']
    p.status=StatusResult('WAIT_RETRY','111111','STATUS_WAIT_RETRY:111111')
    view=s.poll(sid)
    assert view['smsCodes']==['111111'] and view['providerFinalized'] is True
    assert p.get_status_calls==1 and p.set_status_calls==[('activation-1',6)]


def test_failed_next_sms_query_defers_closing_until_original_code_can_be_read(monkeypatch):
    s,p=sms();sid=next_window(s,p)
    clock=[100.0];monkeypatch.setattr('smsinvit.sms.service.time.monotonic',lambda:clock[0])
    p.status=StatusResult('ERROR',None,'temporary failure')
    view=s.poll(sid)
    assert view['smsCodes']==['111111'] and not view['providerFinalized'] and not p.set_status_calls
    assert view['listeningNextSms'] and view['resultPending'] and not view['canRequestNextSms']
    with pytest.raises(Error) as error:s.next_sms(sid)
    assert error.value.code=='SMS_RESULT_PENDING'
    assert not any(status==3 for _,status in p.set_status_calls)
    p.status=StatusResult('OK','222222','STATUS_OK:222222');clock[0]+=5
    assert s.poll(sid)['smsCodes']==['111111','222222']
    assert p.set_status_calls==[('activation-1',6)]


def test_active_query_finishing_after_deadline_keeps_same_listening_window(monkeypatch):
    s,p=sms();sid=next_window(s,p,expires_in=1);base=now()
    def status(aid):
        p.get_status_calls+=1
        monkeypatch.setattr('smsinvit.sms.service.now',lambda:base+timedelta(seconds=2))
        return StatusResult('OK','222222','STATUS_OK:222222')
    p.get_status=status
    assert s.poll(sid)['smsCodes']==['111111','222222']
    assert p.get_status_calls==1 and p.calls==1


def test_competing_poll_and_mutation_do_not_close_an_inflight_code_read():
    s,p=sms();sid=waiting_code(s,p);entered=threading.Event();release=threading.Event()
    def status(aid):
        p.get_status_calls+=1;entered.set();assert release.wait(5)
        return StatusResult('OK','445566','STATUS_OK:445566')
    p.get_status=status
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(s.poll,sid)
        try:
            assert entered.wait(2)
            other=pool.submit(s.poll,sid).result(timeout=1)
            assert other['status']=='WAITING_CODE' and p.get_status_calls==1 and not p.set_status_calls
            with pytest.raises(Error,match='正在查询') as error:s.retry(sid)
            assert error.value.code=='SMS_RESULT_PENDING'
        finally:release.set()
        assert pending.result(timeout=2)['smsCode']=='445566'
    assert p.calls==1 and not any(status==8 for _,status in p.set_status_calls)
    assert not s.operation_locks


def test_parallel_final_next_sms_read_appends_and_finalizes_once():
    s,p=sms();sid=next_window(s,p);entered=threading.Event();release=threading.Event()
    def status(aid):
        p.get_status_calls+=1;entered.set();assert release.wait(5)
        return StatusResult('OK','222222','STATUS_OK:222222')
    p.get_status=status
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(s.poll,sid)
        try:
            assert entered.wait(2)
            assert pool.submit(s.poll,sid).result(timeout=1)['smsCodes']==['111111']
            with pytest.raises(Error):s.next_sms(sid)
        finally:release.set()
        assert pending.result(timeout=2)['smsCodes']==['111111','222222']
    assert p.get_status_calls==1 and p.set_status_calls==[('activation-1',6)]
    assert parse_codes(s.session(sid))==['111111','222222'] and not s.operation_locks


def test_sweep_queries_expired_original_number_and_next_window_before_closing():
    s,p=sms();sid=waiting_code(s,p)
    s.m.markStaleAcquisitionsUnknown=lambda:None
    s.m.listTimedOutWaiting=lambda:[{'id':sid,'redeemKeyId':'k'}]
    s.m.listTimedOutSuccess=lambda:[]
    s.m.listCancelPending=lambda:[]
    p.status=StatusResult('OK','445566','STATUS_OK:445566')
    s.sweep()
    assert s.session(sid)['smsCode']=='445566' and s.m.key['status']=='USED'
    assert p.get_status_calls==1 and not any(status==8 for _,status in p.set_status_calls)
    s,p=sms();sid=next_window(s,p)
    s.m.markStaleAcquisitionsUnknown=lambda:None
    s.m.listTimedOutWaiting=lambda:[]
    s.m.listTimedOutSuccess=lambda:[{'id':sid,'activationId':'activation-1'}]
    s.m.listCancelPending=lambda:[]
    p.status=StatusResult('OK','222222','STATUS_OK:222222')
    s.sweep()
    assert parse_codes(s.session(sid))==['111111','222222']
    assert p.get_status_calls==1 and p.set_status_calls==[('activation-1',6)]

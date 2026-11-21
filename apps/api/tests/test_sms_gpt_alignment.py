"""Java alignment regressions: SMS poll expiry/cancel-pending and DbBool parsing.

These cases mirror the desktop Java baseline (SmsRedeemService.poll, DbBool.isTrue)
and are written to fail before the alignment fixes.
"""
from __future__ import annotations
import copy,threading
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from smsinvit.errors import Error
from smsinvit.sms.line_type import FreeLineChecker
from smsinvit.sms.service import SmsService
from smsinvit.sms.client import NumberOrder,StatusResult
from smsinvit.sms.support import now,db_true,flag_true

# --------------------------------------------------------------------------------------
# shared fakes
# --------------------------------------------------------------------------------------

class TransactionDB:
    def __init__(self):self.lock=threading.RLock();self.store=None;self.calls=[]
    @contextmanager
    def transaction(self,requires_new=False):
        with self.lock:
            self.calls.append(requires_new);snapshot=copy.deepcopy(self.store.state) if self.store else None
            try:yield self
            except Exception:
                if self.store:self.store.state=snapshot
                raise

class Codec:
    def digest(self,v):return 'fp:'+v
    def decrypt(self,v):return v.removeprefix('encrypted:')
    def encrypt(self,v):return 'encrypted:'+v

class Config:
    def get(self,k,d=None):return d

class Tokens:
    def issue(self,scope,sid):return scope+':'+sid

class Settings:
    """Fake business settings; runtime_* keeps the Java-compatible int fallbacks."""
    def __init__(self,values=None,defaults=None):
        self.values=dict(values or {});self.defaults=dict(defaults or {})
    def get(self,k,d=None):return self.values.get(k,d if k not in self.defaults else self.defaults[k])
    def load(self):return {**self.defaults,**self.values}
    def secret(self,k='apiKey'):return self.values.get(k,'')
    def runtime_int(self,k,d):return int(self.values.get(k,d))
    def runtime_float(self,k,d):return float(self.values.get(k,d))

def sms_settings(**overrides):
    values={'minBalance':0,'unavailableMessage':'maintenance','sms_status_poll_min_ms':1,'sms_number_wait_seconds':600,'sms_cancel_grace_seconds':300,'sms_max_attempts':3}
    values.update(overrides);return Settings(values)

class SmsMapper:
    def __init__(self):
        self.state={'key':{'id':'k','inventoryItemId':'i','codeFingerprint':'fp:valid-code','status':'UNUSED','secretCiphertext':'encrypted:valid-code','inventoryStatus':'ISSUED','attemptCount':0,'smsService':'ot','smsCountry':0,'smsMinPrice':Decimal('0.01'),'smsMaxPrice':Decimal('0.3'),'smsMinCount':20,'expiresAt':now()+timedelta(days=1)},'sessions':{},'events':[]}
    @property
    def key(self):return self.state['key']
    def findKeyByFingerprintForUpdate(self,fp):return [self.key] if fp==self.key['codeFingerprint'] else []
    def findKeyForUpdate(self,k):return self.key
    findKeyById=findKeyForUpdate
    def isCdkAmbiguous(self,*args):return False
    def lockInventoryStatus(self,*args):return 'ISSUED'
    def listExpiredKeyIds(self):return []
    def findActiveSessionId(self,k):return next((s['id'] for s in self.state['sessions'].values() if s['status'] in ('PENDING_NUMBER','WAITING_CODE','ACQUIRE_UNKNOWN','CANCEL_PENDING')),None)
    def findLatestSuccessSessionId(self,k):return next((s['id'] for s in self.state['sessions'].values() if s['status']=='SUCCESS'),None)
    def findLatestSessionWithPhoneId(self,k):return next((s['id'] for s in reversed(list(self.state['sessions'].values())) if s.get('phone')),None)
    def findReusableSessionId(self,k,a):return next((s['id'] for s in self.state['sessions'].values() if s['attempt']==a and s['status'] in ('UNAVAILABLE','CANCELLED','FAILED')),None)
    def resetSessionPending(self,sid):self.state['sessions'][sid].update(status='PENDING_NUMBER',acquisitionStartedAt=None,activationId=None,phone=None,cancelRefundAttempt=False)
    def insertSession(self,sid,k,a):self.state['sessions'][sid]={'id':sid,'redeemKeyId':k,'attempt':a,'status':'PENDING_NUMBER','activationId':None,'acquisitionStartedAt':None,'cancelRefundAttempt':False}
    def lockKey(self,k,a):self.key.update(status='LOCKED',attemptCount=a)
    def listAutoExcludedAreas(self):return sorted(self.state.get('autoAreas',{}))
    def addAutoExcludedArea(self,prefix,line_type):
        pool=self.state.setdefault('autoAreas',{});pool[prefix]=pool.get(prefix,0)+1;return 1
    def listDisplayPhones(self,k):return [s['phone'] for s in self.state['sessions'].values() if s.get('phone') and not s.get('cancelRefundAttempt')]
    def listPhones(self,k):return [s['phone'] for s in self.state['sessions'].values() if s.get('phone')]
    def startAcquisition(self,sid):
        s=self.state['sessions'][sid]
        if s['status']=='PENDING_NUMBER' and not s.get('acquisitionStartedAt'):s['acquisitionStartedAt']=now();return 1
        return 0
    def findSessionById(self,sid):return self.state['sessions'].get(sid)
    findSessionForUpdate=findSessionById
    def markSessionWaitingCode(self,sid,aid,phone,cost,cancel,expires):
        s=self.state['sessions'][sid]
        if not ((s['status'] in ('PENDING_NUMBER','ACQUIRE_UNKNOWN') and not s.get('activationId')) or (s['status']=='CANCEL_PENDING' and s.get('cancelRefundAttempt') and s.get('activationId')==aid)):return 0
        s.update(status='WAITING_CODE',cancelRefundAttempt=False,activationId=aid,phone=phone,activationCost=cost,cancelEligibleAt=cancel,numberExpiresAt=expires);return 1
    def markSessionAcquireUnknown(self,sid):
        s=self.state['sessions'][sid]
        if s['status']=='PENDING_NUMBER':s['status']='ACQUIRE_UNKNOWN'
    def markSessionUnavailable(self,sid):self.state['sessions'][sid]['status']='UNAVAILABLE'
    def rollbackAttempt(self,k):self.key['attemptCount']=max(0,self.key['attemptCount']-1);self.key['status']='UNUSED' if self.key['attemptCount']==0 else 'LOCKED'
    def markSessionCancelPending(self,sid,aid,phone,cost,refund):
        s=self.state['sessions'][sid]
        if s['status'] not in ('PENDING_NUMBER','ACQUIRE_UNKNOWN','WAITING_CODE') or s.get('activationId') not in (None,aid):return 0
        s.update(status='CANCEL_PENDING',activationId=aid,cancelRefundAttempt=refund)
        if phone is not None:s['phone']=phone
        if cost is not None:s['activationCost']=cost
        return 1
    def markSessionCancelled(self,sid):self.state['sessions'][sid]['status']='CANCELLED'
    def markKeyFailed(self,k):self.key['status']='FAILED'
    def markKeyExpired(self,k):self.key['status']='EXPIRED'
    def insertEvent(self,*args):self.state['events'].append(args)
    def renameKeyId(self,k):
        """Keeps the single-key fixture consistent when sessions are created under a new CDK id."""
        self.state['key']['id']=k
    def sessionView(self,sid):
        return self.state['sessions'][sid]|{'keyStatus':self.key['status'],'attemptCount':self.key['attemptCount'],'keyExpiresAt':self.key['expiresAt'],'smsService':self.key['smsService'],'smsCountry':self.key['smsCountry']}
    def findSessionIdByActivationForUpdate(self,aid):return next((s['id'] for s in self.state['sessions'].values() if s.get('activationId')==aid and (s['status'] in ('WAITING_CODE','SUCCESS') or s['status']=='CANCEL_PENDING' and not s['cancelRefundAttempt'])),None)
    def markSessionSuccess(self,sid,code,sms_text,codes):
        s=self.state['sessions'][sid]
        if s['status']!='WAITING_CODE' and not (s['status']=='CANCEL_PENDING' and not s['cancelRefundAttempt']):return 0
        s.update(status='SUCCESS',smsCode=code,smsText=sms_text,smsCodes=codes);return 1
    def markKeyUsed(self,k):
        if self.key['status']!='LOCKED':return 0
        self.key['status']='USED';return 1

class SmsProvider:
    def __init__(self):
        self.calls=0;self.lock=threading.Lock();self.error=None;self.phone='12025550123';self.cancel='ACCESS_CANCEL'
        self.status=StatusResult('WAIT');self.set_status_calls=[];self.get_status_calls=0
    def configured(self):return True
    def get_balance(self):return 'ACCESS_BALANCE:100'
    def get_prices(self,*args):return {'0':{'ot':{'1':{'price':'0.1','count':20}}}}
    def get_number(self,*args):
        with self.lock:self.calls+=1
        if self.error:raise self.error
        return NumberOrder('activation-'+str(self.calls),self.phone,Decimal('0.1'))
    def set_status(self,aid,status):
        self.set_status_calls.append((aid,status))
        if status==8:return self.cancel
        return 'ACCESS_RETRY_GET'
    def get_status(self,aid):
        self.get_status_calls+=1
        return self.status

def sms(settings=None,provider=None):
    db=TransactionDB();provider=provider or SmsProvider();s=SmsService(db,Codec(),Config(),provider,Tokens(),line_checker=FreeLineChecker(SimpleNamespace(secret=lambda key:None)))
    s.m=SmsMapper();db.store=s.m;s.settings=settings or sms_settings()
    s.catalog=SimpleNamespace(name=lambda kind,value:str(value));return s,provider

def session(s,sid,**fields):
    """Mutates the stored session in place; the mapper returns copies, not references."""
    s.m.state['sessions'][sid].update(**fields)
    return s.m.state['sessions'][sid]

def waiting_code(s,provider,expires_in=-60,attempt=1):
    """Brings the CDK to a WAITING_CODE session exactly like finish_acquire would."""
    result=s.redeem('valid-code');sid=result['id']
    session(s,sid,status='WAITING_CODE',activationId='activation-1',phone=provider.phone,
            activationCost=Decimal('0.1'),cancelEligibleAt=now()-timedelta(seconds=1),
            numberExpiresAt=now()+timedelta(seconds=expires_in))
    s.m.key.update(status='LOCKED',attemptCount=attempt)
    return sid

# --------------------------------------------------------------------------------------
# SMS-01: polling must cancel an expired number without buying a new one
# --------------------------------------------------------------------------------------

def test_sms_poll_cancels_expired_number_without_rebuying():
    s,p=sms();sid=waiting_code(s,p)
    view=s.poll(sid)
    assert p.set_status_calls==[('activation-1',8)],'poll must request upstream cancel (setStatus 8) on expiry'
    assert p.calls==1,'expired polling must never buy another number'
    assert p.get_status_calls==1,'query the original activation once before closing an expired number'
    assert s.m.state['sessions'][sid]['status']=='CANCELLED'
    assert s.m.key['attemptCount']==1,'ordinary cancel keeps the consumed attempt'
    assert view['status']=='CANCELLED'
    assert view['resultPending'] is False
    for kind in ('CANCEL_PENDING','CANCEL_CONFIRMED'):
        assert any(e[3]==kind for e in s.m.state['events']),kind

def test_sms_poll_expiry_unconfirmed_cancel_holds_state_without_rebuying():
    s,p=sms();p.cancel='EARLY_CANCEL';sid=waiting_code(s,p)
    view=s.poll(sid)
    assert s.m.state['sessions'][sid]['status']=='CANCEL_PENDING','unknown cancel result must stay pending'
    assert view['resultPending'] is True
    assert view['canRetry'] is False
    assert p.calls==1,'no rebuy while the cancel result is unknown'
    assert s.m.key['attemptCount']==1

def test_sms_poll_expiry_marks_key_failed_at_attempt_limit_without_failure_notice():
    # 通知只保留「卡密核销完成」：换号失败不再推送，只落 KEY_FAILED 事件。
    outbox=Mock();outbox.value.return_value='chat-1'
    s,p=sms();s.outbox=outbox;sid=waiting_code(s,p,attempt=3)
    s.poll(sid)
    assert s.m.key['status']=='FAILED'
    assert any(e[3]=='KEY_FAILED' for e in s.m.state['events'])
    assert outbox.enqueue.call_count==0

# --------------------------------------------------------------------------------------
# SMS-02: polling must re-query upstream while a cancellation is unconfirmed
# --------------------------------------------------------------------------------------

def test_sms_poll_probes_cancel_pending_and_completes_cancellation():
    s,p=sms();sid=s.redeem('valid-code')['id']
    session(s,sid,status='CANCEL_PENDING',activationId='activation-1',cancelRefundAttempt=False)
    p.status=StatusResult('CANCEL')
    with s.probe_lock:s.probes['activation-1']=(4,0)
    view=s.poll(sid)
    assert p.get_status_calls==1,'CANCEL_PENDING must probe upstream on the Java cadence'
    assert s.m.state['sessions'][sid]['status']=='CANCELLED'
    assert any(e[3]=='CANCEL_CONFIRMED' for e in s.m.state['events'])
    assert view['status']=='CANCELLED'

def test_sms_poll_cancel_pending_compensation_refunds_once():
    s,p=sms();sid=s.redeem('valid-code')['id']
    session(s,sid,status='CANCEL_PENDING',activationId='activation-1',cancelRefundAttempt=True)
    s.m.key.update(status='LOCKED',attemptCount=1)
    p.status=StatusResult('CANCEL')
    with s.probe_lock:s.probes['activation-1']=(4,0)
    s.poll(sid)
    assert s.m.key['attemptCount']==0,'compensation cancel refunds once'
    assert s.m.state['sessions'][sid]['status']=='UNAVAILABLE'
    with s.probe_lock:s.probes['activation-1']=(4,0)
    s.poll(sid)
    assert s.m.key['attemptCount']==0,'second confirmation must not refund again'

def test_sms_poll_cancel_pending_delivered_code_wins_before_confirmation():
    s,p=sms();sid=s.redeem('valid-code')['id']
    session(s,sid,status='CANCEL_PENDING',activationId='activation-1',cancelRefundAttempt=False)
    s.m.key.update(status='LOCKED',attemptCount=1)
    p.status=StatusResult('OK','778899',"STATUS_OK:778899")
    with s.probe_lock:s.probes['activation-1']=(4,0)
    view=s.poll(sid)
    assert view['status']=='SUCCESS','a delivered code wins while cancellation is unconfirmed'
    assert s.m.key['status']=='USED'
    assert any(e[3]=='CODE_RECEIVED' for e in s.m.state['events'])

def test_sms_poll_cancel_pending_late_code_ignored_after_confirmation():
    s,p=sms();sid=s.redeem('valid-code')['id']
    session(s,sid,status='CANCEL_PENDING',activationId='activation-1',cancelRefundAttempt=False)
    s.complete_cancellation(sid,'activation-1')
    p.status=StatusResult('OK','778899',"STATUS_OK:778899")
    with s.probe_lock:s.probes['activation-1']=(4,0)
    view=s.poll(sid)
    assert view['status']=='CANCELLED'
    assert s.m.key['status']=='LOCKED','late code after confirmed cancel must not consume the CDK'

def test_sms_poll_cancel_pending_compensation_late_code_ignored():
    s,p=sms();sid=s.redeem('valid-code')['id']
    session(s,sid,status='CANCEL_PENDING',activationId='activation-1',cancelRefundAttempt=True)
    s.m.key.update(status='LOCKED',attemptCount=1)
    p.status=StatusResult('OK','778899',"STATUS_OK:778899")
    with s.probe_lock:s.probes['activation-1']=(4,0)
    s.poll(sid)
    # Java parity: a compensation session can never win on a code; the poll only records the ignore.
    assert s.m.state['sessions'][sid]['status']=='CANCEL_PENDING'
    assert s.m.key['attemptCount']==1,'no refund without upstream confirmation'
    assert s.m.key['status']=='LOCKED'
    assert 'SUCCESS' not in [e[3] for e in s.m.state['events']]
    assert 'LATE_CODE_IGNORED' in [e[3] for e in s.m.state['events']]

def test_sms_poll_probe_uses_elapsed_time_and_queries_after_a_quiet_period(monkeypatch):
    s,p=sms();sid=s.redeem('valid-code')['id']
    session(s,sid,status='WAITING_CODE',activationId='activation-1',numberExpiresAt=now()+timedelta(minutes=5))
    clock=[100.0]
    monkeypatch.setattr('smsinvit.sms.service.time.monotonic',lambda:clock[0])
    for _ in range(4):s.poll(sid)
    assert p.get_status_calls==1,'first poll must query immediately; repeated reads cannot amplify upstream'
    clock[0]+=4.9
    s.poll(sid)
    assert p.get_status_calls==1
    clock[0]+=0.1
    s.poll(sid)
    assert p.get_status_calls==2,'five elapsed seconds permit the next supplier query'
    clock[0]+=60
    s.poll(sid)
    assert p.get_status_calls==3,'a cooldown must not require another five HTTP polls'

def test_sms_poll_expired_after_cancel_marks_key_expired():
    s,p=sms();sid=waiting_code(s,p)
    s.m.key['expiresAt']=now()-timedelta(seconds=1)
    # Java parity: poll() cancels with auto=true, so the terminal key state is applied without raising.
    view=s.poll(sid)
    assert s.m.key['status']=='EXPIRED'
    assert s.m.state['sessions'][sid]['status']=='CANCELLED'
    assert 'KEY_EXPIRED' in [e[3] for e in s.m.state['events']]
    assert view['status']=='CANCELLED'
    # Java parity: a terminal card cannot be re-acquired (acquireNewAttemptForKey -> SMS_SESSION_STATE).
    with pytest.raises(Error) as error:s.retry(sid)
    assert error.value.code=='SMS_SESSION_STATE'

# --------------------------------------------------------------------------------------
# BOOL-01: DbBool.isTrue parity for 0/1 columns coming back as strings
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize('value,expected',[(True,True),(False,False),(1,True),(0,False),('1',False),('0',False),('true',False),(Decimal('1'),True),(None,False)])
def test_db_true_matches_java_dbbool(value,expected):
    assert db_true(value) is expected

@pytest.mark.parametrize('value,expected',[(True,True),(False,False),('true',True),('TRUE',True),(' false ',False),('yes',False),(1,True),(0,False),(None,False)])
def test_flag_true_matches_java_boolval(value,expected):
    assert flag_true(value,False) is expected

def test_sms_compensation_refund_ignores_string_zero_flag():
    s,p=sms();sid=s.redeem('valid-code')['id']
    s.m.state['sessions'][sid].update(status='CANCEL_PENDING',activationId='activation-1',cancelRefundAttempt='0')
    s.m.key.update(status='LOCKED',attemptCount=1)
    s.complete_cancellation(sid,'activation-1')
    assert s.m.state['sessions'][sid]['status']=='CANCELLED','string "0" is a false refund flag in Java'


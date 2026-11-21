"""State machine regression tests with fake supplier and transactional state store."""
from __future__ import annotations
import copy,threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from smsinvit.errors import Error
from smsinvit.sms.line_type import FreeLineChecker
from smsinvit.sms.service import SmsService
from smsinvit.sms.client import NumberOrder,StatusResult
from smsinvit.sms.support import now

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
    def one(self,*args):return None
class Codec:
    def digest(self,v):return 'fp:'+v
    def decrypt(self,v):return v.removeprefix('encrypted:')
    def encrypt(self,v):return 'encrypted:'+v
class Config:
    def get(self,k,d=None):return d
class Tokens:
    def issue(self,scope,sid):return scope+':'+sid
class Settings:
    def get(self,k,d=None):return {'minBalance':0,'unavailableMessage':'maintenance'}.get(k,d)
    def secret(self,*args):return 'key'
    def runtime_int(self,k,d):return d
    def load(self):return {}

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
    def insertEvent(self,*args):self.state['events'].append(args)
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
    def __init__(self):self.calls=0;self.lock=threading.Lock();self.error=None;self.phone='12025550123';self.cancel='ACCESS_CANCEL'
    def configured(self):return True
    def get_balance(self):return 'ACCESS_BALANCE:100'
    def get_prices(self,*args):return {'0':{'ot':{'1':{'price':'0.1','count':20}}}}
    def get_number(self,*args):
        with self.lock:self.calls+=1
        if self.error:raise self.error
        return NumberOrder('activation-'+str(self.calls),self.phone,Decimal('0.1'))
    def set_status(self,*args):return self.cancel
    def get_status(self,*args):return StatusResult('WAIT')

def sms():
    db=TransactionDB();provider=SmsProvider();s=SmsService(db,Codec(),Config(),provider,Tokens(),line_checker=FreeLineChecker(SimpleNamespace(secret=lambda key:None)));s.m=SmsMapper();db.store=s.m;s.settings=Settings();s.catalog=SimpleNamespace(name=lambda kind,value:str(value));return s,provider

def test_sms_100_concurrent_redeems_one_paid_number_and_one_attempt():
    s,provider=sms()
    with ThreadPoolExecutor(max_workers=32) as pool:results=list(pool.map(lambda _:s.redeem('valid-code'),range(100)))
    assert provider.calls==1;assert s.m.key['attemptCount']==1;assert len(s.m.state['sessions'])==1
    assert len({r['id'] for r in results})==1;assert all(r['resultPending'] or r['status']=='WAITING_CODE' for r in results)
    assert all(s.db.calls), 'Every acquisition phase must use REQUIRES_NEW'

def test_sms_timeout_never_rebuys_and_retry_held():
    s,p=sms();p.error=Error('SMS_PROVIDER_ERROR','timeout',502)
    with pytest.raises(Error):s.redeem('valid-code')
    sid=next(iter(s.m.state['sessions']));assert s.m.state['sessions'][sid]['status']=='ACQUIRE_UNKNOWN';assert s.m.key['attemptCount']==1
    for _ in range(20):assert s.redeem('valid-code')['resultPending']
    with pytest.raises(Error) as e:s.retry(sid)
    assert e.value.code=='SMS_RESULT_PENDING';assert p.calls==1

def test_sms_clear_rejection_refunds_and_reuses_attempt_slot():
    s,p=sms();p.error=Error('SMS_UNAVAILABLE','no numbers',503)
    with pytest.raises(Error):s.redeem('valid-code')
    sid=next(iter(s.m.state['sessions']));assert s.m.key['attemptCount']==0
    p.error=None;result=s.redeem('valid-code');assert result['id']==sid;assert s.m.key['attemptCount']==1;assert p.calls==2

def test_sms_acquire_compensation_holds_until_cancel_confirmed_and_refunds_once():
    s,p=sms();p.phone='';p.cancel='EARLY_CANCEL';result=s.redeem('valid-code');sid=result['id'];assert result['status']=='CANCEL_PENDING';assert s.m.key['attemptCount']==1
    assert s.redeem('valid-code')['resultPending'];assert p.calls==1
    s.complete_cancellation(sid,'activation-1');s.complete_cancellation(sid,'activation-1')
    assert s.m.key['attemptCount']==0;assert s.m.state['sessions'][sid]['status']=='UNAVAILABLE'

def test_sms_code_can_win_unconfirmed_manual_cancel_but_late_code_cannot_win_confirmed_cancel():
    s,p=sms();sid=s.redeem('valid-code')['id']
    s.m.markSessionCancelPending(sid,'activation-1',None,None,False)
    assert s.on_webhook('activation-1','123456','text')['status']=='SUCCESS';assert s.m.key['status']=='USED'
    s.complete_cancellation(sid,'activation-1');assert s.m.state['sessions'][sid]['status']=='SUCCESS'
    s,p=sms();sid=s.redeem('valid-code')['id'];s.m.markSessionCancelPending(sid,'activation-1',None,None,False);s.complete_cancellation(sid,'activation-1')
    assert s.on_webhook('activation-1','123456','text')=={'ok':True,'ignored':True};assert s.m.key['status']=='LOCKED'

# --------------------------------------------------------------------------------------
# 通知收敛：只有「卡密核销完成」会推送，且未配置 Telegram 时绝不影响业务
# --------------------------------------------------------------------------------------

def test_sms_card_redeemed_sends_exactly_one_notification():
    s,p=sms();sid=s.redeem('valid-code')['id'];s.m.markSessionCancelPending(sid,'activation-1',None,None,False)
    outbox=Mock();outbox.value.return_value='-100123';s.outbox=outbox
    assert s.on_webhook('activation-1','123456','text')['status']=='SUCCESS'
    assert s.m.key['status']=='USED'
    args=outbox.enqueue.call_args.args
    assert outbox.enqueue.call_count==1
    assert args[0]=='sms.card-redeemed:k' and args[1]=='TELEGRAM' and args[2]=='CARD_REDEEMED'
    assert args[4]['chat']=='-100123' and '【接码】卡密核销完成' in args[4]['body']

def test_sms_unconfigured_outbox_never_breaks_redemption():
    # 未保存 Telegram Chat ID：value() 返回空 → 不写 outbox，核销照常成功。
    s,p=sms();sid=s.redeem('valid-code')['id'];s.m.markSessionCancelPending(sid,'activation-1',None,None,False)
    outbox=Mock();outbox.value.return_value='';s.outbox=outbox
    assert s.on_webhook('activation-1','123456','text')['status']=='SUCCESS'
    assert outbox.enqueue.call_count==0 and s.m.key['status']=='USED'

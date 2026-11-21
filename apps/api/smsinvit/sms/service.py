from __future__ import annotations
from .line_type import FreeLineChecker
from .line_monitor import LineCheckMonitor
import re, threading, time
from contextlib import contextmanager
from pathlib import Path
from decimal import Decimal
from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as PoolTimeout
from smsinvit.errors import Error,ok
from smsinvit.common.cdk import inventory_code_map
from smsinvit.common.notifications import CARD_REDEEMED_KIND,card_redeemed_body
from .support import Mapper,BusinessSettings,text,parse_json,dumps,uid,now,instant,mask,db_true
from .client import SmsbowerClient,SmsCatalog,service_map,cost_usd_to_cny_minor

PRODUCT_KIND='SMS_REDEEM'
def digits(v):return re.sub(r'\D','',text(v))
def exception_prefix(v):
    d=digits(v)
    if len(d)<4:return None
    n=min(7,len(d)-3) if len(d)>=10 else min(7,len(d))
    return d[:max(4,min(10,n))]
def normalize_exception(v):return ','.join(dict.fromkeys(d for p in re.split(r'[,，\s]+',text(v)) if len(d:=digits(p))>=4))
def merge_exceptions(v,phones):return ','.join(dict.fromkeys([*filter(None,normalize_exception(v).split(',')),*filter(None,(exception_prefix(p) for p in phones))]))
def excluded(phone,exceptions):return any(digits(phone).startswith(p) for p in map(digits,text(exceptions).split(',')) if p)
def voip_area_prefix(phone):
    number=text(phone).lstrip('+')
    return number[:4] if re.fullmatch(r'1[2-9][0-9]{2}[2-9][0-9]{6}',number) else None
def normalize_area_prefix(value):
    match=re.fullmatch(r'(?:\+?1[\s-]?)?([2-9][0-9]{2})',text(value))
    if not match:raise Error('VALIDATION_ERROR','请输入三位区号或 +1 xxx，例如 +1 832；不要填写完整号码。',422)
    return '1'+match[1]
def mask_phone(phone):
    p=re.sub(r'\s','',text(phone))
    return '****' if len(p)<=4 else p[:2]+'****'+p[-2:] if len(p)<=7 else p[:3]+'****'+p[-4:]
def parse_codes(session):
    raw=session.get('smsCodes');single=text(session.get('smsCode'))
    if not raw:return [single] if single else []
    parsed=parse_json(raw)
    if isinstance(parsed,list):return [text(v) for v in parsed if v is not None and text(v)]
    return [text(raw)] if not text(raw).startswith('[') else [single] if single else []

class SmsService:
    def __init__(self,db,codec,config,client=None,tokens=None,jobs=None,outbox=None,policy=None,http=None,line_checker=None):
        self.db=db;self.codec=codec;self.config=config;self.tokens=tokens;self.jobs=jobs;self.outbox=outbox
        self.settings=BusinessSettings(db,codec,config,'sms.smsbower',Path(__file__).with_name('defaults.json'))
        self.m=Mapper(db,Path(__file__).with_name('queries.json'))
        self.client=client or SmsbowerClient(self.settings,transport=http,policy=policy)
        self.line_monitor=LineCheckMonitor(db,codec)
        self.line_checker=line_checker or FreeLineChecker(self.settings,monitor=self.line_monitor)
        self.catalog=SmsCatalog(self.client,self.settings);self.probes={};self.probe_lock=threading.Lock()
        self.operation_locks={};self.operation_context=threading.local();self.operation_lock_engine=None
    def max_attempts(self):return max(1,self.settings.runtime_int('sms_max_attempts',3))
    def grace(self):return max(120,self.settings.runtime_int('sms_cancel_grace_seconds',300))
    def wait(self):return max(60,self.settings.runtime_int('sms_number_wait_seconds',600))
    @contextmanager
    def session_operation(self,sid):
        """Serialize provider reads/close/retry for one session, including other API processes.

        The named lock uses its own connection. Provider I/O never holds a session/key row
        lock, and a concurrent poll returns the stored snapshot instead of queuing work.
        """
        sid=str(sid);held=getattr(self.operation_context,'held',set())
        if sid in held:
            yield True;return
        with self.probe_lock:
            entry=self.operation_locks.setdefault(sid,[threading.Lock(),0]);entry[1]+=1
        local_acquired=entry[0].acquire(blocking=False);connection=None;acquired=False
        lock_name='smsinvit:sms:'+sid
        try:
            if local_acquired:
                engine=getattr(self.db,'engine',None)
                if engine is not None and engine.dialect.name=='mysql':
                    with self.probe_lock:
                        if self.operation_lock_engine is None:
                            # Named locks must not consume the business pool while
                            # provider I/O is awaiting its next transaction connection.
                            # Preserve the actual configured DBAPI creator/TLS options.
                            self.operation_lock_engine=create_engine(engine.url,creator=engine.pool._creator,
                                pool_size=8,max_overflow=0,pool_timeout=0.05,
                                pool_pre_ping=True,isolation_level='AUTOCOMMIT',hide_parameters=True)
                        lock_engine=self.operation_lock_engine
                    try:connection=lock_engine.connect()
                    except PoolTimeout:pass
                    if connection is not None:
                        acquired=connection.exec_driver_sql('SELECT GET_LOCK(%s,0)',(lock_name,)).scalar()==1
                else:acquired=True
            if acquired:
                self.operation_context.held=held|{sid}
                contexts=getattr(self.operation_context,'connections',{})
                self.operation_context.connections=contexts|{sid:(connection,lock_name)}
            yield acquired
        finally:
            if acquired:
                self.operation_context.held=held
                self.operation_context.connections={k:v for k,v in self.operation_context.connections.items() if k!=sid}
            try:
                if connection is not None:
                    try:
                        if acquired and not connection.invalidated:
                            try:connection.exec_driver_sql('SELECT RELEASE_LOCK(%s)',(lock_name,))
                            except Exception:connection.invalidate()
                    finally:connection.close()
            finally:
                if local_acquired:entry[0].release()
                with self.probe_lock:
                    entry[1]-=1
                    if entry[1]==0:self.operation_locks.pop(sid,None)
    def operation_is_current(self):
        """A dropped lock connection must not authorize a stale provider completion."""
        for connection,name in getattr(self.operation_context,'connections',{}).values():
            if connection is None:continue
            if connection.invalidated or connection.closed:return False
            try:
                if connection.exec_driver_sql('SELECT IS_USED_LOCK(%s)=CONNECTION_ID()',(name,)).scalar()!=1:return False
            except Exception:
                connection.invalidate();return False
        return True
    def close(self):
        self.line_checker.close()
        if self.operation_lock_engine is not None:self.operation_lock_engine.dispose()
    @staticmethod
    def is_sku(attrs):return text(attrs.get('productKind')).upper()==PRODUCT_KIND if attrs.get('productKind') else attrs.get('smsService') is not None and attrs.get('smsCountry') is not None
    def register_issued_key(self,inventory_item_id,sku_id,attrs,plaintext):
        if not self.is_sku(attrs):return
        try:
            service=text(attrs.get('smsService'));country=int(attrs.get('smsCountry',-1));minimum=Decimal(str(attrs.get('smsMinPrice',0)));maximum=Decimal(str(attrs['smsMaxPrice']))
            if not service or country<0 or minimum>maximum:raise ValueError()
        except (KeyError,TypeError,ValueError,ArithmeticError):raise Error('SMS_SKU_MISCONFIGURED','接码 SKU 缺少 service/country/价格带配置。',500)
        valid=int(attrs.get('cdkValidDays',15));count=int(attrs.get('smsMinCount',20));kid=uid()
        self.m.insertRedeemKey(kid,str(inventory_item_id),str(sku_id),self.codec.digest(plaintext.strip()),service,country,minimum,maximum,count if count>0 else 20,normalize_exception(attrs.get('smsPhoneException')) or None,valid if valid>0 else 15)
        self.event(kid,None,'REGISTERED',{'inventoryItemId':str(inventory_item_id)})
    def event(self,key_id,sid,kind,detail):self.m.insertEvent(uid(),key_id,sid,kind,dumps(detail))
    def ensure_inventory(self,key):
        if not key:raise Error('SMS_CDK_NOT_FOUND','CDK 无效或不存在。',404)
        if key.get('status')=='DISABLED':raise Error('SMS_CDK_DISABLED','该 CDK 已被停用。',409)
        status=key.get('inventoryStatus') or self.m.lockInventoryStatus(key['inventoryItemId'])
        if status!='ISSUED':raise Error('SMS_CDK_DISABLED','该 CDK 已被作废或不可兑换。',409)
        if self.m.isCdkAmbiguous(key['codeFingerprint']):raise Error('SMS_CDK_AMBIGUOUS','该 CDK 存在重复记录，请联系客服核查。',409)
    def session(self,sid,locked=False):
        row=self.m.findSessionForUpdate(str(sid)) if locked else self.m.findSessionById(str(sid))
        if not row:raise Error('SMS_SESSION_NOT_FOUND','会话不存在。',404)
        return row
    def redeem(self,code):
        self.expire_due_keys();value=text(code)
        if not 4<=len(value)<=256 or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in value):raise Error('VALIDATION_ERROR','CDK 格式不合法。',422)
        resume=None;claim=None
        with self.db.transaction(requires_new=True):
            keys=self.m.findKeyByFingerprintForUpdate(self.codec.digest(value))
            if not keys:raise Error('SMS_CDK_NOT_FOUND','CDK 无效或不存在。',404)
            if len(keys)!=1:raise Error('SMS_CDK_AMBIGUOUS','该 CDK 存在重复记录，请联系客服核查。',409)
            key=keys[0];self.ensure_inventory(key)
            if self.codec.decrypt(key['secretCiphertext']).strip()!=value:raise Error('SMS_CDK_NOT_FOUND','CDK 无效或不存在。',404)
            kid=key['id'];status=key['status']
            if status=='USED':
                resume=self.m.findLatestSuccessSessionId(kid)
                if not resume:raise Error('SMS_CDK_USED','该 CDK 已使用成功。',409)
            elif status=='FAILED':raise Error('SMS_CDK_FAILED','该 CDK 已使用失败。',409)
            elif status=='EXPIRED' or (instant(key.get('expiresAt')) and instant(key['expiresAt'])<=now()):raise Error('SMS_CDK_EXPIRED','该 CDK 已过期。',410)
            else:
                resume=self.m.findActiveSessionId(kid)
                if not resume and (int(key['attemptCount'])>0 or status=='LOCKED'):
                    if int(key['attemptCount'])>=self.max_attempts():raise Error('SMS_CDK_FAILED','该 CDK 已使用失败。',409)
                    resume=self.m.findLatestSessionWithPhoneId(kid)
                    if not resume:raise Error('SMS_NUMBER_INVALID','上次号码已失效。请点击换号重新获取（仍有剩余次数时），或联系客服。',409)
                if not resume:claim=self.claim_attempt(key)
        if resume:return self.poll(resume) if self.session(resume)['status']=='WAITING_CODE' else self.view(resume)
        return self.finish_acquire(claim)
    def claim_attempt(self,key):
        kid=key['id'];active=self.m.findActiveSessionId(kid)
        if active:return {'resume':active}
        attempt=int(key['attemptCount'])+1
        if attempt>self.max_attempts():raise Error('SMS_CDK_FAILED','三次取号均未收到验证码，CDK 已作废。',409)
        # A compensated acquisition reuses its attempt row; capture its rejected
        # number before resetSessionPending clears it so we still exclude that prefix.
        exceptions=merge_exceptions(key.get('smsPhoneException'),self.m.listPhones(kid))
        sid=self.m.findReusableSessionId(kid,attempt)
        if sid:self.m.resetSessionPending(sid)
        else:sid=uid();self.m.insertSession(sid,kid,attempt)
        self.m.lockKey(kid,attempt)
        return {'sid':sid,'kid':kid,'service':key['smsService'],'country':key['smsCountry'],'min':Decimal(str(key['smsMinPrice'])),'max':Decimal(str(key['smsMaxPrice'])),'count':key['smsMinCount'],'exception':exceptions}
    def select_providers(self,service,country,minimum,maximum,count):
        ids=[]
        for k,v in service_map(self.client.get_prices(service,country),service,country).items():
            if not isinstance(v,dict) or v.get('count') is None or v.get('price') is None:continue
            if int(Decimal(str(v['count'])))>=count and minimum<=Decimal(str(v['price']))<=maximum:ids.append(str(v.get('provider_id',k)))
        if not ids:raise Error('SMS_UNAVAILABLE',self.settings.get('unavailableMessage'),503)
        return ','.join(ids)
    def with_auto_exclusions(self,exceptions):
        return normalize_exception(','.join([text(exceptions),*self.m.listAutoExcludedAreas()]))
    def release_claim(self,c,code):
        with self.db.transaction(requires_new=True):
            s=self.session(c['sid'],True)
            if s['status'] not in ('PENDING_NUMBER','ACQUIRE_UNKNOWN'):return
            self.m.findKeyForUpdate(c['kid']);self.m.markSessionUnavailable(c['sid']);self.m.rollbackAttempt(c['kid'])
            if code=='SMS_UNAVAILABLE':self.event(c['kid'],c['sid'],'UNAVAILABLE',{'reason':code})
            self.event(c['kid'],c['sid'],'GET_NUMBER_FAILED',{'code':code})
    def finish_acquire(self,c):
        if c.get('resume'):return self.view(c['resume'])
        with self.session_operation(c['sid']) as acquired:
            if not acquired:return self.view(c['sid'])
            return self._finish_acquire(c)
    def _finish_acquire(self,c):
        try:
            if not self.client.configured():raise Error('SMS_UNAVAILABLE',self.settings.get('unavailableMessage'),503)
            raw=self.client.get_balance()
            if text(raw).startswith('ACCESS_BALANCE:'):
                try:balance=Decimal(raw.split(':',1)[1])
                except ArithmeticError:balance=None
                if balance is not None and balance<max(Decimal(str(self.settings.get('minBalance',1))),c['max']):raise Error('SMS_UNAVAILABLE',self.settings.get('unavailableMessage'),503)
            providers=self.select_providers(c['service'],c['country'],c['min'],c['max'],c['count'])
        except Error as e:self.release_claim(c,e.code);raise
        with self.db.transaction(requires_new=True):started=self.m.startAcquisition(c['sid'])
        if started!=1:return self.view(c['sid'])
        base_exception=c['exception']
        c['exception']=self.with_auto_exclusions(base_exception)
        try:order=self.client.get_number(c['service'],c['country'],c['min'],c['max'],providers,c['exception'])
        except Error as e:
            if e.code in ('SMS_UNAVAILABLE','SMS_WRONG_EXCEPTION_PHONE'):self.release_claim(c,e.code)
            else:
                with self.db.transaction(requires_new=True):self.m.markSessionAcquireUnknown(c['sid']);self.event(c['kid'],c['sid'],'ACQUIRE_UNKNOWN',{'code':e.code})
            raise
        except Exception:
            with self.db.transaction(requires_new=True):self.m.markSessionAcquireUnknown(c['sid'])
            raise Error('SMS_PROVIDER_ERROR','取号结果暂时无法确认，请联系客服。',502) from None
        # Keep the paid order recoverable even if refreshing the shared pool fails.
        with self.db.transaction(requires_new=True):
            self.m.markSessionCancelPending(c['sid'],order.activation_id,order.phone,order.cost,True)
        c['exception']=self.with_auto_exclusions(base_exception)
        if not order.phone or excluded(order.phone,c['exception']):
            self.compensate(c,order);return self.view(c['sid'])
        result=self.line_checker.check(order.phone)
        if not result.skipped and result.line_type!='mobile':
            try:
                with self.db.transaction(requires_new=True):
                    if result.line_type=='voip':
                        prefix=voip_area_prefix(order.phone)
                        if prefix:
                            self.m.addAutoExcludedArea(prefix,'voip')
                            self.event(c['kid'],c['sid'],'VOIP_AREA_EXCLUDED',{'prefix':prefix,'type':'voip','provider':result.provider,'reasons':result.reasons})
                    self.event(c['kid'],c['sid'],'LINE_TYPE_REJECTED',{'type':result.line_type,'provider':result.provider,'reasons':result.reasons})
            finally:self.compensate(c,order)
            raise Error('SMS_LINE_NOT_MOBILE','该号码未被确认为手机号码，已申请取消，请重试。',503)
        if not self.operation_is_current():
            raise Error('SMS_RESULT_PENDING','取号操作锁已失效，原号码待取消确认。',409)
        try:
            with self.db.transaction(requires_new=True):
                # 这一句的取值为 FOR UPDATE 锁；行内容本身不用，所以不绑定变量。
                self.session(c['sid'],True);key=self.m.findKeyForUpdate(c['kid'])
                if not key or key['status']!='LOCKED':raise RuntimeError('CDK no longer available')
                if self.m.markSessionWaitingCode(c['sid'],order.activation_id,order.phone,order.cost,now()+timedelta(seconds=self.grace()),now()+timedelta(seconds=self.wait()))!=1:raise RuntimeError('Acquisition state changed')
                if result.skipped:self.event(c['kid'],c['sid'],'LINE_TYPE_SKIPPED',{'reasons':result.reasons})
                else:self.event(c['kid'],c['sid'],'LINE_TYPE_VERIFIED',{'type':'mobile','provider':result.provider,'reasons':result.reasons})
                self.event(c['kid'],c['sid'],'NUMBER_ACQUIRED',{'activationId':order.activation_id,'phone':mask_phone(order.phone),**({'cost':str(order.cost)} if order.cost is not None else {'costUnknown':True})})
        except Exception:
            self.compensate(c,order);raise Error('SMS_UNAVAILABLE',self.settings.get('unavailableMessage'),503) from None
        return self.view(c['sid'])
    def compensate(self,c,order):
        with self.db.transaction(requires_new=True):
            self.m.markSessionCancelPending(c['sid'],order.activation_id,order.phone,order.cost,True)
            self.event(c['kid'],c['sid'],'CANCEL_PENDING',{'activationId':order.activation_id,'phone':mask_phone(order.phone)})
        if self.try_cancel(order.activation_id):self.complete_cancellation(c['sid'],order.activation_id)
    def try_cancel(self,aid):
        if not aid:return False
        try:return text(self.client.set_status(aid,8))=='ACCESS_CANCEL' or self.client.get_status(aid).kind=='CANCEL'
        except Exception:return False
    def complete_cancellation(self,sid,aid):
        with self.db.transaction(requires_new=True):
            s=self.session(sid,True)
            if s['status']!='CANCEL_PENDING' or s['activationId']!=aid:return
            key=self.m.findKeyForUpdate(s['redeemKeyId']);kid=s['redeemKeyId']
            if db_true(s.get('cancelRefundAttempt')):self.m.markSessionUnavailable(sid);self.m.rollbackAttempt(kid)
            else:self.m.markSessionCancelled(sid)
            self.event(kid,sid,'CANCEL_CONFIRMED',{'activationId':aid})
            if not db_true(s.get('cancelRefundAttempt')) and key and key['status']=='LOCKED' and int(key['attemptCount'])>=self.max_attempts():self.m.markKeyFailed(kid);self.event(kid,sid,'KEY_FAILED',{'attempts':key['attemptCount']})
    def poll(self,sid):
        with self.session_operation(sid) as acquired:
            if not acquired:return self.view(sid)
            self.operation_context.query_unavailable=False
            result=self._poll(sid)
            return result | {'statusQueryUnavailable':self.operation_context.query_unavailable}
    def _poll(self,sid):
        sid=str(sid);self.expire_due_keys();s=self.session(sid)
        with self.db.transaction(requires_new=True):self.ensure_inventory(self.m.findKeyForUpdate(s['redeemKeyId']))
        latest=self.m.findActiveSessionId(s['redeemKeyId'])
        if latest and latest!=sid:return self.poll(latest)
        aid=text(s.get('activationId'));listen=instant(s.get('nextListenUntil'));expiry=instant(s.get('numberExpiresAt'))
        if s['status']=='SUCCESS' and ((listen and listen<=now()) or (expiry and expiry<=now())):
            if s.get('providerFinalizedAt'):return self.view(sid)
            if listen:
                result=self.provider_status(aid,closing=True)
                if result is None:return self.view(sid)
                if result.kind in ('OK','WAIT_RETRY') and result.code:
                    self.offer_code(sid,result.code,result.raw,expected_listen_until=listen)
                elif result.kind not in ('WAIT','WAIT_RETRY','CANCEL'):return self.view(sid)
                # A webhook can have consumed this window while the query was running.
                # Do not close a different/new window based on an old snapshot.
                current=self.session(sid)
                current_listen=instant(current.get('nextListenUntil'))
                if current_listen is not None and current_listen!=listen:return self.view(sid)
            self.finalize(sid,aid);return self.view(sid)
        listening=s['status']=='SUCCESS' and listen and listen>now()
        # Java parity: an expired number is cancelled from the front-end poll itself, never by buying another one.
        if s['status']=='WAITING_CODE' and expiry and expiry<=now():return self.cancel_and_maybe_retry(s['redeemKeyId'],sid,True)
        # Java parity: unconfirmed cancellations are re-checked on the same cadence as active numbers so a
        # lost setStatus response can still converge, and a code delivered before confirmation still wins.
        if aid and (s['status']=='WAITING_CODE' or listening or s['status']=='CANCEL_PENDING'):
            result=self.provider_status(aid)
            if result is None:return self.view(sid)
            if result.kind in ('OK','WAIT_RETRY') and result.code:return self.offer_code(sid,result.code,result.raw,expected_listen_until=listen) if listening else self.mark_success(sid,result.code,result.raw)
            if result.kind=='CANCEL':
                if listening:self.finalize(sid,aid)
                elif s['status']=='CANCEL_PENDING':self.complete_cancellation(sid,aid)
                else:
                    with self.db.transaction(requires_new=True):
                        locked=self.session(sid,True)
                        if locked['status'] in ('WAITING_CODE','PENDING_NUMBER'):self.m.markSessionCancelled(sid);self.event(s['redeemKeyId'],sid,'PROVIDER_CANCELLED',{'kind':'CANCEL'})
                with self.probe_lock:self.probes.pop(aid,None)
        return self.view(sid)
    def provider_status(self,aid,closing=False,force=False):
        if not aid:return None
        if not self.should_probe(aid,closing,force):
            with self.probe_lock:failed,_=self.probes.get(aid,(False,None))
            if failed is True:self.operation_context.query_unavailable=True
            return None
        try:
            result=self.client.get_status(aid)
            failed=result.kind=='ERROR'
            with self.probe_lock:
                _,last=self.probes.get(aid,(False,None));self.probes[aid]=(failed,last)
            if failed:self.operation_context.query_unavailable=True
            return result if self.operation_is_current() else None
        except Exception:
            with self.probe_lock:
                _,last=self.probes.get(aid,(False,None));self.probes[aid]=(True,last)
            self.operation_context.query_unavailable=True
            return None
    def should_probe(self,aid,closing=False,force=False):
        with self.probe_lock:
            failed,last=self.probes.get(aid,(False,None));current=time.monotonic()
            # Keep the normal supplier cadence at >=5s, but query immediately after
            # a quiet period/restart. HTTP request counts cannot measure elapsed time.
            floor=1 if closing else 5
            due=force or last is None or current-last>=max(floor,self.settings.runtime_int('sms_status_poll_min_ms',1000)/1000)
            if due:self.probes[aid]=(failed,current)
            return due
    def retry(self,sid):
        with self.session_operation(sid) as acquired:
            if not acquired:raise Error('SMS_RESULT_PENDING','原号码正在查询，请稍候再操作。',409)
            return self._retry(sid)
    def _retry(self,sid):
        sid=str(sid);self.expire_due_keys();s=self.session(sid)
        with self.db.transaction(requires_new=True):self.ensure_inventory(self.m.findKeyForUpdate(s['redeemKeyId']))
        if s['status']=='WAITING_CODE':
            if instant(s.get('cancelEligibleAt')) and instant(s['cancelEligibleAt'])>now():raise Error('SMS_CANCEL_TOO_EARLY','号码获取后需等待约 5 分钟才能换号。',409)
            return self.cancel_and_retry(sid,False)
        if s['status'] in ('CANCELLED','UNAVAILABLE'):return self.acquire_new(s['redeemKeyId'])
        if s['status'] in ('CANCEL_PENDING','ACQUIRE_UNKNOWN'):raise Error('SMS_RESULT_PENDING','取号或取消结果确认中，请稍候或联系客服，请勿重复换号。',409)
        raise Error('SMS_SESSION_STATE','当前会话不可换号。',409)
    def acquire_new(self,kid):
        failed=False
        with self.db.transaction(requires_new=True):
            key=self.m.findKeyForUpdate(kid);self.ensure_inventory(key)
            if key['status']=='USED':raise Error('SMS_CDK_USED','该 CDK 已使用成功，不能换号。',409)
            if key['status'] in ('FAILED','EXPIRED'):raise Error('SMS_SESSION_STATE','当前 CDK 不可换号。',409)
            if instant(key.get('expiresAt')) and instant(key['expiresAt'])<=now():raise Error('SMS_CDK_EXPIRED','该 CDK 已过期。',410)
            if int(key['attemptCount'])>=self.max_attempts():self.m.markKeyFailed(kid);self.event(kid,None,'KEY_FAILED',{'attempts':key['attemptCount']});failed=True
            else:claim=self.claim_attempt(key)
        if failed:raise Error('SMS_CDK_FAILED','三次取号均未收到验证码，CDK 已作废。',409)
        return self.finish_acquire(claim)
    def cancel_and_retry(self,sid,auto):
        s=self.session(sid)
        return self.cancel_and_maybe_retry(s['redeemKeyId'],sid,auto)
    def cancel_and_maybe_retry(self,kid,sid,auto):
        """Java parity for cancelAndMaybeRetry: provider I/O outside locks, terminal state re-read after."""
        with self.session_operation(sid) as acquired:
            if not acquired:return self.view(sid)
            return self._cancel_and_maybe_retry(kid,sid,auto)
    def _cancel_and_maybe_retry(self,kid,sid,auto):
        s=self.session(sid)
        if s['status'] in ('CANCELLED','UNAVAILABLE','FAILED'):return self.view(sid) if auto else self.acquire_new(kid)
        if s['status']=='SUCCESS' or not s.get('activationId'):return self.view(sid)
        # Preserve a code already delivered on the original activation before closing it.
        # An unavailable/unknown query cannot prove the number has no code: keep the
        # same session and let the next bounded read recover it, without buying again.
        # An explicit change must not silently become a no-op just because a poll
        # ran <1s ago. The operation lock still serializes reads/cancel/acquisition;
        # always obtain fresh no-code evidence before cancelling this paid number.
        result=self.provider_status(s['activationId'],closing=True,force=not auto)
        if result is None:return self.view(sid)
        if result.kind in ('OK','WAIT_RETRY') and result.code:
            view=self.mark_success(sid,result.code,result.raw)
            expiry=instant(self.session(sid).get('numberExpiresAt'))
            if view['status']=='SUCCESS' and expiry and expiry<=now():self.finalize(sid,s['activationId'])
            return self.view(sid)
        if result.kind not in ('WAIT','WAIT_RETRY','CANCEL'):return self.view(sid)
        if s['status']=='CANCEL_PENDING':claimed=1
        else:
            with self.db.transaction(requires_new=True):
                claimed=self.m.markSessionCancelPending(sid,s['activationId'],None,None,False)
                if claimed==1:self.event(kid,sid,'CANCEL_PENDING',{'activationId':s['activationId']})
        if claimed!=1:return self.view(sid)
        if result.kind!='CANCEL':
            if not self.operation_is_current():return self.view(sid)
            try:confirmed=text(self.client.set_status(s['activationId'],8))=='ACCESS_CANCEL'
            except Exception:confirmed=False
            if not self.operation_is_current():return self.view(sid)
            if not confirmed:
                # Cancellation responses can race code delivery. Consume the read
                # evidence rather than discarding an OK code inside try_cancel().
                try:after=self.client.get_status(s['activationId'])
                except Exception:return self.view(sid)
                if not self.operation_is_current():return self.view(sid)
                if after.kind in ('OK','WAIT_RETRY') and after.code:return self.mark_success(sid,after.code,after.raw)
                if after.kind!='CANCEL':return self.view(sid)
        self.complete_cancellation(sid,s['activationId'])
        terminal=self.cancel_terminal(kid,sid)
        if terminal=='FAILED':
            if auto:return self.view(sid)
            raise Error('SMS_CDK_FAILED','三次取号均未收到验证码，CDK 已作废。',409)
        if terminal=='EXPIRED':
            if auto:return self.view(sid)
            raise Error('SMS_CDK_EXPIRED','该 CDK 已过期。',410)
        latest=self.m.findActiveSessionId(kid)
        if latest:return self.view(latest)
        return self.view(sid) if auto else self.acquire_new(kid)
    def cancel_terminal(self,kid,sid):
        """Post-cancel key terminal state, in the same order as Java (expiry before attempt limit)."""
        with self.db.transaction(requires_new=True):
            s=self.session(sid,True)
            if s['status'] not in ('CANCELLED','UNAVAILABLE'):return None
            key=self.m.findKeyForUpdate(kid)
            if key and key['status'] in ('USED','EXPIRED','FAILED'):return key['status']
            if key and instant(key.get('expiresAt')) and instant(key['expiresAt'])<=now():
                self.m.markKeyExpired(kid);self.event(kid,sid,'KEY_EXPIRED',{});return 'EXPIRED'
            if key and int(key['attemptCount'])>=self.max_attempts():
                self.m.markKeyFailed(kid);self.event(kid,sid,'KEY_FAILED',{'attempts':key['attemptCount']});return 'FAILED'
        return None
    def next_sms(self,sid):
        with self.session_operation(sid) as acquired:
            if not acquired:raise Error('SMS_RESULT_PENDING','原号码正在查询，请稍候再操作。',409)
            return self._next_sms(sid)
    def _next_sms(self,sid):
        sid=str(sid);self.expire_due_keys();s=self.session(sid)
        with self.db.transaction(requires_new=True):self.ensure_inventory(self.m.findKeyForUpdate(s['redeemKeyId']))
        if s['status']!='SUCCESS':raise Error('SMS_SESSION_STATE','请先收到第一封验证码。',409)
        previous_listen=instant(s.get('nextListenUntil'))
        if previous_listen and previous_listen<=now() and not s.get('providerFinalizedAt'):
            self._poll(sid);s=self.session(sid)
            if s.get('nextListenUntil') and not s.get('providerFinalizedAt'):
                raise Error('SMS_RESULT_PENDING','正在确认上一封短信的原号码结果，请稍候。',409)
        if s.get('providerFinalizedAt'):raise Error('SMS_NEXT_CLOSED','接码已结束，不能再获取新短信。',409)
        if instant(s.get('nextListenUntil')) and instant(s['nextListenUntil'])>now():raise Error('SMS_ALREADY_LISTENING','正在监听新短信，请稍候。',409)
        if instant(s.get('numberExpiresAt')) and instant(s['numberExpiresAt'])<now():raise Error('SMS_NUMBER_EXPIRED','该号码已超时。',410)
        if len(parse_codes(s))>=self.max_attempts():raise Error('SMS_MAX_CODES','本号码验证码已达上限。',409)
        if not s.get('activationId'):raise Error('SMS_SESSION_STATE','会话缺少激活号。',409)
        if not self.operation_is_current():raise Error('SMS_RESULT_PENDING','原号码查询锁已失效，请重新查询原会话。',409)
        if text(self.client.set_status(s['activationId'],3))!='ACCESS_RETRY_GET':raise Error('SMS_PROVIDER_ERROR','上游尚未确认新短信请求，请稍后查询。',502)
        if not self.operation_is_current():raise Error('SMS_RESULT_PENDING','原号码查询锁已失效，请重新查询原会话。',409)
        with self.db.transaction(requires_new=True):
            locked=self.session(sid,True)
            if locked.get('providerFinalizedAt'):raise Error('SMS_NEXT_CLOSED','接码已结束。',409)
            if instant(locked.get('nextListenUntil')) and instant(locked['nextListenUntil'])>now():raise Error('SMS_ALREADY_LISTENING','正在监听新短信，请稍候。',409)
            self.m.startNextListen(sid,now()+timedelta(seconds=self.grace()));self.event(s['redeemKeyId'],sid,'NEXT_SMS_REQUESTED',{'codes':len(parse_codes(locked))})
        return self.view(sid)
    def on_webhook(self,aid,code,sms_text):
        if not aid:raise Error('VALIDATION_ERROR','缺少 activationId。',422)
        with self.db.transaction(requires_new=True):sid=self.m.findSessionIdByActivationForUpdate(aid)
        if not sid:return {'ok':True,'ignored':True}
        if not text(code):self.event(self.session(sid)['redeemKeyId'],sid,'WEBHOOK_PENDING',{'hasCode':False});return {'ok':True,'ignored':True}
        return self.offer_code(sid,code,sms_text) if self.session(sid)['status']=='SUCCESS' else self.mark_success(sid,code,sms_text)
    def mark_success(self,sid,code,sms_text):
        with self.db.transaction(requires_new=True):
            s=self.session(sid,True)
            if s['status']=='SUCCESS':return self.view(sid)
            if s['status']!='WAITING_CODE' and not (s['status']=='CANCEL_PENDING' and not s.get('cancelRefundAttempt')):self.event(s['redeemKeyId'],sid,'LATE_CODE_IGNORED',{'status':s['status']});return self.view(sid)
            key=self.m.findKeyForUpdate(s['redeemKeyId'])
            if not key or key['status']!='LOCKED':return self.view(sid)
            if self.m.markSessionSuccess(sid,text(code),sms_text,dumps([text(code)]))!=1:return self.view(sid)
            if self.m.markKeyUsed(s['redeemKeyId'])!=1:raise RuntimeError('SMS key and session commit must be atomic')
            self.event(s['redeemKeyId'],sid,'CODE_RECEIVED',{'hasCode':bool(text(code))})
            self.notify_card_redeemed(s['redeemKeyId'],key)
        return self.view(sid)
    def offer_code(self,sid,code,sms_text,expected_listen_until=None):
        finalize=False
        with self.db.transaction(requires_new=True):
            s=self.session(sid,True);listen=instant(s.get('nextListenUntil'));codes=parse_codes(s);incoming=text(code)
            # Only the final query for this exact original listening window may
            # save a code after its deadline. Webhooks retain their normal cutoff.
            if s.get('providerFinalizedAt') or s['status']!='SUCCESS' or not listen or (listen<=now() and listen!=expected_listen_until):return self.view(sid)
            if not incoming or incoming in codes or len(codes)>=self.max_attempts():return self.view(sid)
            codes.append(incoming);self.m.appendSmsCode(sid,incoming,sms_text,dumps(codes));self.event(s['redeemKeyId'],sid,'NEXT_SMS_RECEIVED',{'codeCount':len(codes)});finalize=len(codes)>=self.max_attempts()
        if finalize:self.finalize(sid,s.get('activationId'))
        return self.view(sid)
    def finalize(self,sid,aid):
        with self.session_operation(sid) as acquired:
            if not acquired:return
            return self._finalize(sid,aid)
    def _finalize(self,sid,aid):
        if not aid:return
        try:
            if self.session(sid).get('providerFinalizedAt'):return
            if not self.operation_is_current():return
            if text(self.client.set_status(aid,6))!='ACCESS_ACTIVATION':return
            if not self.operation_is_current():return
            with self.db.transaction(requires_new=True):
                s=self.session(sid,True)
                if not s.get('providerFinalizedAt'):self.m.markProviderFinalized(sid);self.event(s['redeemKeyId'],sid,'PROVIDER_FINALIZED',{})
        except Exception:return
    def view(self,sid):
        v=self.m.sessionView(str(sid))
        if not v:raise Error('SMS_SESSION_NOT_FOUND','会话不存在。',404)
        codes=parse_codes(v);status=v['status'];listen=instant(v.get('nextListenUntil'));expiry=instant(v.get('numberExpiresAt'));alive=expiry is None or expiry>now();listening=listen is not None and listen>now();finalized=v.get('providerFinalizedAt') is not None
        closing_pending=status=='SUCCESS' and listen is not None and listen<=now() and not finalized
        key_ok=v['keyStatus'] not in ('USED','FAILED','EXPIRED','DISABLED');remaining=int(v['attemptCount'])<self.max_attempts();eligible=instant(v.get('cancelEligibleAt'));pending=status in ('PENDING_NUMBER','ACQUIRE_UNKNOWN','CANCEL_PENDING') or closing_pending
        v.update(phoneMasked=mask_phone(v.get('phone')),phoneHistory=','.join(self.m.listDisplayPhones(v['redeemKeyId'])),smsCodes=codes or None,codeCount=len(codes),maxCodes=self.max_attempts(),maxAttempts=self.max_attempts(),canRetry=bool(not pending and key_ok and remaining and (status in ('CANCELLED','UNAVAILABLE') or status=='WAITING_CODE' and eligible and eligible<=now())),resultPending=pending,canRequestNextSms=bool(status=='SUCCESS' and alive and not finalized and not listening and not closing_pending and 0<len(codes)<self.max_attempts()),listeningNextSms=bool(status=='SUCCESS' and (listening or closing_pending)),providerFinalized=finalized,cancelGraceSeconds=self.grace(),codePending=status=='WAITING_CODE',numberInvalid=status in ('CANCELLED','UNAVAILABLE') or status=='WAITING_CODE' and not alive,smsServiceName=self.catalog.name('services',v['smsService']),smsCountryName=self.catalog.name('countries',v['smsCountry']),sessionSecret=self.tokens.issue('sms-session',str(sid)) if self.tokens else None)
        if v.get('phone') is None:v.pop('phoneMasked',None)
        if not v.get('phoneHistory'):v.pop('phoneHistory',None)
        if db_true(v.pop('cancelRefundAttempt',False)):
            for field in ('phone','phoneMasked','phoneHistory'):v.pop(field,None)
        # MySQL stores UTC without tzinfo. A zone-less JSON date is interpreted as
        # browser-local time, moving the number deadline by eight hours in China.
        for field in ('cancelEligibleAt','numberExpiresAt','keyExpiresAt','nextListenUntil'):
            value=instant(v.get(field))
            if value:v[field]=value.isoformat()+'Z'
        v.pop('nextSmsCode',None)
        return {k:value for k,value in v.items() if value is not None}
    def expire_due_keys(self):
        with self.db.transaction(requires_new=True):
            for kid in self.m.listExpiredKeyIds():
                key=self.m.findKeyForUpdate(kid)
                if not key or key['status'] not in ('UNUSED','LOCKED') or instant(key['expiresAt'])>now():continue
                for s in self.m.listActiveSessionsForKey(kid):
                    if s.get('activationId'):self.m.markSessionCancelPending(s['id'],s['activationId'],None,None,False)
                    elif s['status']=='PENDING_NUMBER':self.m.markSessionAcquireUnknown(s['id'])
                self.m.markKeyExpired(kid);self.event(kid,None,'KEY_EXPIRED',{})
    def sweep(self):
        self.expire_due_keys();self.m.markStaleAcquisitionsUnknown()
        for s in self.m.listTimedOutWaiting():
            try:self.cancel_and_retry(s['id'],True)
            except Error:pass
        for s in self.m.listTimedOutSuccess():self.poll(s['id'])
        for s in self.m.listCancelPending():
            if db_true(self.session(s['id']).get('cancelRefundAttempt')):
                with self.session_operation(s['id']) as acquired:
                    if acquired and self.try_cancel(s['activationId']):self.complete_cancellation(s['id'],s['activationId'])
            else:self.cancel_and_maybe_retry(s['redeemKeyId'],s['id'],True)
    def notify_card_redeemed(self,kid,key=None):
        """卡密核销完成（验证码核销成功，key 置为 USED）的唯一通知出口。

        必须在业务事务内调用：enqueue 与状态变更同事务提交，未配置 Telegram 或
        outbox 缺失时直接返回，任何异常都不影响主流程。event_key 唯一约束保证幂等。
        """
        if not self.outbox:return
        try:
            chat=self.outbox.value('telegram_ops_chat_id')
            if not chat:return
            key=key or self.m.findKeyById(kid)
            body=card_redeemed_body('接码',key.get('batchId'),key.get('codeFingerprint'),kid,
                [('服务/国家',f'{key.get("smsService")} / {key.get("smsCountry")}')])
            self.outbox.enqueue('sms.card-redeemed:'+str(kid),'TELEGRAM',CARD_REDEEMED_KIND,kid,{'chat':chat,'body':body},None)
        except Exception:return
    def admin_overview(self):
        balance=None;error=None
        try:
            if self.client.configured():balance=self.client.get_balance()
        except Exception:error='无法读取余额'
        counts=dict.fromkeys(('UNUSED','LOCKED','USED','FAILED','EXPIRED'),0);counts.update({r['status']:r['cnt'] for r in self.m.countKeysByStatus()})
        from smsinvit.common.mapper import Mapper as CommonMapper
        from .support import business_window
        stats=CommonMapper(self.db,'OpsStatsMapper');start,end=business_window(self.config)
        # Java exposes the商城 order finance figures for this DTO. The standalone redemption station has
        # no mall orders, so the same fields are reported as zero rather than dropped from the payload.
        return {'configured':self.client.configured(),**({'balanceRaw':balance} if balance is not None else {}),**({'balanceError':error} if error else {}),'keys':counts,'todayUsed':stats.smsUsedBetween(start,end),'todayFailed':stats.smsFailedBetween(start,end),'todaySalesMinor':0,'todayProfitMinor':0,'todaySupplierCost':stats.smsSupplierCostBetween(start,end)}
    def admin_redeems(self,page,size,status='',session_status='',cdk='',reveal=False):
        cdk_fp=self.codec.digest(text(cdk)) if text(cdk) else ''
        rows=self.m.pageRedeems(status or '',session_status or '',cdk_fp,size,(page-1)*size);fx=self.catalog.fx()
        for r in rows:
            r['lastPhoneMasked']=mask_phone(r.pop('lastPhone',None));r['codeFingerprintShort']=r['codeFingerprint'][:12]+'…';r['maxAttempts']=self.max_attempts();r['smsServiceName']=self.catalog.name('services',r['smsService']);r['smsCountryName']=self.catalog.name('countries',r['smsCountry']);r['phoneHistory']=','.join(self.m.listPhones(r['id'])) or None
            summaries=self.m.listSessionSummaries(r['id']);codes=[];total=Decimal(0);pending=False;success=False
            for s in summaries:
                for c in parse_codes(s):
                    if c not in codes:codes.append(c)
                current_codes=parse_codes(s);s['smsCodes']=current_codes or None;s['smsCode']=current_codes[-1] if current_codes else None;s['phoneMasked']=mask_phone(s.get('phone')) if s.get('phone') is not None else None
                if s['status']=='SUCCESS' and s.get('activationCost') is not None:
                    try:total+=Decimal(str(s['activationCost']));success=True
                    except ArithmeticError:pass
                if s['status'] in ('ACQUIRE_UNKNOWN','CANCEL_PENDING'):pending=True
            summaries=[{k:s.get(k) for k in ('attempt','phone','phoneMasked','status','activationCost','smsCodes','smsCode','smsText','nextSmsCode')} for s in summaries]
            r.update(smsCodes=codes or None,sessions=summaries or None,confirmedCostUsd=total if success else None,usdCnyRate=fx['usdCnyRate'],costSettlementStatus='PENDING' if pending else 'CONFIRMED' if success else ('CONFIRMED' if r.get('lastSessionStatus')=='SUCCESS' else 'PENDING' if r.get('lastSessionStatus') in ('ACQUIRE_UNKNOWN','CANCEL_PENDING') else 'NONE') if not summaries else 'NONE')
        fields=parse_json(Path(__file__).with_name('dto_fields.json').read_text())['SmsAdminRedeemItem'];rows=[{k:r.get(k) for k in fields} for r in rows]
        codes=inventory_code_map(self.db,self.codec,[r.get('inventoryItemId') for r in rows],reveal=reveal)
        for r in rows:
            r['smsCountry']=str(r['smsCountry']) if r['smsCountry'] is not None else None
            detail=codes.get(str(r.get('inventoryItemId')),{})
            r['codeMasked']=detail.get('codeMasked')
            if reveal:r['code']=detail.get('code')
        from smsinvit.errors import page_meta
        return ok(rows,page_meta(page,size,self.m.countRedeems(status or '',session_status or '',cdk_fp)))

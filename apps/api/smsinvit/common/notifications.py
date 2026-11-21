from __future__ import annotations
from datetime import datetime,timezone,timedelta
import re
from uuid import UUID,uuid4
import httpx
from fastapi import APIRouter,Request,Response,Body,Query
from .mapper import Mapper
from .catalog import parse_json,json_text
from ..errors import Error,ok,bad,page_meta
from ..security import ctx,require_permission

router=APIRouter()
# 管理员取消的通知在库里是 SKIPPED（CHECK 约束没有 CANCELLED），用这个错误码还原成
# 对外的 CANCELLED。写入、列表、详情共用这一处转换。
CANCELLED_CODE='ADMIN_CANCELLED'
def public_notification_status(row):
    out=dict(row)
    if out.get('status')=='SKIPPED' and out.get('lastErrorCode')==CANCELLED_CODE:out['status']='CANCELLED'
    return out
# 运营要求通知通道只保留 Telegram：KEYS/SECRETS 里不再有 email_*/smtp_*。
# 邮件配置写入接口因此自动拒绝这些键（supports() 只放行 KEYS）。
KEYS={'telegram_bot_token','telegram_ops_chat_id'}
SECRETS={'telegram_bot_token'}
# 只用于历史邮件行的正文脱敏：EMAIL 通道已删除，但库里可能仍有旧的认证邮件记录，
# 详情接口继续隐藏它们的正文，避免历史敏感内容被读出。
AUTH_KINDS={'PASSWORD_RESET','EMAIL_REGISTER_CODE','EMAIL_RESET_CODE'}
# 业务「卡密核销完成」通知：四个业务共用一个模板，event_key 前缀区分业务。
CARD_REDEEMED_KIND='CARD_REDEEMED'
# Telegram 的 400/403 描述 → 可辨识错误码。原来所有拒绝都归成 TELEGRAM_REJECTED，
# 运营只看到「请检查通道」，而 Telegram 其实已经说明了原因（如 chat not found）。
# 键是 Telegram description 里的小写片段，取第一个命中项。
TELEGRAM_FAILURES=(('chat not found','TELEGRAM_CHAT_NOT_FOUND','机器人访问不到该会话：请把机器人拉进这个群/频道，并在群里发一条消息后再取 Chat ID。'),
                   ('chat_id is empty','TELEGRAM_CHAT_EMPTY','Chat ID 为空，请填写运营群/频道的 Chat ID。'),
                   ('bot was blocked','TELEGRAM_BOT_BLOCKED','机器人被该会话拉黑，请先解除拉黑。'),
                   ('bot is not a member','TELEGRAM_BOT_NOT_MEMBER','机器人不是该会话成员，请把它拉进群。'),
                   ('not enough rights','TELEGRAM_NO_RIGHTS','机器人没有发言权限，请在群里给它发送消息的权限。'),
                   ('user not found','TELEGRAM_CHAT_NOT_FOUND','机器人访问不到该会话：请把机器人拉进这个群/频道，并在群里发一条消息后再取 Chat ID。'),
                   ('topic_closed','TELEGRAM_TOPIC_CLOSED','目标话题已关闭，无法发送。'),
                   ('message is too long','TELEGRAM_MESSAGE_TOO_LONG','消息过长，Telegram 拒收。'),
                   ('unauthorized','TELEGRAM_TOKEN_INVALID','Bot Token 无效，请重新复制粘贴。'))
def telegram_failure(data):
    """把 Telegram 返回的 description 转成「错误码 + 可执行的中文处理建议」。"""
    description=str((data or {}).get('description') or '').strip()[:200]
    low=description.lower()
    for needle,code,hint in TELEGRAM_FAILURES:
        if needle in low:return code,(f'{hint}（Telegram 原话：{description}）' if description else hint)
    if description:return 'TELEGRAM_REJECTED',f'Telegram 拒绝发送：{description}'
    return 'TELEGRAM_REJECTED','Telegram 拒绝发送，请检查通道。'

def card_redeemed_body(product,batch,fingerprint,key_id,extra,**times):
    """拼一条核销完成通知；批次/指纹等缺失时用「—」占位，绝不抛错影响业务。"""
    def line(label,value):return f'{label}：{value if value not in (None,"") else "—"}'
    rows=[f'✅【{product}】卡密核销完成',line('批次',batch),line('指纹',(fingerprint or '')[:12]+'…' if fingerprint else '')]
    rows.extend(line(label,value) for label,value in extra if value not in (None,''))
    rows.append(line('keyId',key_id))
    rows.append(line('时间',times.get('time') or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')))
    return '\n'.join(rows)
# notification_outbox_channel_check 仍只允许 ('EMAIL','TELEGRAM')（见
# migrations/legacy/V1__mysql_schema.sql），这里不改历史迁移；代码侧只再写 TELEGRAM，
# 旧的 EMAIL 行在 outbox 列表/详情里照常可读。
# 这四个码只可能来自 Telegram 通道本身；EMAIL_*/SMTP_* 与 OUTBOUND_ENDPOINT_FORBIDDEN
# 随邮件通道一起删除。
DEFINITE={'TELEGRAM_REJECTED','TELEGRAM_CONFIG_INVALID','TELEGRAM_DISABLED','CARD_REDEEMED',
          'TELEGRAM_CHAT_NOT_FOUND','TELEGRAM_CHAT_EMPTY','TELEGRAM_BOT_BLOCKED',
          'TELEGRAM_BOT_NOT_MEMBER','TELEGRAM_NO_RIGHTS','TELEGRAM_TOKEN_INVALID',
          'TELEGRAM_TOPIC_CLOSED','TELEGRAM_MESSAGE_TOO_LONG'}

class Notifications:
    def __init__(self,services):
        self.s=services;self.db=services.db;self.channels=Mapper(self.db,'NotificationChannelsMapper');self.mapper=Mapper(self.db,'NotificationOutboxMapper');self.worker=Mapper(self.db,'NotificationWorkerMapper')
    def supports(self,key):return key.startswith('notify.') and key[7:] in KEYS
    def value(self,name):
        if name not in KEYS:raise ValueError('Unknown notification field')
        key='notify.'+name;encrypted=self.channels.findEncrypted(key)
        if encrypted:return self.s.codec.decrypt(encrypted[0])
        old=self.channels.findLegacyValueJson(key)
        if old:
            value=parse_json(old[0])
            if isinstance(value,dict):value=value.get('value')
            if value is not None:return 'true' if value is True else 'false' if value is False else str(value)
        return str(self.s.config.get(name.upper(),''))
    def admin_values(self):
        rows=[]
        for name in sorted(KEYS):
            raw=self.value(name);secret=name in SECRETS
            output={'value':'********' if secret and raw else raw}
            rows.append({'key':'notify.'+name,'category':'notify','valueJson':output,'visibility':'PRIVATE','isSecret':secret})
        return rows
    def validate(self,name,raw):
        if len(raw)>4096 or any(c in raw for c in '\r\n\0'):raise Error('NOTIFICATION_CONFIG_INVALID','通知配置包含非法字符或过长。',422)
        if not raw or raw=='********' and name in SECRETS:return
        if name.endswith('chat_id') and not re.fullmatch('-?[0-9]{1,20}',raw):raise Error('NOTIFICATION_CONFIG_INVALID','Telegram 群 ID 必须是数字。',422)
        if name=='telegram_bot_token' and not re.fullmatch('[0-9]{5,20}:[A-Za-z0-9_-]{20,200}',raw):raise Error('NOTIFICATION_CONFIG_INVALID','Telegram Bot Token 格式错误。',422)
    def save(self,key,value,actor):
        if not self.supports(key) or not isinstance(value,dict):raise Error('NOTIFICATION_CONFIG_INVALID','通知配置格式错误。',422)
        name=key[7:];field='value'
        if set(value)!={field}:raise Error('NOTIFICATION_CONFIG_INVALID','通知配置字段错误。',422)
        input_=value[field]
        if not isinstance(input_,str):raise Error('NOTIFICATION_CONFIG_INVALID','通知配置必须为文本。',422)
        raw=input_.strip()
        self.validate(name,raw)
        with self.db.advisory_lock('settings.write',10) as acquired:
            if not acquired:raise Error('CONFLICT','设置更新繁忙，请稍后重试。',409)
            for old in KEYS:
                old_key='notify.'+old
                if self.channels.legacyExists(old_key):self.channels.insertIgnore(old_key,self.s.codec.encrypt(self.value(old)),actor.user_id);self.channels.deleteLegacy(old_key)
            if not (name in SECRETS and raw=='********'):self.channels.upsert(key,self.s.codec.encrypt(raw),actor.user_id)
            self.s.audit.record(actor.user_id,'notification.channel.update','setting',key,{'field':name})
            return next(row for row in self.admin_values() if row['key']==key)
    def enqueue(self,event,channel,kind,reference,payload,expires):
        if not self.db.in_transaction:raise RuntimeError('Notification enqueue requires a business transaction')
        id=str(uuid4());encrypted=self.s.codec.encrypt(json_text(payload)) if payload is not None else None
        self.mapper.enqueue(id,event,channel,kind,str(reference) if reference else None,encrypted,expires)
        return self.mapper.findIdByEventKey(event)
    def test_telegram(self,actor):
        self.s.limiter.check('telegram-test:'+actor.user_id,5);chat=self.value('telegram_ops_chat_id')
        if not chat or not self.value('telegram_bot_token'):raise Error('TELEGRAM_DISABLED','请先保存 Bot Token 和运营群 Chat ID。',422)
        with self.db.transaction():
            id=self.enqueue('telegram.test:'+str(uuid4()),'TELEGRAM','TELEGRAM',None,{'chat':chat,'body':'SmsInvit 兑换服务通知通道验证。'},datetime.now(timezone.utc)+timedelta(seconds=1800));self.s.audit.record(actor.user_id,'notification.test.enqueue','notification',id,{'channel':'TELEGRAM'});return {'queued':True,'sent':False,'notificationId':id}
    def list(self,page,size,status):
        if status and status not in ('PENDING','SENDING','SENT','FAILED','UNCERTAIN','SKIPPED','CANCELLED'):raise bad('通知状态错误。')
        filter_status,cancelled=('SKIPPED',True) if status=='CANCELLED' else (status,False)
        rows=[public_notification_status(row) for row in self.mapper.page(filter_status or '',cancelled,size,(page-1)*size)]
        return ok(rows,page_meta(page,size,self.mapper.count(filter_status or '',cancelled)))
    def detail(self,actor,id,reveal=False):
        with self.db.transaction():
            row=self.mapper.findById(id)
            if not row:raise Error('NOTIFICATION_NOT_FOUND','发送任务不存在。',404)
            row=dict(row);encrypted=row.pop('payloadEncrypted',None);payload=None;kind=row['kind']
            # 主动取消的记录在库里是 SKIPPED，详情也要显示成 CANCELLED。
            if row.get('status')=='SKIPPED' and row.get('lastErrorCode')==CANCELLED_CODE:row['status']='CANCELLED'
            # 认证邮件（AUTH_KINDS）的正文**任何情况下都不返回**：body 里就是重置链接/验证码
            # 这类凭据，`reveal` 曾绕过这层脱敏，导致带 inventory.export 的人能直接读到它
            # （test_authentication_notification_payload_never_revealed 一直要求
            # `'reset-private-token' not in ...`）。reveal 只对普通通知生效。
            if kind in AUTH_KINDS:payload={'_redacted':True,'body':'认证邮件内容已隐藏。'}
            elif encrypted:
                try:
                    payload=parse_json(self.s.codec.decrypt(encrypted))
                    for field in ('challengeKey','codeHash','tokenHash','token','code'):payload.pop(field,None)
                except (ValueError,Error):payload={'_undecryptable':True,'_reason':'负载无法解密（加密密钥可能已变更）'}
            row['payload']=payload
            if payload:row['to']=payload.get('to') or payload.get('chat');row['subject']=payload.get('subject')
            return row
    def retry(self,actor,id,ack):
        self.s.limiter.check('notification-retry:'+actor.user_id,30)
        with self.db.transaction():
            row=self.mapper.findForUpdate(id)
            if not row:raise Error('NOTIFICATION_NOT_FOUND','发送任务不存在。',404)
            status=row['status']
            if status not in ('FAILED','UNCERTAIN','SKIPPED'):raise Error('NOTIFICATION_NOT_RETRYABLE','该任务当前不能重发。',409)
            if status=='UNCERTAIN' and not ack:raise Error('NOTIFICATION_CONFIRM_REQUIRED','发送结果不确定，重发可能重复通知，请先核查。',409)
            expires=row.get('expiresAt')
            if expires and (expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires)<=datetime.now(timezone.utc):raise Error('NOTIFICATION_EXPIRED','通知已过期，请重新申请。',409)
            self.mapper.resetToPending(id);self.s.audit.record(actor.user_id,'notification.retry','notification',id,{'previousStatus':status});return {'queued':True}
    def cancel(self,actor,id):
        """只取消未发送项；已发送/发送中的记录原地返回，绝不改写结果。

        `notification_outbox_status_check` 不含 CANCELLED（见
        migrations/legacy/V1__mysql_schema.sql），所以终态落库为 SKIPPED +
        last_error_code='ADMIN_CANCELLED'，对外仍按契约回答 CANCELLED。
        """
        with self.db.transaction():
            row=self.mapper.findForUpdate(id)
            if not row:raise Error('NOTIFICATION_NOT_FOUND','发送任务不存在。',404)
            if row['status'] not in ('PENDING','FAILED','SKIPPED'):
                return {'id':id,'status':public_notification_status(row)['status']}
            if row['status']=='SKIPPED' and row.get('lastErrorCode')==CANCELLED_CODE:
                return {'id':id,'status':'CANCELLED'}
            self.mapper.cancelUnsent(id)
            self.s.audit.record(actor.user_id,'notification.cancel','notification',id,{'previousStatus':row['status']})
        return {'id':id,'status':'CANCELLED'}
    def telegram(self,payload):
        token=self.value('telegram_bot_token');chat=str(payload.get('chat',''))
        if not token or not chat:raise Error('TELEGRAM_DISABLED','Telegram 通道未配置。',503)
        if not re.fullmatch('[0-9]{5,20}:[A-Za-z0-9_-]{20,200}',token) or not re.fullmatch('-?[0-9]{1,20}',chat):raise Error('TELEGRAM_CONFIG_INVALID','Telegram 配置无效。',422)
        with httpx.Client(verify=self.s.config.get('SMSINVIT_CA_BUNDLE',True),follow_redirects=False,trust_env=False,timeout=20) as client:
            r=client.post('https://api.telegram.org/bot'+token+'/sendMessage',json={'chat_id':chat,'text':str(payload.get('body',''))[:4000],'disable_web_page_preview':True})
        if r.is_redirect or len(r.content)>65536:raise Error('TELEGRAM_RESULT_UNCERTAIN','Telegram 响应异常，需核查。',502)
        data=r.json()
        if r.status_code!=200 or data.get('ok') is not True:
            code,message=telegram_failure(data);raise Error(code,message,502)
        id=data.get('result',{}).get('message_id')
        if not isinstance(id,int):raise Error('TELEGRAM_RESULT_UNCERTAIN','发送结果需核查。',502)
        return str(id)
    def tick(self):
        with self.db.transaction():
            self.worker.markExpiredSendingUncertain();self.worker.markExpiredPendingSkipped();id=self.worker.selectPendingIdForUpdate()
            if not id:return
            lease=str(uuid4());self.worker.markSending(id,lease);job=self.worker.findById(id)
        attempted=False;status='FAILED';code=None;remote=None
        try:
            if not job.get('payloadEncrypted'):status='SKIPPED';code='CHANNEL_DISABLED_OR_EVENT_OBSOLETE';return
            payload=parse_json(self.s.codec.decrypt(job['payloadEncrypted']))
            if not self.worker.sendLeaseValid(id,lease):raise Error('NOTIFICATION_LEASE_LOST','通知事件已失效。',409)
            if not self.value('telegram_bot_token') or not payload.get('chat'):status='SKIPPED';code='TELEGRAM_DISABLED';return
            attempted=True;remote=self.telegram(payload)
            status='SENT'
        except Exception as exc:
            code=exc.code if isinstance(exc,Error) and re.fullmatch('[A-Z][A-Z0-9_]{0,99}',exc.code) else 'NOTIFICATION_SEND_FAILED'
            status='SKIPPED' if code=='NOTIFICATION_LEASE_LOST' else 'UNCERTAIN' if attempted and code not in DEFINITE else 'FAILED'
        finally:
            with self.db.transaction():self.worker.finish(id,lease,status,code,remote)

@router.post('/api/v1/admin/settings/test-telegram')
def test_telegram(request:Request):return ok(ctx(request).notifications.test_telegram(require_permission(request,'settings.update')))
@router.get('/api/v1/admin/notifications/outbox')
def outbox(request:Request,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100),status:str|None=Query(None,max_length=32)):require_permission(request,'settings.read');return ctx(request).notifications.list(page,pageSize,status)
@router.get('/api/v1/admin/notifications/outbox/{id}')
def detail(request:Request,response:Response,id:UUID,revealSecrets:bool=False):
    actor=require_permission(request,'settings.read')
    if revealSecrets:require_permission(request,'inventory.export')
    if revealSecrets:
        # 解密的正文含历史认证邮件的一次性验证码，必须留痕。
        ctx(request).audit.record(getattr(actor,'user_id',None) or actor.id,'notification.payload_revealed','notification',str(id),{'revealSecrets':True})
    response.headers['Cache-Control']='no-store';return ok(ctx(request).notifications.detail(actor,str(id),revealSecrets))
@router.post('/api/v1/admin/notifications/outbox/{id}/retry')
def retry(request:Request,id:UUID,body:dict|None=Body(None)):return ok(ctx(request).notifications.retry(require_permission(request,'settings.update'),str(id),(body or {}).get('acknowledgeDuplicateRisk') is True))
@router.post('/api/v1/admin/notifications/outbox/{id}/cancel')
def cancel(request:Request,id:UUID):return ok(ctx(request).notifications.cancel(require_permission(request,'settings.update'),str(id)))

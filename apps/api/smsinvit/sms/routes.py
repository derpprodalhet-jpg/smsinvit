from __future__ import annotations
import hmac,json
from uuid import UUID
from typing import Annotated
from fastapi import APIRouter,Request,Query
from pydantic import BaseModel,Field,ConfigDict
from smsinvit.errors import Error,ok
from smsinvit.security import ctx,require_permission,client_ip
from smsinvit.public_limits import cards_rate,require_session
from .support import text,mask
from starlette.concurrency import run_in_threadpool

router=APIRouter()
class RedeemBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    code:Annotated[str,Field(min_length=4,max_length=256)]
class SessionBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sessionSecret:Annotated[str|None,Field(max_length=128)]=None
class AreaBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    prefix:Annotated[str,Field(min_length=3,max_length=32)]
class SettingsBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    apiKey:Annotated[str|None,Field(max_length=16384)]=None
    baseUrl:Annotated[str|None,Field(max_length=2048)]=None
    publicApiBase:Annotated[str|None,Field(max_length=2048)]=None
    webhookAllowedIps:Annotated[str|None,Field(max_length=2000)]=None
    unavailableMessage:Annotated[str|None,Field(max_length=20000)]=None
    pageIntro:Annotated[str|None,Field(max_length=20000)]=None
    importantNotices:Annotated[str|None,Field(max_length=20000)]=None
    deliveryInstructions:Annotated[str|None,Field(max_length=20000)]=None
    minBalance:Annotated[float|None,Field(ge=0,le=1000000)]=None
    rotateWebhookToken:bool|None=None
    clearApiKey:bool|None=None
    numlookupApiKey:Annotated[str|None,Field(max_length=256)]=None
    abstractApiKey:Annotated[str|None,Field(max_length=256)]=None
    clearNumlookupApiKey:bool|None=None
    clearAbstractApiKey:bool|None=None

def service(r):return ctx(r).sms
def capability(r,sid,body=None,operation='read'):
    token=r.headers.get('x-session-secret') or (body.sessionSecret if body else None)
    require_session(r,'sms','sms-session',sid,token,operation)
def public_settings(r,reveal):
    s=service(r).settings;v=s.load();token=text(v.get('webhookToken'));base=s.public_base()
    out={k:s.get(k) for k in ('baseUrl','webhookAllowedIps','unavailableMessage','pageIntro','importantNotices','deliveryInstructions','minBalance')}
    out.update(publicApiBase=base,apiKeyConfigured=bool(s.secret()),apiKeyMasked=mask(s.secret()),webhookTokenMasked=token[:4]+'****' if reveal and token else '********' if token else None,webhookUrl=base+'/api/v1/sms/webhook/'+(token if reveal else '[REDACTED]') if base and token else None)
    out['lineChecks']={name:bool(s.secret(key)) for name,key in [('numlookupapi','numlookupApiKey'),('abstractapi','abstractApiKey')]}
    out['autoExcludedAreas']=service(r).m.autoExcludedAreas()
    return out
@router.get('/api/v1/sms/page-content')
def page_content(r:Request):
    s=service(r).settings;return ok({'intro':s.get('pageIntro'),'notices':[p.strip() for p in s.get('importantNotices').splitlines() if p.strip()]})
@router.post('/api/v1/sms/redeem')
def redeem(r:Request,body:RedeemBody):
    cards_rate(r,'sms',[body.code])
    return ok(service(r).redeem(body.code))
@router.get('/api/v1/sms/sessions/{sid}')
def session(r:Request,sid:UUID):
    capability(r,sid)
    return ok(service(r).poll(str(sid)))
@router.post('/api/v1/sms/sessions/{sid}/fetch-code')
def fetch(r:Request,sid:UUID,body:SessionBody|None=None):capability(r,sid,body);return ok(service(r).poll(str(sid)))
@router.post('/api/v1/sms/sessions/{sid}/retry')
def retry(r:Request,sid:UUID,body:SessionBody|None=None):capability(r,sid,body,'retry');return ok(service(r).retry(str(sid)))
@router.post('/api/v1/sms/sessions/{sid}/next-sms')
def next_sms(r:Request,sid:UUID,body:SessionBody|None=None):capability(r,sid,body,'next-sms');return ok(service(r).next_sms(str(sid)))
@router.post('/api/v1/sms/webhook/{token}')
async def webhook(r:Request,token:str):
    raw=await r.body()
    if len(raw)>65536:raise Error('PAYLOAD_TOO_LARGE','请求内容过大。',413)
    s=service(r);expected=text(s.settings.get('webhookToken'))
    if len(token)>256 or not token.isascii() or not expected or not hmac.compare_digest(expected,token):raise Error('SMS_WEBHOOK_FORBIDDEN','Webhook 未授权。',403)
    allowed={i.strip() for i in text(s.settings.get('webhookAllowedIps')).split(',') if i.strip()}
    if allowed and client_ip(r) not in allowed:raise Error('SMS_WEBHOOK_FORBIDDEN','Webhook 来源未允许。',403)
    try:payload=json.loads(raw) if raw else {}
    except ValueError:payload={}
    if not isinstance(payload,dict):payload={}
    aid=text(payload.get('activationId',payload.get('activation_id')))[:128];code=text(payload.get('code'))[:64];sms=text(payload.get('text',payload.get('sms')))[:2000]
    return await run_in_threadpool(s.on_webhook,aid,code,sms)
@router.get('/api/v1/admin/sms/overview')
def overview(r:Request):require_permission(r,'inventory.read');return ok(service(r).admin_overview())
@router.get('/api/v1/admin/sms/redeems')
def redeems(r:Request,page:Annotated[int,Query(ge=1)]=1,pageSize:Annotated[int,Query(ge=1,le=100)]=20,status:Annotated[str,Query(max_length=64)]='',sessionStatus:Annotated[str,Query(max_length=64)]='',cdk:Annotated[str,Query(max_length=256)]=''):
    principal=require_permission(r,'inventory.read');reveal=ctx(r).permissions.allows(principal,'inventory.export')
    if reveal:ctx(r).audit.record(principal.user_id,'sms.batch_reveal','sms_redeem_key',None,{'scope':'list'})
    return service(r).admin_redeems(page,pageSize,status,sessionStatus,cdk,reveal)
@router.get('/api/v1/admin/sms/catalog/countries')
def countries(r:Request):require_permission(r,'inventory.read');return ok(service(r).catalog.directory('countries'))
@router.get('/api/v1/admin/sms/catalog/services')
def services(r:Request):require_permission(r,'inventory.read');return ok(service(r).catalog.directory('services'))
@router.get('/api/v1/admin/sms/catalog/prices')
def prices(r:Request,service:Annotated[str,Query(min_length=1,max_length=32)],country:Annotated[int,Query(ge=0)]):require_permission(r,'inventory.read');return ok(ctx(r).sms.catalog.prices_board(service.strip(),country))
@router.get('/api/v1/admin/sms/catalog/fx')
def fx(r:Request):require_permission(r,'settings.read');return ok(service(r).catalog.fx()|{'fallbackRate':service(r).settings.runtime_float('sms_usd_cny_rate_fallback',7.2)})
@router.get('/api/v1/admin/sms/settings')
def settings(r:Request):
    principal=require_permission(r,'settings.read');s=service(r).settings
    if not s.get('webhookToken'):s.save_body({})
    return ok(public_settings(r,ctx(r).permissions.allows(principal,'inventory.export')))
@router.post('/api/v1/admin/sms/settings')
def save_settings(r:Request,body:SettingsBody):
    principal=require_permission(r,'settings.update');values=body.model_dump(exclude_none=True);s=service(r).settings
    url=values.get('baseUrl') or s.get('baseUrl');ctx(r).policy.require_allowed(url)
    s.save_body(values,'publicApiBase');ctx(r).audit.record(principal.user_id,'sms.settings_updated','settings','sms.smsbower',{'fields':[k for k in values if k not in ('apiKey','numlookupApiKey','abstractApiKey')]})
    return ok(public_settings(r,ctx(r).permissions.allows(principal,'inventory.export')))

@router.get('/api/v1/admin/sms/line-check-status')
def line_check_status(r:Request):
    require_permission(r,'settings.read')
    s=service(r)
    return ok(s.line_monitor.snapshot(s.settings))

@router.get('/api/v1/admin/sms/excluded-areas')
def excluded_areas(r:Request):
    require_permission(r,'settings.read')
    return ok(service(r).m.autoExcludedAreas())

@router.post('/api/v1/admin/sms/excluded-areas')
def add_excluded_area(r:Request,body:AreaBody):
    from .service import normalize_area_prefix
    actor=require_permission(r,'settings.update');prefix=normalize_area_prefix(body.prefix);s=service(r)
    with ctx(r).db.transaction():
        s.m.addManualExcludedArea(prefix)
        ctx(r).audit.record(actor.user_id,'sms.area.add','sms_excluded_area',prefix,{'prefix':prefix})
    return ok(s.m.autoExcludedAreas())

@router.delete('/api/v1/admin/sms/excluded-areas/{prefix}')
def delete_excluded_area(r:Request,prefix:str):
    from .service import normalize_area_prefix
    actor=require_permission(r,'settings.update');prefix=normalize_area_prefix(prefix);s=service(r)
    with ctx(r).db.transaction():
        s.m.deleteExcludedArea(prefix)
        ctx(r).audit.record(actor.user_id,'sms.area.delete','sms_excluded_area',prefix,{'prefix':prefix})
    return ok(s.m.autoExcludedAreas())

from __future__ import annotations
from decimal import Decimal,InvalidOperation
import json
from uuid import uuid4,UUID
from fastapi import APIRouter,Request,Body,Query
from .mapper import Mapper
from .auth import bounded
from ..errors import Error,ok,bad,page_meta
from ..security import ctx,require_permission

router=APIRouter()
KINDS={'SMS_REDEEM','CODEX_INVITE_REDEEM'}
def like(value):return value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
def parse_json(raw,default=None):
    if raw is None:return {} if default is None else default
    if isinstance(raw,(dict,list)):return raw
    try:return json.loads(raw)
    except (ValueError,TypeError):raise bad('配置无法读取。')
def json_text(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def uuid(value):
    try:return str(UUID(str(value)))
    except (ValueError,TypeError):raise bad('记录标识格式无效。')
def require_map(value):
    if not isinstance(value,dict):raise bad('配置必须为对象。')
    if len(json_text(value))>65536:raise bad('配置内容过大。')
    def depth(v,level):
        if level>10:raise bad('配置层级过深。')
        if isinstance(v,dict):
            if len(v)>256:raise bad('配置字段过多。')
            for k,x in v.items():
                if not isinstance(k,str) or len(k)>128:raise bad('配置字段无效。')
                depth(x,level+1)
        elif isinstance(v,list):
            if len(v)>1000:raise bad('配置列表过长。')
            for x in v:depth(x,level+1)
        elif not isinstance(v,(str,int,float,bool,type(None))):raise bad('配置值无效。')
    depth(value,0);return value

def validate_attrs(source,invitation=None):
    attrs=dict(require_map(source));kind=str(attrs.get('productKind','')).upper()
    if kind not in KINDS:raise bad('套餐类型须为接码或 Codex 邀请。')
    attrs['productKind']=kind
    def integer(field,min_,max_,required=False):
        value=attrs.get(field)
        if value is None and not required:return
        try:
            number=Decimal(str(value));i=int(number)
            if number!=i or i<min_ or i>max_ or not number.is_finite():raise ValueError()
        except (ValueError,InvalidOperation,OverflowError):raise bad(f'套餐参数 {field} 必须为有效整数。')
    if kind=='SMS_REDEEM':
        if not str(attrs.get('smsService','')).strip():raise bad('接码套餐缺少服务。')
        integer('smsCountry',0,2147483647,True)
        try:
            min_=Decimal(str(attrs.get('smsMinPrice',0)));max_=Decimal(str(attrs['smsMaxPrice']))
            if not min_.is_finite() or not max_.is_finite() or min_<0 or max_<min_:raise ValueError()
        except (ValueError,InvalidOperation,KeyError):raise bad('接码价格带无效。')
        integer('smsMinCount',1,2147483647)
    if kind=='CODEX_INVITE_REDEEM' and attrs.get('invitationPreferences') is not None:
        prefs=require_map(attrs['invitationPreferences'])
        allowed={'invite_timeout','otp_timeout','want_2fa','want_sms','want_refresh_token','sms_country','sms_service','sms_min_price','sms_max_price','say_hi','say_hi_mode','say_hi_model','say_hi_profile','say_hi_prompt','cli_timeout','accept_referral','accept_without_say_hi'}
        if set(prefs)-allowed:raise bad('邀请套餐包含不支持的参数。')
        if invitation and hasattr(invitation,'validate_preferences'):attrs['invitationPreferences']=invitation.validate_preferences(prefs)
    integer('cdkValidDays',1,3650 if kind=='CODEX_INVITE_REDEEM' else 2147483647)
    return attrs

class Plans:
    def __init__(self,services):self.s=services;self.db=services.db;self.mapper=Mapper(self.db,'PlanMapper')
    def response(self,row):
        if not row:raise Error('SKU_NOT_FOUND','套餐不存在。',404)
        row=dict(row);row['attributesJson']=parse_json(row['attributesJson']);return row
    def get(self,id,lock=False):return self.response(self.mapper.lock(str(id)) if lock else self.mapper.find(str(id)))
    def list(self,page,size,search=None,kind=None):
        query='%'+like((search or '').strip())+'%' if search and search.strip() else ''
        kind=(kind or '').strip().upper()
        if kind and kind!='ALL':
            if kind not in KINDS:raise bad('套餐类型筛选无效。')
            total=self.mapper.countFiltered(query,kind);rows=self.mapper.listFiltered(query,kind,size,(page-1)*size)
        else:total=self.mapper.count(query);rows=self.mapper.list(query,size,(page-1)*size)
        return ok([self.response(row) for row in rows],page_meta(page,size,total))
    def create(self,body,actor):
        code=bounded(body.get('code'),100,True).strip().upper();name=bounded(body.get('name'),200,True).strip();attrs=validate_attrs(body.get('attributesJson'),getattr(self.s,'invitation',None));id=str(uuid4())
        with self.db.transaction():
            self.mapper.insert(id,code,name,json_text(attrs));self.s.audit.record(actor.user_id,'plan.create','plan',id,{'code':code});return self.get(id)
    def update(self,id,body,actor):
        with self.db.transaction():
            existing=self.get(id,True);attrs=existing['attributesJson'] if body.get('attributesJson') is None else validate_attrs(body['attributesJson'],getattr(self.s,'invitation',None));name=existing['name'] if body.get('name') is None else bounded(body['name'],200,True).strip()
            self.mapper.update(id,name,json_text(attrs));self.s.audit.record(actor.user_id,'plan.update','plan',id,{});return self.get(id)
    def status(self,id,enabled,actor):
        with self.db.transaction():self.get(id,True);self.mapper.status(id,'ACTIVE' if enabled else 'INACTIVE');self.s.audit.record(actor.user_id,'plan.enable' if enabled else 'plan.disable','plan',id,{});return self.get(id)
    def delete(self,id,actor):
        with self.db.transaction():self.get(id,True);self.mapper.delete(id);self.s.audit.record(actor.user_id,'plan.delete','plan',id,{})

@router.get('/api/v1/admin/plans')
def list_plans(request:Request,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100),search:str|None=Query(None,max_length=200),productKind:str|None=Query(None,max_length=32)):
    require_permission(request,'sku.read');return ctx(request).plans.list(page,pageSize,search,productKind)
@router.get('/api/v1/admin/plans/{id}')
def get_plan(request:Request,id:UUID):require_permission(request,'sku.read');return ok(ctx(request).plans.get(str(id)))
@router.post('/api/v1/admin/plans')
def create_plan(request:Request,body:dict=Body(...)):return ok(ctx(request).plans.create(body,require_permission(request,'sku.create')))
@router.patch('/api/v1/admin/plans/{id}')
def update_plan(request:Request,id:UUID,body:dict=Body(...)):return ok(ctx(request).plans.update(str(id),body,require_permission(request,'sku.update')))
@router.post('/api/v1/admin/plans/{id}/enable')
def enable_plan(request:Request,id:UUID):return ok(ctx(request).plans.status(str(id),True,require_permission(request,'sku.update')))
@router.post('/api/v1/admin/plans/{id}/disable')
def disable_plan(request:Request,id:UUID):return ok(ctx(request).plans.status(str(id),False,require_permission(request,'sku.update')))
@router.delete('/api/v1/admin/plans/{id}')
def delete_plan(request:Request,id:UUID):ctx(request).plans.delete(str(id),require_permission(request,'sku.delete'));return ok(None)

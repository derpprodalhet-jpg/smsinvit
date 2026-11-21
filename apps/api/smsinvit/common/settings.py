from __future__ import annotations
import re
from uuid import UUID,uuid4
from urllib.parse import urlsplit
from fastapi import APIRouter,Request,Body,Query
from .mapper import Mapper
from .catalog import parse_json,json_text,require_map
from .auth import bounded
from ..config import RUNTIME_CATALOG,RUNTIME_BY_KEY,validate_runtime
from ..errors import Error,ok,bad
from ..security import ctx,require_permission

router=APIRouter()
DEFAULTS={'site.name':{'name':'SmsInvit 兑换服务'},'site.description':{'description':''},'site.support_email':{'email':''},'notify.telegram_cs_link':{'value':''}}

def validate_link(raw):
    if not raw:return
    u=urlsplit(raw)
    if len(raw)>2048 or u.scheme not in ('https','http','mailto','tg') or u.scheme in ('http','https') and (not u.hostname or u.username) or any(c in raw for c in '\r\n\0'):raise bad('链接格式无效。')
def validate_links(value,depth=0):
    if depth>10:raise bad('设置嵌套过深。')
    if isinstance(value,dict):
        for key,item in value.items():
            if key in ('url','href','link','telegram') and isinstance(item,str):validate_link(item)
            validate_links(item,depth+1)
    elif isinstance(value,list):
        for item in value:validate_links(item,depth+1)

class Settings:
    def __init__(self,services):self.s=services;self.db=services.db;self.mapper=Mapper(self.db,'SettingsMapper')
    def list(self,category=None):
        rows={}
        for key,value in DEFAULTS.items():rows[key]={'id':None,'key':key,'category':key.split('.')[0],'valueJson':value,'visibility':'PUBLIC','isSecret':False}
        for row in self.mapper.listAll():
            if row['key'] in DEFAULTS:
                row=dict(row);row['isSecret']=bool(row['isSecret']);row['valueJson']={'value':'********'} if row['isSecret'] else parse_json(row['valueJson']);rows[row['key']]=row
        for spec in RUNTIME_CATALOG:
            # 运行时目录里已没有密钥型配置（历史遗留的 Turnstile 三项已删除）。
            key=spec['key'];secret=False;value=self.s.config.get(key,spec['initialValue'])
            if spec['type']=='BOOLEAN':value=self.s.config.flag(key)
            elif spec['type']=='INTEGER':value=self.s.config.int(key)
            if secret:value='********' if value else ''
            rows[key]={'id':None,'key':key,'category':spec['category'],'valueJson':{'value':value},'visibility':'PRIVATE','isSecret':secret,'label':spec['label'],'valueType':spec['type'],'requiresRestart':False,'initialValue':spec['initialValue'],'source':'DATABASE' if key in self.s.config.runtime else 'ENVIRONMENT' if self.s.config.get(spec['property'])!=spec['initialValue'] else 'DEFAULT'}
        for row in self.s.notifications.admin_values():rows[row['key']]=row
        return [rows[key] for key in sorted(rows) if not category or category==rows[key]['category']]
    def save(self,actor,body):
        key=bounded(body.get('key'),128,True).strip();category=bounded(body.get('category'),64);visibility=bounded(body.get('visibility'),32);value=require_map(body.get('valueJson'))
        if self.s.notifications.supports(key):
            if category not in (None,'notify') or visibility not in (None,'PRIVATE'):raise bad('通知通道配置必须为私有设置。')
            return self.s.notifications.save(key,value,actor)
        if key in RUNTIME_BY_KEY:
            spec=RUNTIME_BY_KEY[key]
            if body.get('isSecret'):raise bad('运行时开关均为非密钥配置，密钥请使用对应通道设置。')
            if category not in (None,spec['category'],'runtime') or visibility not in (None,'PRIVATE'):raise bad('设置分类与键不匹配或不是私有设置。')
            raw=value.get('value')
            if not isinstance(raw,(str,int,float,bool)):raise bad('运行时配置必须提供 value 字段。')
            plain=('true' if raw else 'false') if isinstance(raw,bool) else str(raw).strip()
            try:validate_runtime(key,plain)
            except (ValueError,KeyError):raise bad('配置值不合法，请检查取值格式。')
            # 运行时目录里已没有密钥型配置（第 37-39 行），所以这里固定走明文运行时写入路径。
            serialized=json_text({'value':plain})
            with self.db.advisory_lock('settings.write',10) as acquired:
                if not acquired:raise Error('CONFLICT','设置更新繁忙，请稍后重试。',409)
                self.mapper.upsertRuntime(str(uuid4()),key,spec['category'],serialized,actor.user_id);self.s.audit.record(actor.user_id,'setting.update','setting',key,{'key':key,'runtime':True,'requiresRestart':False})
            self.s.config.runtime[key]=plain
            return next(r for r in self.list() if r['key']==key)
        if key not in DEFAULTS:raise bad('该配置尚未接入此接口，请使用对应专用设置页面。')
        if body.get('isSecret'):raise bad('此接口不接收密钥配置。')
        if category not in (None,key.split('.')[0]):raise bad('设置分类与键不匹配。')
        visibility=visibility or 'PUBLIC'
        if visibility not in ('PUBLIC','PRIVATE'):raise bad('此配置不能公开。')
        validate_links(value)
        if key=='site.name':bounded(value.get('name'),100,True)
        if key=='site.description':
            desc=bounded(value.get('description'),300,True if value.get('description') is None else False)
            if '<' in desc or '>' in desc:raise bad('站点描述不能包含 HTML 标签。')
        if key=='site.support_email':
            if any(not isinstance(v,str) for v in value.values()):raise bad('联系方式必须为文本。')
            if value.get('email') and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value['email']):raise bad('邮箱格式错误。')
        if key=='notify.telegram_cs_link':validate_link(bounded(value.get('value'),2048,True if value.get('value') is None else False))
        with self.db.advisory_lock('settings.write',10) as acquired:
            if not acquired:raise Error('CONFLICT','设置更新繁忙，请稍后重试。',409)
            if self.mapper.existsSecret(key):raise bad('不能通过普通设置覆盖历史密钥配置。')
            self.mapper.upsert(str(uuid4()),key,key.split('.')[0],json_text(value),visibility,actor.user_id);self.s.audit.record(actor.user_id,'setting.update','setting',key,{'key':key,'visibility':visibility})
        return next(row for row in self.list() if row['key']==key)
    def remove(self,actor,id):
        with self.db.advisory_lock('settings.write',10) as acquired:
            if not acquired:raise Error('CONFLICT','设置更新繁忙，请稍后重试。',409)
            row=self.mapper.findByIdForUpdate(id)
            if not row:raise Error('SETTING_NOT_FOUND','配置不存在。',404)
            key=row['key']
            if row['isSecret'] or key not in DEFAULTS and key not in RUNTIME_BY_KEY:raise bad('该配置不能通过普通设置删除。')
            self.mapper.deleteByIdExact(id);self.s.audit.record(actor.user_id,'setting.delete','setting',key,{'key':key})
        self.s.config.runtime.pop(key,None);return {'deleted':True}

@router.get('/api/v1/admin/settings')
def list_settings(request:Request,category:str|None=Query(None,max_length=64)):require_permission(request,'settings.read');return ok(ctx(request).settings.list(category))
@router.post('/api/v1/admin/settings')
def save_setting(request:Request,body:dict=Body(...)):return ok(ctx(request).settings.save(require_permission(request,'settings.update'),body))
@router.delete('/api/v1/admin/settings/{id}')
def delete_setting(request:Request,id:UUID):return ok(ctx(request).settings.remove(require_permission(request,'settings.update'),str(id)))

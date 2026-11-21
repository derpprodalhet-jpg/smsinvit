"""Compatibility utilities shared by the two independently configured services."""
from __future__ import annotations
import json, re, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from smsinvit.errors import Error


def now(): return datetime.now(timezone.utc).replace(tzinfo=None)
def instant(v):
    if v is None: return None
    if isinstance(v,datetime): return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v
    try: return datetime.fromisoformat(str(v).replace('Z','+00:00')).replace(tzinfo=None)
    except (ValueError,TypeError): return None
def text(v): return '' if v is None else str(v).strip()
def db_true(v):
    """Mirror com.smsinvit.common.DbBool.isTrue for MySQL 0/1 and bit columns."""
    if isinstance(v,bool): return v
    if isinstance(v,(int,float,Decimal)): return v!=0
    return False
def flag_true(v,default):
    """Mirror ZovoCardSettings.boolVal: booleans and 0/1 win, "true"/"false" ignore case, else default."""
    if isinstance(v,bool): return v
    if isinstance(v,(int,float,Decimal)): return v!=0
    if v is None: return default
    s=text(v).lower()
    if s=='true': return True
    if s=='false': return False
    return default
def parse_json(v,default=None):
    if isinstance(v,(dict,list)): return v
    try: return json.loads(v)
    except (TypeError,ValueError): return default

def dumps(v): return json.dumps(v,ensure_ascii=False,default=str,separators=(',',':'))
def uid(): return str(uuid.uuid4())
def mask(v):
    s=text(v)
    return None if not s else '****' if len(s)<=4 else s[:2]+'****'+s[-2:]
def project(row,names): return {k:row.get(k) for k in names if row.get(k) is not None}

class Mapper:
    """Native SQL definitions, preserving the existing MySQL tables and guarded updates."""
    def __init__(self,db,path):
        self.db=db; self.queries=json.loads(Path(path).read_text())
    def __getattr__(self,name):
        if name not in self.queries: raise AttributeError(name)
        q=self.queries[name]
        def call(*args,**kw):
            values=dict(zip(q['params'],args)); values.update(kw)
            if len(values)!=len(q['params']): raise TypeError(f'{name} expects {q["params"]}, got {list(values)}')
            sql=q['sql'].replace('<script>','').replace('</script>','')
            def condition(m):
                fields=re.findall(r'(\w+)\s*!=',m[1]); return m[2] if all(values.get(f) is not None and values.get(f)!='' for f in fields) else ''
            sql=re.sub(r'<if\s+test="([^"]+)">(.*?)</if>',condition,sql,flags=re.S)
            params=[]
            def bind(m): params.append(values[m[1]]); return '?'
            sql=re.sub(r'#\{(\w+)\}',bind,sql)
            if '<' in sql and re.search(r'<(?:if|where|choose|foreach)',sql): raise ValueError('Unsupported SQL construct')
            shape=q['shape']
            if shape=='execute': return self.db.execute(sql,*params).rowcount
            if shape in ('all','column'):
                rows=self.db.all(sql,*params)
                return rows if shape=='all' else [next(iter(r.values())) for r in rows]
            row=self.db.one(sql,*params)
            return row if shape=='one' else next(iter(row.values())) if row else None
        return call

class BusinessSettings:
    def __init__(self,db,codec,config,key,defaults_path):
        self.db=db; self.codec=codec; self.config=config; self.key=key
        self.defaults=json.loads(Path(defaults_path).read_text())
    def load(self):
        row=self.db.one('SELECT CAST(value_json AS CHAR) AS valueJson FROM settings WHERE `key`=?',self.key)
        value=self.defaults | (parse_json(row.get('valueJson'),{}) if row else {})
        if value.get('webhookTokenEncrypted'):
            try:value['webhookToken']=self.codec.decrypt(value['webhookTokenEncrypted'])
            except Exception:value['webhookToken']=''
        return value
    def save(self,v):
        v=dict(v)
        if text(v.get('webhookToken')):v['webhookTokenEncrypted']=self.codec.encrypt(v.pop('webhookToken'))
        self.db.execute("INSERT INTO settings(id,`key`,category,value_json,visibility,is_secret,updated_at) VALUES(?,?,?,?, 'PRIVATE',true,NOW(3)) ON DUPLICATE KEY UPDATE value_json=VALUES(value_json),visibility='PRIVATE',is_secret=true,updated_at=NOW(3)",uid(),self.key,self.key.split('.')[0],dumps(v))
    def get(self,k,default=None):
        v=self.load().get(k,default)
        fallback=self.defaults.get(k)
        return fallback if isinstance(v,str) and not v.strip() and isinstance(fallback,str) and fallback else v
    def secret(self,k='apiKey'):
        value=self.load(); enc=value.get(k+'Encrypted')
        if enc:
            try: return self.codec.decrypt(enc)
            except Exception: return ''
        return text(value.get(k))
    def set_secret(self,v,k,new):
        if not new or '*' in new: return
        v[k+'Encrypted']=self.codec.encrypt(new.strip()); v.pop(k,None)
    def public_base(self):
        row=self.db.one("SELECT CAST(value_json AS CHAR) AS v FROM settings WHERE `key`='runtime.public_api_base'")
        val=parse_json(row['v']) if row else None
        return text((val.get('value') if isinstance(val,dict) else val) if row else self.config.get('runtime.public_api_base',self.config.get('PUBLIC_API_BASE',''))).rstrip('/')
    def save_body(self,body,forbidden=None):
        if forbidden and body.get(forbidden) is not None: raise Error('VALIDATION_ERROR','公网 API 基址请到「系统管理 → 运行时配置」中修改。',422)
        v=self.load()
        for k,value in body.items():
            if value is None or k.startswith('clear') or k=='rotateWebhookToken' or k in ('apiKey','webhookSecret','generalWebhookSecret','numlookupApiKey','abstractApiKey'):continue
            v[k]=value.strip() if isinstance(value,str) else value
        for k in ('apiKey','webhookSecret','generalWebhookSecret','numlookupApiKey','abstractApiKey'):
            if body.get('clear'+k[0].upper()+k[1:]): v.pop(k,None);v.pop(k+'Encrypted',None)
            elif body.get(k):self.set_secret(v,k,body[k])
        # Upgrade old plaintext secrets on any save; blank edits preserve secrets.
        for k in ('apiKey','webhookSecret','generalWebhookSecret','numlookupApiKey','abstractApiKey'):
            if v.get(k) and not v.get(k+'Encrypted'): self.set_secret(v,k,v[k])
            v.pop(k,None)
        if body.get('rotateWebhookToken') or not v.get('webhookToken'):
            import secrets
            v['webhookToken']=secrets.token_hex(24)
        self.save(v); return v
    def runtime_value(self,k,default):
        row=self.db.one('SELECT CAST(value_json AS CHAR) AS v FROM settings WHERE `key`=?','runtime.'+k)
        value=parse_json(row['v'],default) if row else self.config.get('runtime.'+k,self.config.get(k.upper(),default))
        return value.get('value',default) if isinstance(value,dict) else value
    def runtime_int(self,k,default):
        try:return int(self.runtime_value(k,default))
        except (TypeError,ValueError):return default
    def runtime_float(self,k,default):
        import math
        try:
            v=float(self.runtime_value(k,default))
            return v if math.isfinite(v) and v>0 else default
        except (TypeError,ValueError):return default


def business_window(config):
    from zoneinfo import ZoneInfo
    local=datetime.now(ZoneInfo(config.get('runtime.business_time_zone','Asia/Shanghai')))
    midnight=local.replace(hour=0,minute=0,second=0,microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None),(midnight+timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)

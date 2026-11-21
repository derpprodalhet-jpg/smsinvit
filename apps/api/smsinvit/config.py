"""Deployment settings with narrowly scoped database-backed runtime overrides."""
from __future__ import annotations
import json
import os
from pathlib import Path
from importlib.resources import files
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import re
import yaml

RUNTIME_CATALOG=json.loads(files('smsinvit.common').joinpath('runtime_catalog.json').read_text())
RUNTIME_BY_KEY={item['key']:item for item in RUNTIME_CATALOG}
RUNTIME_BY_PROPERTY={item['property']:item for item in RUNTIME_CATALOG}


def normalize_cors_origin(value):
    """Use browser origin syntax for both stored settings and comparisons."""
    raw=str(value).strip()
    if not raw or re.search(r'[\s\\]',raw):raise ValueError('Explicit CORS origins required')
    u=urlsplit(raw)
    if u.scheme not in ('http','https') or not u.hostname or u.username is not None or u.password is not None or u.path not in ('','/') or u.query or u.fragment:
        raise ValueError('Explicit CORS origins required')
    host=u.hostname.encode('idna').decode('ascii').lower()
    if ':' in host:
        import ipaddress
        ipaddress.IPv6Address(host)
        host='['+host+']'
    elif not re.fullmatch(r'[a-z0-9.-]+',host):raise ValueError('Explicit CORS origins required')
    port=u.port
    if port is not None and not 1<=port<=65535:raise ValueError('Explicit CORS origins required')
    suffix='' if port is None or port=={'http':80,'https':443}[u.scheme] else ':'+str(port)
    return u.scheme+'://'+host+suffix


def normalize_cors_origins(value):
    if not str(value).strip():return []
    return list(dict.fromkeys(normalize_cors_origin(origin) for origin in str(value).split(',')))

class Config:
    def __init__(self,values=None,*,environ=None,path=None):
        self.values={}; self.environ=dict(os.environ if environ is None else environ);self.runtime={};self.sources={}
        if path:
            loaded=yaml.safe_load(Path(path).read_text()) or {}
            def flatten(value,prefix=''):
                for k,v in value.items():
                    key=f'{prefix}.{k}' if prefix else str(k)
                    if isinstance(v,dict):flatten(v,key)
                    else:self.values[key]=v
            flatten(loaded)
        self.values.update(values or {})
    def get(self,key,default=None):
        aliases={'inventory_encryption_key':'smsinvit.inventory-encryption-key','inventory_fingerprint_key':'smsinvit.inventory-fingerprint-key','jwt_secret':'smsinvit.jwt-secret','database_url':'DATABASE_URL'}
        key=aliases.get(key,key)
        spec=RUNTIME_BY_KEY.get(key) or RUNTIME_BY_PROPERTY.get(key)
        if spec and spec['key'] in self.runtime:return self.runtime[spec['key']]
        effective_key=spec['property'] if spec else key
        env_key=effective_key.replace('smsinvit.','').replace('-','_').replace('.','_').upper()
        aliases_env={'WORKER_ENABLED':'SMSINVIT_WORKER_ENABLED','INVENTORY_ENCRYPTION_KEY':'INVENTORY_ENCRYPTION_KEY','INVENTORY_FINGERPRINT_KEY':'INVENTORY_FINGERPRINT_KEY','JWT_SECRET':'JWT_SECRET'}
        candidates=[key,effective_key,aliases_env.get(env_key,env_key),'SMSINVIT_'+env_key]
        for candidate in candidates:
            if candidate in self.environ:return self.environ[candidate]
        value_key=key if key in self.values else effective_key
        if value_key in self.values:
            value=self.values[value_key]
            if isinstance(value,str):
                m=re.fullmatch(r'\$\{([^}:]+)(?::(.*))?\}',value)
                if m:return self.environ.get(m[1],m[2] if m[2] is not None else default)
            return value
        return spec['initialValue'] if spec else default
    def flag(self,key,default=False):
        value=self.get(key,default)
        if isinstance(value,bool):return value
        if str(value).lower() not in ('true','false','1','0','yes','no','on','off'):raise ValueError(f'Invalid boolean setting {key}')
        return str(value).lower() in ('true','1','yes','on')
    def int(self,key,default=0):return int(self.get(key,default))
    def require(self,key):
        value=self.get(key)
        if value is None or not str(value).strip():raise ValueError(f'Required deployment setting {key} is missing')
        return value
    def load_runtime(self,db,codec):
        found={}
        for row in db.all("SELECT `key`,CAST(value_json AS CHAR) AS value_json,is_secret FROM settings WHERE `key` LIKE 'runtime.%'"):
            key=row['key']
            if key not in RUNTIME_BY_KEY:continue
            value=json.loads(row['value_json'])['value']
            if row['is_secret']:value=codec.decrypt(str(value))
            validate_runtime(key,value);found[key]=value
        self.runtime=found
    @classmethod
    def from_env(cls):return cls(path=os.environ.get('SMSINVIT_CONFIG_FILE'))
    def database_url(self):
        from sqlalchemy import URL
        raw=self.get('DATABASE_URL')
        if raw:return raw
        return URL.create('mysql+pymysql',username=self.get('MYSQL_USER',self.get('DB_USER','smsinvit')),password=self.get('MYSQL_PASSWORD',self.get('DB_PASSWORD','')),host=self.get('MYSQL_HOST',self.get('DB_HOST','127.0.0.1')),port=self.int('MYSQL_PORT',self.int('DB_PORT',3306)),database=self.get('MYSQL_DATABASE',self.get('DB_NAME','smsinvit')),query={'charset':'utf8mb4'})
    def database_connect_args(self):
        """A deployment policy, never a runtime setting editable by staff."""
        import ipaddress
        import ssl
        from sqlalchemy.engine import make_url
        url=make_url(self.database_url())
        if url.get_backend_name()!='mysql':raise ValueError('The API requires MySQL')
        # Reject URL parameters which could silently override the verified context.
        if any(key.lower().startswith('ssl') for key in url.query):
            raise ValueError('Configure database TLS with MYSQL_TLS_MODE and MYSQL_SSL_CA')
        mode=str(self.get('MYSQL_TLS_MODE','VERIFY_IDENTITY')).upper()
        options={'connect_timeout':10,'read_timeout':60,'write_timeout':60}
        if mode=='DISABLED':
            host=url.host or ''
            try:loopback=ipaddress.ip_address(host).is_loopback
            except ValueError:loopback=host=='localhost'
            if not loopback:raise ValueError('Database TLS may be disabled only for explicit loopback tests')
            return options
        if mode!='VERIFY_IDENTITY':raise ValueError('Database TLS must verify certificates and hostnames')
        ca=Path(str(self.require('MYSQL_SSL_CA'))).expanduser()
        if not ca.is_file():raise ValueError('Database trusted CA file is unavailable')
        context=ssl.create_default_context(cafile=str(ca));context.check_hostname=True;context.verify_mode=ssl.CERT_REQUIRED;context.minimum_version=ssl.TLSVersion.TLSv1_2
        cert=self.get('MYSQL_SSL_CERT');key=self.get('MYSQL_SSL_KEY')
        if bool(cert)!=bool(key):raise ValueError('Database client certificate and key must be configured together')
        if cert:context.load_cert_chain(str(cert),str(key))
        options['ssl']=context
        return options

def validate_runtime(key,value):
    spec=RUNTIME_BY_KEY[key];raw=str(value).strip()
    if spec['type']=='BOOLEAN':
        if raw.lower() not in ('true','false'):raise ValueError('Boolean expected')
    elif spec['type']=='INTEGER':
        number=int(raw)
        if str(number)!=raw or spec['min'] is not None and number<spec['min'] or spec['max'] is not None and number>spec['max']:raise ValueError('Integer out of bounds')
    elif not raw and not spec['allowBlank']:raise ValueError('Blank value forbidden')
    if any(c in raw for c in ('\n','\r','\0')) or len(raw)>4096:raise ValueError('Invalid text')
    if key=='runtime.business_time_zone':ZoneInfo(raw)
    if key=='runtime.public_api_base' and raw:
        u=urlsplit(raw)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.query or u.fragment:raise ValueError('Invalid origin')
    if key=='runtime.cors_origins':
        normalize_cors_origins(raw)
    if key=='runtime.sms_usd_cny_rate_fallback':
        from decimal import Decimal
        if not Decimal(raw).is_finite() or Decimal(raw)<=0:raise ValueError('Positive finite rate required')

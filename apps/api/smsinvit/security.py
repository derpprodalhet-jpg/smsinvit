"""Authentication, capabilities, trusted ingress and deployment-owned egress."""
from __future__ import annotations
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import struct
import time
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
import jwt
from .errors import Error

ECMASCRIPT_SPACE='\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
def normalize_cdk(value): return str(value or '').strip(ECMASCRIPT_SPACE)
def b64url(value):return base64.urlsafe_b64encode(value).decode().rstrip('=')
def unb64url(value):return base64.urlsafe_b64decode(value+'='*(-len(value)%4))

class SecretCodec:
    def __init__(self,encryption_key,fingerprint_key=None):
        if fingerprint_key is None and hasattr(encryption_key,'require'):
            config=encryption_key;encryption_key=config.require('smsinvit.inventory-encryption-key');fingerprint_key=config.require('smsinvit.inventory-fingerprint-key')
        if not re.fullmatch(r'[0-9a-fA-F]{64}',str(encryption_key)):raise ValueError('INVENTORY_ENCRYPTION_KEY requires 32-byte hex key')
        if not fingerprint_key:raise ValueError('INVENTORY_FINGERPRINT_KEY is required')
        self.key=bytes.fromhex(encryption_key);self.fingerprint_key=str(fingerprint_key).encode()
        self.passwords=PasswordHasher(time_cost=3,memory_cost=65536,parallelism=1,hash_len=32,salt_len=16)
    def encrypt(self,plaintext):
        iv=secrets.token_bytes(12);sealed=AESGCM(self.key).encrypt(iv,str(plaintext).encode(),None)
        return '.'.join(base64.b64encode(part).decode() for part in (iv,sealed[-16:],sealed[:-16]))
    def decrypt(self,payload):
        try:
            iv,tag,data=(base64.b64decode(part,validate=True) for part in payload.split('.'))
            if len(iv)!=12 or len(tag)!=16:raise ValueError()
            return AESGCM(self.key).decrypt(iv,data+tag,None).decode()
        except Exception as exc:raise ValueError('Invalid encrypted payload') from exc
    def fingerprint(self,value):return hmac.new(self.fingerprint_key,normalize_cdk(value).encode(),hashlib.sha256).hexdigest()
    digest=fingerprint
    hmac=fingerprint
    def sha256(self,value):return hashlib.sha256(str(value).encode()).hexdigest()
    def random_token(self,n):return secrets.token_hex(n)
    randomToken=random_token
    def hash_password(self,password):return self.passwords.hash(password)
    hashPassword=hash_password
    def verify_password(self,encoded,password):
        try:return self.passwords.verify(encoded,password)
        except (Argon2Error,ValueError,TypeError):return False
    verifyPassword=verify_password

@dataclass(frozen=True)
class Principal:
    user_id: str
    session_id: str
    roles: tuple[str,...]
    totp_setup_required: bool=False
    @property
    def id(self):return self.user_id
    @property
    def userId(self):return self.user_id
    @property
    def sessionId(self):return self.session_id
    @property
    def totpSetupRequired(self):return self.totp_setup_required

class TokenService:
    SCOPE_SMS='sms-session';SCOPE_INVITATION='invocation-session'
    def __init__(self,secret):
        if hasattr(secret,'require'):secret=secret.require('smsinvit.jwt-secret')
        self.key=str(secret).encode()
        if len(self.key)<32:raise ValueError('JWT_SECRET requires at least 32 bytes')
    def sign(self,user_id,email,username,roles,session_id,audience='admin'):
        now=int(time.time());return jwt.encode({'sub':str(user_id),'iat':now,'exp':now+900,'email':email,'username':username,'roles':list(roles),'sessionId':str(session_id),'aud':audience},self.key,algorithm='HS256')
    def verify(self,token,audience='admin'):
        try:
            claims=jwt.decode(token,self.key,algorithms=['HS256'],audience=audience,options={'require':['exp','sub','sessionId'],'verify_iat':False})
            UUID(claims['sub']);UUID(claims['sessionId']);return claims
        except (jwt.PyJWTError,ValueError,TypeError,KeyError):raise Error('AUTH_INVALID_TOKEN','访问凭证无效或已过期，请重新登录。',401)
    def issue(self,scope,session_id):
        if session_id is None:raise Error('INTERNAL_ERROR','会话标识缺失。',500)
        return hmac.new(self.key,f'{scope}:{session_id}'.encode(),hashlib.sha256).hexdigest()
    capability=issue
    def require(self,scope,session_id,presented):
        if not presented or not str(presented).strip():raise Error('SESSION_SECRET_REQUIRED','缺少会话密钥，请重新兑换。',401)
        if not hmac.compare_digest(self.issue(scope,session_id),str(presented).strip()):raise Error('SESSION_SECRET_INVALID','会话密钥无效，请重新兑换。',401)
    require_capability=require

class PermissionService:
    def __init__(self,db):self.db=db
    def _principal(self,actor):
        return getattr(actor.state,'principal',None) if hasattr(actor,'state') else actor
    def allows(self,actor,code,owner=None,board=None):
        actor=self._principal(actor)
        if actor is None or actor.totp_setup_required:return False
        if 'SUPER_ADMIN' in actor.roles:return True
        grants=self.db.all('SELECT rp.scope_type AS permission_scope FROM admin_user_roles aur JOIN role_permissions rp ON rp.role_id=aur.role_id JOIN permissions p ON p.id=rp.permission_id WHERE aur.admin_user_id=? AND p.code=?',actor.user_id,code)
        return any((g['permission_scope'] or 'ALL')=='ALL' or g['permission_scope']=='OWN' and str(owner)==actor.user_id for g in grants)
    def require(self,actor,code,owner=None,board=None):
        actor=self._principal(actor)
        if actor is None:raise Error('AUTH_REQUIRED','请先登录。',401)
        if actor.totp_setup_required:raise Error('AUTH_TOTP_REQUIRED','请先完成双重验证设置。',403)
        if not self.allows(actor,code,owner,board):raise Error('PERMISSION_DENIED','没有操作权限。',403)
        return actor

def ctx(request):return request.app.state.services
def require_permission(request,code,owner=None,board=None):return ctx(request).permissions.require(request,code,owner,board)
def principal(request):
    actor=getattr(request.state,'principal',None)
    if actor is None:raise Error('AUTH_REQUIRED','请先登录。',401)
    return actor

def totp_secret():return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
def totp_code(secret,instant=None):
    key=base64.b32decode(secret.upper()+'='*(-len(secret)%8),casefold=True)
    msg=struct.pack('>Q',int(time.time() if instant is None else instant)//30)
    raw=hmac.new(key,msg,hashlib.sha1).digest();offset=raw[-1]&15
    number=struct.unpack('>I',raw[offset:offset+4])[0]&0x7fffffff
    return f'{number%1000000:06d}'
def totp_verify(secret,submitted,instant=None,steps=1):
    token=re.sub(r'\s','',str(submitted or ''))
    if not re.fullmatch(r'[0-9]{6}',token):return False
    # 必须容忍相邻窗口。只比对当前 30 秒窗口时，手机时钟与服务器差几秒、或用户在
    # 30 秒边界上输码，正确的验证码也会被判错——用户看到的就是「验证码明明对却报错」。
    # RFC 6238 §6 也要求验证方接受一定的时间漂移。
    base=int(time.time() if instant is None else instant)
    try:
        for offset in range(-steps,steps+1):
            window=base+offset*30
            # 负时间戳会让 struct.pack('>Q') 抛错；夹具可以传很小的 instant，这里跳过。
            if window<0:continue
            if hmac.compare_digest(totp_code(secret,window),token):return True
    except (ValueError,TypeError,struct.error):return False
    return False

class TrustedProxyResolver:
    HEADERS=('forwarded','x-forwarded-for','x-real-ip','x-forwarded-proto','x-forwarded-host','x-forwarded-port')
    def __init__(self,raw='127.0.0.0/8,::1/128'):
        self.networks=[ipaddress.ip_network(v.strip(),strict=False) for v in raw.split(',') if v.strip()]
        if any(n.prefixlen==0 for n in self.networks):raise ValueError('Trusted proxies cannot be a wildcard CIDR')
    def trusted(self,value):return any(value.version==n.version and value in n for n in self.networks)
    @staticmethod
    def denied():return Error('INVALID_PROXY_HEADERS','代理请求头无效，请通过受信任的入口访问。',400)
    def address(self,value):
        # Zone IDs are local interfaces, not transferable client addresses.
        if not value or '%' in value:raise self.denied()
        try:return ipaddress.ip_address(value)
        except ValueError:raise self.denied()
    def port(self,value):
        if not re.fullmatch('[0-9]{1,5}',value) or not 1<=int(value)<=65535:raise self.denied()
    def authority(self,value):
        if value.startswith('['):
            match=re.fullmatch(r'\[([0-9A-Fa-f:.]+)\](?::([0-9]{1,5}))?',value)
            if not match or self.address(match[1]).version!=6:raise self.denied()
            if match[2]:self.port(match[2])
            return
        host,separator,port=value.partition(':')
        if separator:self.port(port)
        if len(host)>253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',label) for label in host.split('.')):raise self.denied()
    def node(self,value):
        if value.startswith('['):
            match=re.fullmatch(r'\[([0-9A-Fa-f:.]+)\](?::([0-9]{1,5}))?',value)
            if not match or self.address(match[1]).version!=6:raise self.denied()
            if match[2]:self.port(match[2])
            return
        if value.count(':')==1:
            host,port=value.split(':');self.port(port)
            if self.address(host).version!=4:raise self.denied()
        else:self.address(value)
    def forwarded(self,value):
        # Only concrete, bounded address/host/protocol fields are supported.
        # This header never overrides the existing XFF/X-Real-IP client rule.
        elements=value.split(',')
        if len(elements)>32:raise self.denied()
        for element in elements:
            seen=set()
            for part in element.split(';'):
                key,separator,item=part.strip().partition('=');key=key.lower()
                if not separator or key not in ('for','by','host','proto') or key in seen:raise self.denied()
                seen.add(key)
                if item.startswith('"') and item.endswith('"'):item=item[1:-1]
                if not item or any(c in item for c in ('"','\\',' ')):raise self.denied()
                if key in ('for','by'):self.node(item)
                elif key=='host':self.authority(item)
                elif item not in ('http','https'):raise self.denied()
    def resolve(self,request):
        raw=request.client.host if request.client else 'unknown'
        present={name:request.headers.getlist(name) for name in self.HEADERS if name in request.headers}
        try:peer=ipaddress.ip_address(raw) if '%' not in raw else None
        except ValueError:peer=None
        if not present:return str(peer) if peer else 'unknown'
        if peer is None or not self.trusted(peer):raise self.denied()
        headers={}
        for name,values in present.items():
            if len(values)!=1:raise self.denied()
            value=values[0].strip(' ')
            if not value or len(value)>2048 or any(ord(c)<32 or ord(c)>126 for c in value):raise self.denied()
            headers[name]=value
        if 'forwarded' in headers:self.forwarded(headers['forwarded'])
        if 'x-forwarded-proto' in headers and headers['x-forwarded-proto'] not in ('http','https'):raise self.denied()
        if 'x-forwarded-host' in headers:self.authority(headers['x-forwarded-host'])
        if 'x-forwarded-port' in headers:self.port(headers['x-forwarded-port'])
        real=self.address(headers['x-real-ip']) if 'x-real-ip' in headers else None
        if 'x-forwarded-for' in headers:
            hops=headers['x-forwarded-for'].split(',')
            if len(hops)>32:raise self.denied()
            addresses=[self.address(h.strip(' ')) for h in hops]
            result=raw
            for value in reversed(addresses):
                if not self.trusted(peer):break
                peer=value;result=str(value)
            return result
        return str(real) if real else str(peer)

def client_ip(request):return getattr(request.state,'client_ip',request.client.host if request.client else 'unknown')

class SupplierEndpointPolicy:
    # 官方供应商端点：SMS 接码用到的三家上游，生产与沙盒都要在。以前只放了沙盒，
    # 于是把「上游环境」切到生产后保存配置就报「供应商地址未获部署配置授权」——
    # 设置页的下拉本来就把生产列为可选项，白名单必须跟上。
    OFFICIAL={'smsbower.page:443','api.numlookupapi.com:443','phonevalidation.abstractapi.com:443'}
    LOCAL={'localhost','127.0.0.1','::1','host.docker.internal'}
    def __init__(self,config,resolver=None):
        self.config=config;self.resolve=resolver or (lambda host: {item[4][0] for item in socket.getaddrinfo(host,None)})
        self.hosts=set(self.OFFICIAL)
        for entry in str(config.get('SMSINVIT_SUPPLIER_ALLOWED_HOSTS','')).split(','):
            if not entry.strip():continue
            u=self.parse('https://'+entry.strip())
            if u.path or u.port is None:raise ValueError('Supplier allowlist must be exact host:port')
            self.hosts.add(self.authority(u))
        profile=str(config.get('SMSINVIT_ENV',config.get('SPRING_PROFILES_ACTIVE','prod')))
        self.mocks_allowed=profile in ('test','dev')
        self.mocks=set()
        for entry in str(config.get('SMSINVIT_SUPPLIER_TEST_URLS','')).split(','):
            if not entry.strip():continue
            u=self.parse(entry.strip())
            if u.hostname not in self.LOCAL or u.port is None:raise ValueError('Test URL must name explicit localhost port')
            self.mocks.add(self.normalized(u))
    @staticmethod
    def denied():return Error('SUPPLIER_ENDPOINT_FORBIDDEN','供应商地址未获部署配置授权，请联系管理员检查地址白名单。',422)
    def parse(self,url,allow_query=False):
        try:
            u=urlsplit(str(url).strip());port=u.port
            if u.scheme not in ('https','http') or not u.hostname or u.username or u.password or u.fragment or (u.query and not allow_query) or port==0 or u.hostname.endswith('.'):raise self.denied()
            return u
        except ValueError:raise self.denied()
    @staticmethod
    def authority(u):return f'{u.hostname.lower()}:{u.port or 443}'
    def normalized(self,u):return f'{u.scheme.lower()}://{self.authority(u)}{u.path.rstrip("/")}'
    def require_allowed(self,url,allow_query=False):
        u=self.parse(url,allow_query)
        if self.mocks_allowed and self.normalized(u) in self.mocks:return None
        # Test URLs authorize their explicit origin + configured base path only.
        if self.mocks_allowed and any(self.normalized(u).startswith(base+'/') for base in self.mocks):return None
        if u.scheme!='https' or self.authority(u) not in self.hosts:raise self.denied()
        try:
            addresses=self.resolve(u.hostname)
            if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast for a in addresses):raise self.denied()
            return tuple(sorted(addresses))
        except (OSError,ValueError):raise self.denied()
    requireAllowed=require_allowed

_DNS_BUDGET_SLOTS=threading.BoundedSemaphore(8)

def _before_deadline(deadline):
    if deadline is not None and time.monotonic()>=deadline:
        raise Error('SUPPLIER_QUERY_TIMEOUT','查询暂时不可用，请稍后重试。',504)

def _bounded_supplier_policy(policy,url,deadline):
    if deadline is None:return policy.require_allowed(url,allow_query=True)
    _before_deadline(deadline)
    # DNS has no socket timeout. Bound its isolated worker count and caller wait;
    # a late DNS completion cannot issue any HTTP request or update a session.
    if not _DNS_BUDGET_SLOTS.acquire(timeout=max(0,deadline-time.monotonic())):
        raise Error('SUPPLIER_QUERY_TIMEOUT','查询暂时不可用，请稍后重试。',504)
    finished=threading.Event();outcome={}
    def resolve():
        try:outcome['value']=policy.require_allowed(url,allow_query=True)
        except BaseException as exc:outcome['error']=exc
        finally:_DNS_BUDGET_SLOTS.release();finished.set()
    try:threading.Thread(target=resolve,daemon=True).start()
    except BaseException:_DNS_BUDGET_SLOTS.release();raise
    if not finished.wait(max(0,deadline-time.monotonic())):
        raise Error('SUPPLIER_QUERY_TIMEOUT','查询暂时不可用，请稍后重试。',504)
    _before_deadline(deadline)
    if 'error' in outcome:raise outcome['error']
    return outcome['value']

class SupplierHttpLogFilter(logging.Filter):
    """Keep HTTP diagnostics but remove query credentials from httpx log records.

    httpx logs its complete request URL at INFO. Queries can contain CDK result
    tokens, so sanitization must happen on the logger before any handler sees
    the message. The wire request is never modified.
    """
    _urls = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

    @classmethod
    def redact(cls, message):
        def clean(match):
            value = match.group(0)
            # Do not decode/re-encode unknown query strings: even malformed or
            # double-encoded credential parameter names must be hidden.
            fragmentless = value.split('#', 1)[0]
            if '?' in fragmentless:
                return fragmentless.split('?', 1)[0] + '?[REDACTED]'
            return fragmentless
        return cls._urls.sub(clean, str(message))

    def filter(self, record):
        record.msg = self.redact(record.getMessage())
        record.args = ()
        return True


def _install_supplier_http_log_filter():
    logger = logging.getLogger('httpx')
    # The logger is shared by all threads; installing once is sufficient, and
    # retaining the filter protects later raw httpx clients as well.
    if not any(isinstance(item, SupplierHttpLogFilter) for item in logger.filters):
        logger.addFilter(SupplierHttpLogFilter())


_install_supplier_http_log_filter()


class SupplierClientGuard:
    def __init__(self,config,policy=None):self.config=config;self.policy=policy or SupplierEndpointPolicy(config)
    def request(self,method,url,**kwargs):
        deadline=kwargs.pop('deadline',None)
        addresses=_bounded_supplier_policy(self.policy,str(url),deadline)
        if kwargs.pop('follow_redirects',False) or kwargs.get('verify') is False:raise self.policy.denied()
        kwargs.pop('verify',None)
        parsed=httpx.URL(url);original_host=parsed.host
        headers=httpx.Headers(kwargs.get('headers') or {});headers['Accept-Encoding']='identity';kwargs['headers']=headers
        # Resolve once: use the checked public IP for the actual TCP connection,
        # retaining the original hostname for HTTP Host and TLS SNI verification.
        if addresses:
            url=parsed.copy_with(host=addresses[0])
            headers=httpx.Headers(kwargs.get('headers') or {})
            headers['Host']=original_host+(':'+str(parsed.port) if parsed.port and parsed.port!=443 else '')
            kwargs['headers']=headers
            kwargs['extensions']={**(kwargs.get('extensions') or {}),'sni_hostname':original_host}
        ca=self.config.get('SMSINVIT_CA_BUNDLE')
        verify=ssl.create_default_context(cafile=ca if isinstance(ca,str) and ca else None)
        _before_deadline(deadline)
        timeout=kwargs.pop('timeout',30)
        if deadline is not None:timeout=min(timeout,max(0.001,deadline-time.monotonic()))
        with httpx.Client(verify=verify,follow_redirects=False,trust_env=False,timeout=timeout) as client:
            request=client.build_request(method,url,**kwargs)
            timer=None
            if deadline is not None:
                def abort():
                    try:client.close()
                    except Exception:pass
                timer=threading.Timer(max(0,deadline-time.monotonic()),abort);timer.daemon=True;timer.start()
            result=None
            try:
                _before_deadline(deadline)
                result=client.send(request,stream=True)
                _before_deadline(deadline)
            except BaseException:
                if timer:timer.cancel()
                if result is not None:result.close()
                raise
            try:
                content=bytearray()
                for chunk in result.iter_raw(65536):
                    _before_deadline(deadline)
                    if len(content)+len(chunk)>4*1024*1024:raise Error('SUPPLIER_RESPONSE_TOO_LARGE','供应商响应内容过大。',502)
                    content.extend(chunk)
                encoding=result.headers.get('content-encoding','identity').lower().strip()
                decoded=bytes(content)
                if encoding not in ('','identity'):
                    import zlib
                    if encoding not in ('gzip','deflate'):raise Error('SUPPLIER_RESPONSE_INVALID','供应商响应编码不受支持。',502)
                    def inflate(wbits):
                        decoder=zlib.decompressobj(wbits);body=decoder.decompress(decoded,4*1024*1024+1)
                        if len(body)>4*1024*1024 or decoder.unconsumed_tail:raise Error('SUPPLIER_RESPONSE_TOO_LARGE','供应商响应内容过大。',502)
                        if not decoder.eof or decoder.unused_data:raise Error('SUPPLIER_RESPONSE_INVALID','供应商压缩响应不完整。',502)
                        return body
                    try:decoded=inflate(zlib.MAX_WBITS|16 if encoding=='gzip' else zlib.MAX_WBITS)
                    except zlib.error:
                        if encoding!='deflate':raise Error('SUPPLIER_RESPONSE_INVALID','供应商响应编码无效。',502)
                        try:decoded=inflate(-zlib.MAX_WBITS)
                        except zlib.error:raise Error('SUPPLIER_RESPONSE_INVALID','供应商响应编码无效。',502)
                response_headers=httpx.Headers(result.headers)
                response_headers.pop('content-encoding',None);response_headers['content-length']=str(len(decoded))
                completed=httpx.Response(result.status_code,headers=response_headers,content=decoded,request=request)
            finally:
                if timer:timer.cancel()
                result.close()
        _before_deadline(deadline)
        if completed.is_redirect:
            location=completed.headers.get('location')
            if location:
                from urllib.parse import urljoin
                _bounded_supplier_policy(self.policy,urljoin(str(parsed),location),deadline)
            raise Error('SUPPLIER_REDIRECT_FORBIDDEN','供应商返回未经支持的重定向。',502)
        return completed
    def get(self,url,**kwargs):return self.request('GET',url,**kwargs)
    def post(self,url,**kwargs):return self.request('POST',url,**kwargs)
    def close(self):pass

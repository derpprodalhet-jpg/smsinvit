from __future__ import annotations
import base64
import hashlib
import hmac
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime,timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from smsinvit.config import Config,validate_runtime,normalize_cors_origins
from smsinvit.errors import Error
from smsinvit.security import SecretCodec,TokenService,Principal,PermissionService,SupplierEndpointPolicy,TrustedProxyResolver,totp_code,totp_verify,normalize_cdk
from smsinvit.common.auth import Captcha,AdminAuth,RateLimiter
from smsinvit.common.mapper import CATALOG,render
from smsinvit.app import Services,create_app

@pytest.fixture
def config():return Config({'smsinvit.inventory-encryption-key':bytes(range(32)).hex(),'smsinvit.inventory-fingerprint-key':'compatibility-fingerprint-key','smsinvit.jwt-secret':'0123456789abcdef0123456789abcdef','runtime.secure_cookies':False,'runtime.totp_enabled':False,'runtime.cors_origins':'http://localhost:3001'},environ={})
@pytest.fixture
def codec(config):return SecretCodec(config)

class RecordingDB:
    def __init__(self):self.calls=[];self.grants=[];self.in_transaction=False
    def all(self,sql,*params):
        self.calls.append(('all',sql,params))
        if 'permission_scope' in sql:return self.grants
        return []
    def one(self,sql,*params):self.calls.append(('one',sql,params));return None
    def execute(self,sql,*params):self.calls.append(('execute',sql,params));return SimpleNamespace(rowcount=1,lastrowid=None)
    @contextmanager
    def transaction(self,requires_new=False):
        old=self.in_transaction;self.in_transaction=True
        try:yield self
        finally:self.in_transaction=old

class ChallengeDB:
    """Execute challenge predicates against an isolated SQLite table."""
    def __init__(self):
        self.lock=threading.RLock();self.db=sqlite3.connect(':memory:',check_same_thread=False)
        self.db.execute("CREATE TABLE auth_challenges(token_hash TEXT PRIMARY KEY,purpose TEXT,expires_at TEXT,consumed_at TEXT)")
    @contextmanager
    def transaction(self,requires_new=False):
        with self.lock:
            with self.db:yield self
    def execute(self,sql,*params):
        sql=sql.replace('DATE_ADD(NOW(3),INTERVAL 5 MINUTE)',"datetime('now','+5 minutes')").replace('NOW(3)',"datetime('now')")
        if sql.startswith('DELETE FROM auth_challenges'):
            sql="DELETE FROM auth_challenges WHERE token_hash IN (SELECT token_hash FROM auth_challenges WHERE purpose='CAPTCHA_LOGIN' AND expires_at<datetime('now') LIMIT 100)"
        return SimpleNamespace(rowcount=self.db.execute(sql,params).rowcount)

class NoopLimiter:
    def __init__(self):self.calls=[]
    def check(self,*args):self.calls.append(args)

def test_aes_legacy_fixture(codec):
    # Fixed Node crypto.createCipheriv('aes-256-gcm') wire fixture. Key bytes0..31,
    # IV bytes0..11, ciphertext/tag generated independently by the Node runtime.
    payload='AAECAwQFBgcICQoL.rOo+mK5vI3t4kWyYRvixrQ==.oZVx/mpjK4koYfTk3JkZGeq061HQCDofSgKRpe32lCI='
    assert codec.decrypt(payload)=='旧密钥 compatible secret 🔐'
    encrypted=codec.encrypt('private token');assert encrypted.count('.')==2;assert codec.decrypt(encrypted)=='private token'
    iv,tag,data=encrypted.split('.')
    altered=base64.b64encode(bytes([base64.b64decode(tag)[0]^1])+base64.b64decode(tag)[1:]).decode()
    with pytest.raises(ValueError):codec.decrypt(iv+'.'+altered+'.'+data)

def test_trim_fingerprint_and_case_are_legacy_compatible(codec):
    expected=hmac.new(b'compatibility-fingerprint-key',b'AbC',hashlib.sha256).hexdigest()
    assert codec.fingerprint('\ufeff\u00a0AbC\u3000')==expected
    assert codec.digest('AbC')==codec.hmac('AbC')==expected
    assert codec.fingerprint('AbC')!=codec.fingerprint('ABC')
    assert normalize_cdk(' AbC ')== 'AbC'
    # ECMAScript does not trim U+0085, despite Python str.strip doing so.
    assert normalize_cdk('\u0085A\u0085')=='\u0085A\u0085'

def test_password_hash_is_original_argon2id_contract(codec):
    encoded=codec.hash_password('correct password')
    assert '$argon2id$v=19$m=65536,t=3,p=1$' in encoded
    assert codec.verify_password(encoded,'correct password')
    assert not codec.verify_password(encoded,'wrong')
    assert not codec.verify_password('invalid legacy hash','correct password')

def test_session_capabilities_reject_ids_and_cross_services(config):
    token=TokenService(config);id=str(uuid4());other=str(uuid4());issued=token.issue('sms-session',id)
    assert issued==hmac.new(config.require('smsinvit.jwt-secret').encode(),('sms-session:'+id).encode(),hashlib.sha256).hexdigest()
    token.require('sms-session',id,issued)
    for scope,sid,presented,code in [('sms-session',id,None,'SESSION_SECRET_REQUIRED'),('sms-session',other,issued,'SESSION_SECRET_INVALID'),('invocation-session',id,issued,'SESSION_SECRET_INVALID')]:
        with pytest.raises(Error) as failure:token.require(scope,sid,presented)
        assert failure.value.status==401 and failure.value.code==code

def test_jwt_audience_required_and_legacy_claims(config):
    tokens=TokenService(config);user=str(uuid4());session=str(uuid4());signed=tokens.sign(user,'u@example.com','admin',['ADMIN'],session,'admin')
    assert tokens.verify(signed)['sessionId']==session
    with pytest.raises(Error):tokens.verify(tokens.sign(user,'u@example.com','admin',['ADMIN'],session,'web'))
    import jwt,time
    expired=jwt.encode({'sub':user,'sessionId':session,'exp':int(time.time())-1,'aud':'admin'},tokens.key,algorithm='HS256')
    with pytest.raises(Error):tokens.verify(expired)

def test_totp_accepts_adjacent_steps_for_clock_drift():
    # RFC6238 SHA1 test secret, truncation to original six-digit contract.
    secret='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
    assert totp_code(secret,59)=='287082'
    assert totp_verify(secret,'287 082',59)
    # 相邻窗口必须接受：手机时钟与服务器有偏差时，用户填的就是上一个/下一个窗口的码。
    assert totp_verify(secret,'287082',29)
    assert totp_verify(secret,'287082',89)
    # 但仍然只容忍 ±1 个窗口，离得远的码不能过。
    assert not totp_verify(secret,'287082',120)
    assert not totp_verify(secret,'287082',300)
    # 非 6 位数字一律拒绝，不受容差影响。
    assert not totp_verify(secret,'28708',59)
    assert not totp_verify(secret,'abcdef',59)

def test_captcha_wire_encryption_and_tampering(config,codec):
    captcha=Captcha(codec,TokenService(config),config,db=ChallengeDB());issued=captcha.issue('math');assert set(issued)=={'captchaToken','expiresInSeconds','type','prompt','image'}
    a,op,b,*_=issued['prompt'].split();answer=int(a)+int(b) if op=='+' else int(a)-int(b)
    captcha.verify_local(issued['captchaToken'],str(answer))
    payload=json.loads(base64.urlsafe_b64decode(issued['captchaToken'].split('.')[0]+'=='))
    assert payload['ans']!=str(answer) and payload['ans'].count('.')==2
    with pytest.raises(Error):captcha.verify_local(issued['captchaToken']+'x',str(answer))
    with pytest.raises(Error):captcha.verify_local(issued['captchaToken'],str(answer+1))
    assert issued['image'] is None and issued['type']=='math'
    with pytest.raises(Error):captcha.issue('numeric')

def test_permission_own_scope_blocks_replaced_ids():
    db=RecordingDB();permissions=PermissionService(db);actor=Principal('owner','session',('ADMIN',));db.grants=[{'permission_scope':'OWN'}]
    assert permissions.allows(actor,'inventory.read',owner='owner')
    assert not permissions.allows(actor,'inventory.read',owner='other')
    assert not permissions.allows(actor,'inventory.read')
    with pytest.raises(Error) as e:permissions.require(actor,'inventory.read',owner='other')
    assert e.value.status==403
    db.grants=[{'permission_scope':'BOARD'}];assert not permissions.allows(actor,'inventory.read',board='anything')
    assert permissions.allows(Principal('s','ss',('SUPER_ADMIN',)),'any.permission')
    assert not permissions.allows(Principal('s','ss',('SUPER_ADMIN',),True),'any.permission')

def test_database_tls_refuses_nonverified_modes_and_url_overrides():
    # The same policy applies to DATABASE_URL and MYSQL_* configuration; staff
    # runtime overrides cannot downgrade the database transport.
    for values in [
        {'DATABASE_URL':'mysql+pymysql://u:p@db.internal/app','MYSQL_TLS_MODE':'DISABLED'},
        {'MYSQL_HOST':'db.internal','MYSQL_TLS_MODE':'PREFERRED'},
        {'DATABASE_URL':'mysql+pymysql://u:p@127.0.0.1/app?ssl_verify_cert=false','MYSQL_TLS_MODE':'DISABLED'},
        {'MYSQL_HOST':'db.internal'},
    ]:
        with pytest.raises(ValueError):Config(values,environ={}).database_connect_args()
    options=Config({'MYSQL_HOST':'127.0.0.1','MYSQL_TLS_MODE':'DISABLED'},environ={}).database_connect_args()
    assert 'ssl' not in options and options['connect_timeout']==10

def app_for_boundary(config):
    db=RecordingDB();services=Services(db,config);services.limiter=NoopLimiter()
    admin=Principal(str(uuid4()),str(uuid4()),('ADMIN',))
    services.auth.authenticate=lambda value:admin if value=='valid-admin' else (_ for _ in ()).throw(Error('AUTH_INVALID_TOKEN','invalid',401))
    app=create_app(config,db,services=services,include_domains=False,load_runtime=False)
    return TestClient(app),db,services

def test_unauthenticated_and_low_privilege_admin_never_mutate(config):
    client,db,_=app_for_boundary(config)
    response=client.post('/api/v1/admin/plans',json={'code':'X','name':'X','attributesJson':{'productKind':'X_PREMIUM_REDEEM','xMonths':3}})
    assert response.status_code==401;assert not db.calls
    response=client.post('/api/v1/admin/plans',headers={'Authorization':'Bearer valid-admin'},json={'code':'X','name':'X','attributesJson':{'productKind':'X_PREMIUM_REDEEM','xMonths':3}})
    assert response.status_code==403;assert all(c[0]!='execute' for c in db.calls)

def test_csrf_rejects_cookie_without_matching_header_before_auth(config):
    client,db,_=app_for_boundary(config);client.cookies.set('lc_admin_access','valid-admin')
    result=client.post('/api/v1/admin/plans',json={})
    assert result.status_code==403 and result.json()['error']['code']=='CSRF_INVALID'
    assert not db.calls
    result=client.post('/api/v1/admin/plans',headers={'X-CSRF-Token':'incorrect'},json={})
    assert result.status_code==403;assert not db.calls

def test_cors_rejects_unlisted_origin_and_wildcard(config):
    client,db,_=app_for_boundary(config)
    result=client.options('/api/v1/admin/plans',headers={'Origin':'https://attacker.invalid','Access-Control-Request-Method':'POST'})
    assert result.status_code==403;assert 'Access-Control-Allow-Origin' not in result.headers;assert not db.calls
    result=client.options('/api/v1/admin/plans',headers={'Origin':'http://localhost:3001','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'Authorization,X-CSRF-Token'})
    assert result.status_code==204 and result.headers['Access-Control-Allow-Origin']=='http://localhost:3001'
    assert result.content==b''
    with pytest.raises(ValueError):validate_runtime('runtime.cors_origins','*')


def test_cors_trailing_slash_default_port_and_client_cache_headers(config):
    config.values['runtime.cors_origins']='http://localhost:3001/, https://EXAMPLE.test:443/'
    validate_runtime('runtime.cors_origins',config.values['runtime.cors_origins'])
    assert normalize_cors_origins(config.values['runtime.cors_origins'])==['http://localhost:3001','https://example.test']
    client,_,_=app_for_boundary(config)
    for origin in ('http://localhost:3001','https://example.test'):
        result=client.options('/api/v1/sms/redeem',headers={'Origin':origin,'Access-Control-Request-Method':'POST',
                              'Access-Control-Request-Headers':'Content-Type,X-CSRF-Token,Cache-Control,Pragma'})
        assert result.status_code==204 and result.headers['Access-Control-Allow-Origin']==origin
        assert result.content==b''
        actual=client.get('/api/v1/admin/auth/captcha',headers={'Origin':origin})
        assert actual.status_code==200 and actual.headers['Access-Control-Allow-Origin']==origin
    # A runtime update is compared canonically without requiring an app restart.
    config.values['runtime.cors_origins']='https://new.example.test/'
    assert client.options('/api/v1/sms/redeem',headers={'Origin':'https://new.example.test','Access-Control-Request-Method':'POST'}).status_code==204
    assert client.options('/api/v1/sms/redeem',headers={'Origin':'https://example.test','Access-Control-Request-Method':'POST'}).status_code==403


@pytest.mark.parametrize('origin',['*','https://user@example.test/','https://:pass@example.test/','https://example.test/path',
                                 'https://example.test/?q=x','https://example.test/#fragment','https://example.test:99999/',
                                 'https://example.test\\evil/','https://exa mple.test/','https://example.test,'])
def test_cors_origin_validation_still_rejects_non_origins(origin):
    with pytest.raises(ValueError):validate_runtime('runtime.cors_origins',origin)

def test_security_headers_request_ids_and_body_limit(config):
    client,db,_=app_for_boundary(config)
    r=client.get('/api/v1/admin/auth/captcha?type=math',headers={'X-Request-Id':'valid-request-1'})
    assert r.status_code==200 and r.headers['x-request-id']=='valid-request-1'
    assert r.headers['x-frame-options']=='DENY' and r.headers['x-content-type-options']=='nosniff'
    db.calls.clear()
    r=client.post('/api/v1/admin/auth/login',content=b'x'*(1048576+1),headers={'Content-Type':'application/json'})
    assert r.status_code==413 and not db.calls

def test_proxy_walk_rejects_untrusted_spoof_and_duplicate_headers():
    from starlette.requests import Request
    def req(peer,headers):return Request({'type':'http','method':'GET','path':'/','headers':[(k.encode(),v.encode()) for k,v in headers],'client':(peer,123),'server':('localhost',80),'scheme':'http','query_string':b''})
    resolver=TrustedProxyResolver('127.0.0.0/8,10.0.0.0/8')
    with pytest.raises(Error) as failure:resolver.resolve(req('203.0.113.10',[('x-forwarded-for','1.1.1.1')]))
    assert failure.value.status==400 and failure.value.code=='INVALID_PROXY_HEADERS'
    assert resolver.resolve(req('127.0.0.1',[('x-forwarded-for','1.1.1.1, 198.51.100.5, 10.0.0.1')]))=='198.51.100.5'
    for headers in [[('x-forwarded-for','1.1.1.1'),('x-forwarded-for','2.2.2.2')],[('x-forwarded-for','localhost')]]:
        with pytest.raises(Error):resolver.resolve(req('127.0.0.1',headers))

PROXY_HEADERS=[('Forwarded','for=1.1.1.1;proto=https;host=example.test'),('X-Forwarded-For','1.1.1.1'),('X-Real-IP','1.1.1.1'),('X-Forwarded-Proto','https'),('X-Forwarded-Host','example.test'),('X-Forwarded-Port','443')]
PROXY_PATHS=['/api/v1/sms/redeem','/api/v1/invitation/redeem','/api/v1/admin/plans','/api/v1/admin/invitation/native/api/settings']

def ingress_client(config,peer):
    original,db,services=app_for_boundary(config);calls={'auth':0,'business':0,'upstream':0}
    def authenticate(token):
        calls['auth']+=1
        return Principal('owner','session',('SUPER_ADMIN',))
    services.auth.authenticate=authenticate
    def business(request):
        calls['business']+=1;db.execute('synthetic business write');calls['upstream']+=1
        return {'ip':request.state.client_ip}
    def route(request:Request):return business(request)
    # The common admin route already exists; other probe routes use the same
    # middleware and record any entry into business or upstream work.
    for path in PROXY_PATHS:
        if path!='/api/v1/admin/plans':original.app.add_api_route(path,route,methods=['POST'])
    return TestClient(original.app,client=(peer,123)),db,services,calls

@pytest.mark.parametrize('path',PROXY_PATHS)
@pytest.mark.parametrize('header',PROXY_HEADERS)
def test_spoofed_proxy_header_rejected_before_auth_rate_and_business(config,path,header):
    client,db,services,calls=ingress_client(config,'203.0.113.10')
    result=client.post(path,headers=[header,('Authorization','Bearer fixture')],json={})
    assert result.status_code==400 and result.json()['error']['code']=='INVALID_PROXY_HEADERS'
    assert calls=={'auth':0,'business':0,'upstream':0} and not db.calls and not services.limiter.calls
    assert result.headers['x-content-type-options']=='nosniff'
    assert result.json()['error']['details'] is None

INVALID_TRUSTED_HEADERS=[
    [('X-Forwarded-For','1.1.1.1'),('X-Forwarded-For','2.2.2.2')],
    [('X-Forwarded-For','')],[('X-Forwarded-For','localhost')],
    [('X-Forwarded-For','1.1.1.1,,10.0.0.1')],[('X-Forwarded-For','1.1.1.1,')],
    [('X-Forwarded-For','1.1.1.1:443')],[('X-Forwarded-For','fe80::1%eth0')],
    [('X-Forwarded-For',','.join(['1.1.1.1']*33))],[('X-Forwarded-For','1'*2049)],
    [('Forwarded','for=unknown')],[('Forwarded','for=1.1.1.1;for=2.2.2.2')],
    [('Forwarded','for="1.1.1.1')],[('Forwarded','for=1.1.1.1;host=attacker.test/path')],
    [('X-Real-IP','localhost')],[('X-Real-IP','1.1.1.1,2.2.2.2')],
    [('X-Forwarded-Proto','https,http')],[('X-Forwarded-Proto','javascript')],
    [('X-Forwarded-Host','https://example.test')],[('X-Forwarded-Host','user@example.test')],
    [('X-Forwarded-Host','example.test/path')],[('X-Forwarded-Host','example.test:65536')],
    [('X-Forwarded-Host','[::1')],[('X-Forwarded-Port','0')],
    [('X-Forwarded-Port','65536')],[('X-Forwarded-Port','443,80')],
    *[[(name,value),(name,value)] for name,value in PROXY_HEADERS],
]

@pytest.mark.parametrize('headers',INVALID_TRUSTED_HEADERS)
def test_malformed_trusted_proxy_headers_rejected_before_auth_rate_and_business(config,headers):
    client,db,services,calls=ingress_client(config,'127.0.0.1')
    result=client.post('/api/v1/admin/invitation/native/api/settings',headers=[*headers,('Authorization','Bearer fixture')],json={})
    assert result.status_code==400 and result.json()['error']['code']=='INVALID_PROXY_HEADERS'
    assert calls=={'auth':0,'business':0,'upstream':0} and not db.calls and not services.limiter.calls

@pytest.mark.parametrize('peer,headers,expected',[
    ('203.0.113.10',[],'203.0.113.10'),
    ('127.0.0.1',[('X-Forwarded-For','198.51.100.5'),('X-Forwarded-Proto','https'),('X-Forwarded-Host','redeem.example.test:443')],'198.51.100.5'),
    ('127.0.0.1',[('X-Real-IP','2001:db8::1'),('X-Forwarded-Host','[2001:db8::2]:443'),('X-Forwarded-Port','443')],'2001:db8::1'),
    ('127.0.0.1',[('Forwarded','for="[2001:db8::1]:443";by=127.0.0.1;host="[2001:db8::2]:443";proto=https')],'127.0.0.1'),
])
def test_valid_proxy_ingress_preserves_normal_business_flow(config,peer,headers,expected):
    client,db,services,calls=ingress_client(config,peer)
    result=client.post('/api/v1/sms/redeem',headers=headers,json={})
    assert result.status_code==200 and result.json()=={'ip':expected}
    assert calls=={'auth':0,'business':1,'upstream':1} and len(db.calls)==1 and len(services.limiter.calls)==1
    assert services.limiter.calls[0]==('public.v2.sms.entry.ip:'+expected,600,60)

def test_outbound_policy_host_dns_and_local_dev_defaults(config):
    policy=SupplierEndpointPolicy(config,lambda host:['93.184.216.34']);policy.require_allowed('https://smsbower.page/stubs/handler_api.php')
    for url in ('http://smsbower.page/stubs','https://evil.invalid/','https://user:pass@smsbower.page/','https://smsbower.page./','https://smsbower.page:444/'):
        with pytest.raises(Error):policy.require_allowed(url)
    for ip in ('127.0.0.1','10.0.0.1','169.254.169.254','100.64.0.1','::1','fd00::1','2001:db8::1'):
        with pytest.raises(Error):SupplierEndpointPolicy(config,lambda host:[ip]).require_allowed('https://smsbower.page/')
    test=Config({'SMSINVIT_ENV':'test','SMSINVIT_SUPPLIER_TEST_URLS':'http://127.0.0.1:9900/upstream'},environ={});dev=SupplierEndpointPolicy(test)
    dev.require_allowed('http://127.0.0.1:9900/upstream');dev.require_allowed('http://127.0.0.1:9900/upstream/order')
    with pytest.raises(Error):dev.require_allowed('http://127.0.0.1:9900/unrelated')

def test_all_legacy_sql_templates_have_only_parameterized_inputs():
    for methods in CATALOG.values():
        for spec in methods.values():
            values={k:None for k in spec['params']};values.update(limit=10,offset=0,size=5,ids=["x' OR 1=1 --",'y'])
            sql,params=render(spec['sql'],values)
            assert '#{' not in sql and '<script>' not in sql and '<foreach' not in sql
            assert "x' OR 1=1 --" not in sql
    sql,params=render(CATALOG['DurableJobMapper']['markClaimed']['sql'],{'ids':['one','two'],'token':'lease','seconds':60})
    assert params==['lease',60,'one','two'];assert 'IN' in sql and sql.count('?')==4

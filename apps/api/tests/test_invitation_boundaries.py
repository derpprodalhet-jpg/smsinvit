"""Security regression tests use no invitation source code or production network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
import time
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from smsinvit.app import Services,create_app
from smsinvit.config import Config
from smsinvit.errors import Error
from smsinvit.security import Principal
from smsinvit.invitation import mysql
from smsinvit.invitation.router import build_router,redacted_stream
from smsinvit.invitation.runtime import TaskGate,TASK_SETTINGS
from smsinvit.invitation.safety import redact,RedactingFormatter

class DB:
    def __init__(self):self.writes=[]
    def one(self,*a):return None
    def all(self,*a):return []
    def execute(self,*a):self.writes.append(a);return SimpleNamespace(rowcount=1)
    @contextmanager
    def transaction(self,**kw):yield self

# 授权凭据文本导出：JWT 形态的 access_token 与 password=k=v 两种写法都会被 redact()
# 改写，所以这一份文本能同时验证两条脱敏规则有没有被绕过。
EXPORT_TEXT='user@example.test----hunter2\naccess_token=eyJhbGciOiJub25lIn0.eyJleHAiOjF9.sig\n'

@pytest.fixture
def native(tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOMES_ROOT',str(tmp_path/'codex-homes'))
    config=Config({'smsinvit.inventory-encryption-key':'a1'*32,'smsinvit.inventory-fingerprint-key':'boundary-fixture-key','smsinvit.jwt-secret':'boundary-fixture-jwt-key-over32chars'},environ={})
    db=DB();s=Services(db,config);buckets=[];s.limiter=SimpleNamespace(check=lambda bucket,limit,window=None:buckets.append((bucket,limit,window)));s.rate_buckets=buckets;s.auth.authenticate=lambda *a:Principal('actor','session',('ADMIN',));allowed={'invitation.read'};calls=[]
    def require(request,permission,*a,**kw):
        if permission not in allowed:raise Error('PERMISSION_DENIED','denied',403)
        return request.state.principal
    s.permissions=SimpleNamespace(require=require,allows=lambda actor,p,*a,**kw:p in allowed)
    app=FastAPI()
    @app.get('/api/accounts')
    def api_accounts():calls.append('accounts');return {'items':getattr(s,'account_records',[{'email':'test@example.test','password':'abc','access_token':'short','phone_number':'+12025550123','phone_code':'123456','note':'normal metadata'}])}
    @app.post('/api/settings/mail')
    def setting():calls.append('write');return {'ok':True}
    @app.post('/api/settings/invite')
    def invite_setting():calls.append('invite-setting');return {'ok':True}
    @app.post('/api/register')
    def register():calls.append('register');return {'ok':True}
    @app.post('/api/registered/codex_auth')
    def codex_auth():calls.append('codex-auth');return {'ok':True}
    @app.get('/api/settings/runtime_check')
    def runtime_check():calls.append('runtime-check');return {'ok':True}
    @app.get('/api/leaky-error')
    def leaky_error():raise HTTPException(400,'password="opaque-credential"')
    @app.post('/api/registered/export')
    def export():calls.append('export');return {'ok':True,'count':1,'filename':'AT.txt','mode':'text',
                                               'mime':'text/plain; charset=utf-8','text':EXPORT_TEXT}
    @app.get('/api/events')
    def events():
        async def data():yield 'event: status\ndata: '+json.dumps({'message':'upstream '+json.dumps({'password':'abc','access_token':'short'}),'refresh_token':'xyz'})+'\n\n'
        return StreamingResponse(data(),media_type='text/event-stream')
    inv=SimpleNamespace(native=SimpleNamespace(app=app),s=s,data_dir=tmp_path,log_dir=tmp_path/'logs',repo=SimpleNamespace(get_setting=lambda *a:''),_started=False,_stopping=False,page_content=lambda:{},action=lambda *a:calls.append('external-action'),generate_native=lambda *a:calls.append('generate'))
    def require_executor():
        if not inv._started or inv._stopping:raise Error('INVITATION_EXECUTOR_UNAVAILABLE','暂时无法开始，请稍后再试。',503)
    inv.require_executor_accepting=require_executor
    inv.executor_status=lambda:{'enabled':False,'ready':False,'state':'disabled','message':'已暂停新的执行请求，已有任务继续处理。'}
    inv.set_executor_enabled=lambda enabled,actor:calls.append(('executor',enabled,actor.id)) or {'enabled':enabled,'ready':False,'state':'unavailable' if enabled else 'disabled','message':'safe fixture'}
    # 账号工厂自己的读写接口：与邀请执行完全独立的一套开关与快照。
    inv.pool_status=lambda:{'enabled':False,'running':False,'target':10,'available':0,'mailboxAvailable':0,'concurrency':2,'checkSeconds':60,'rounds':0,'lastError':''}
    inv.set_pool_enabled=lambda enabled,actor:calls.append(('pool',enabled,actor.id)) or {**inv.pool_status(),'enabled':enabled}
    s.invitation=inv;merged=create_app(config,db,services=s,include_domains=False,load_runtime=False);merged.include_router(build_router(s))
    with TestClient(merged) as client:yield client,s,allowed,calls

def test_anonymous_low_privilege_and_bad_csrf_do_not_call_native(native):
    client,s,allowed,calls=native;base='/api/v1/admin/invitation/native'
    assert client.get(base+'/api/accounts').status_code==401
    assert client.post(base+'/api/settings/mail',headers={'Authorization':'Bearer fixture'},json={}).status_code==403
    allowed.add('invitation.manage')
    assert client.post(base+'/api/settings/mail',headers={'Authorization':'Bearer fixture'},cookies={'lc_refresh':'r','lc_csrf':'valid'},json={}).status_code==403
    assert not calls and not s.db.writes

def test_native_generator_requires_export_and_credentials_before_business_write(native):
    client,s,allowed,calls=native;allowed.update(('invitation.manage','inventory.manage'))
    path='/api/v1/admin/invitation/native/api/cdk/generate';headers={'Authorization':'Bearer fixture'}
    assert client.post(path,headers=headers,json={'count':1}).status_code==403
    allowed.add('inventory.export')
    assert client.post(path,headers=headers,json={'count':1}).status_code==403
    assert not calls
    allowed.add('invitation.credentials')
    assert client.post(path,headers=headers,json={'count':1}).status_code==200 and calls==['generate']

def test_native_structured_response_and_sse_hide_short_secrets(native):
    client,s,allowed,calls=native;headers={'Authorization':'Bearer fixture'};base='/api/v1/admin/invitation/native'
    data=client.get(base+'/api/accounts',headers=headers).json()['items'][0]
    assert data['password']==data['access_token']=='***' and data['note']=='normal metadata'
    assert data['phone_number']==data['phone_code']=='***'
    body=client.get(base+'/api/events',headers=headers).text
    frame=json.loads(next(line[6:] for line in body.splitlines() if line.startswith('data: ')))
    assert frame['refresh_token']=='***' and 'abc' not in body and 'short' not in body
    assert 'opaque-credential' not in client.get(base+'/api/leaky-error',headers=headers).text


def test_sms_details_require_existing_credential_permission(native):
    client,s,allowed,calls=native
    allowed.add('invitation.credentials')
    data=client.get('/api/v1/admin/invitation/native/api/accounts',
                    headers={'Authorization':'Bearer fixture'}).json()['items'][0]
    assert data['phone_number']=='+12025550123' and data['phone_code']=='123456'


def test_historical_phone_details_are_attached_before_permission_masking(native):
    client,s,allowed,calls=native
    inv=s.invitation
    inv.log_dir.mkdir()
    log=inv.log_dir/'fixture.log'
    email='legacy@example.test'
    log.write_text(
        '01:00:00 [INFO] registrar: [sms] 已租到新号码，平台=smsbower 国家=73 测试 号码=+12025550123，金额=0.04 USD\n'
        '01:00:01 [INFO] registrar: [sms] 已收到验证码：001234（号码=+12025550123，金额=0.04 USD）\n'
        '01:00:02 [INFO] registrar: [sms] 业务验证成功，平台结算已确认\n'
        f'01:00:03 [INFO] registrar: [register] 注册阶段手机绑定成功: {email}（兑换时不再需要 add-phone）\n',
        encoding='utf-8')
    inv.repo.list_runs_for_emails=lambda emails,limit:[{'email':email,'kind':'register_pool','log_path':str(log)}]
    s.account_records=[{'email':email,'phone_bound_ok':1,'used_proxy':1}]
    path='/api/v1/admin/invitation/native/api/accounts'
    headers={'Authorization':'Bearer fixture'}
    masked=client.get(path,headers=headers).json()['items'][0]
    assert masked['phone_number']==masked['phone_code']=='***'
    allowed.add('invitation.credentials')
    details=client.get(path,headers=headers).json()['items'][0]
    assert details['phone_number']=='+12025550123' and details['phone_code']=='001234'
    assert s.account_records==[{'email':email,'phone_bound_ok':1,'used_proxy':1}]

def test_stopped_executor_rejects_public_start_without_action(native):
    client,s,allowed,calls=native
    r=client.post('/api/v1/invitation/start',json={'sessions':[{'id':'ignored','sessionSecret':'ignored'}]})
    assert r.status_code==503 and not calls
    assert r.json()['error']['code']=='SERVICE_UNAVAILABLE'
    assert 'EXECUTOR' not in r.text

@pytest.mark.parametrize('path,method', [('/api/v1/invitation/redeem','POST'),
                                        ('/api/v1/invitation/status','POST'),
                                        ('/api/v1/invitation/redemption-attempts/attempt','GET')])
def test_public_error_boundary_hides_diagnostics_including_recovery(native,path,method):
    client,s,allowed,calls=native
    def fail(*args):raise Error('INVITATION_PRIVATE_STAGE','PRIVATE_DETAIL',503,{'diagnostic':'PRIVATE_DETAIL'})
    s.invitation.redeem=s.invitation.action=s.invitation.redemption_attempt_status=fail
    result=client.request(method,path,json={})
    assert result.status_code==503 and result.json()['error']['code']=='SERVICE_UNAVAILABLE'
    assert 'PRIVATE_' not in result.text and result.json()['error']['details'] is None

@pytest.mark.parametrize('count,ok', [(1,True),(10,True),(11,False),(50,False),(51,False)])
def test_invitation_code_batch_limit_is_ten(native,count,ok):
    """服务端硬上限：一次最多 10 条卡密。

    直接调用真实的校验函数（不是替身），确保绕过页面直接打接口也会被拒。
    校验只依赖错误构造，不需要已初始化的实例。
    """
    from smsinvit.invitation.service import INVITATION_BATCH_LIMIT, InvitationService
    assert INVITATION_BATCH_LIMIT==10
    service=InvitationService.__new__(InvitationService)
    codes=[f'SYNTHETIC-{i}' for i in range(count)]
    if ok:
        assert service._codes(codes)==codes
    else:
        with pytest.raises(Error) as error:
            service._codes(codes)
        assert error.value.status==422
        assert error.value.message==f'一次请输入 1–{INVITATION_BATCH_LIMIT} 条卡密。'

@pytest.mark.parametrize('count,ok', [(1,True),(10,True),(11,False)])
def test_invitation_action_batch_limit_is_ten(native,count,ok):
    """同一次「开始/查询/确认邀请信」也最多 10 个会话。

    `action` 是先校验再起循环，并且支持"只处理前 N 个"的部分处理标记；
    这里用全 1 的标记确保 count==10 时会走到真实处理（进而暴露出替身缺失），
    所以只断言"超限在进入处理之前就被拒"。
    """
    from smsinvit.invitation.service import INVITATION_BATCH_LIMIT, InvitationService
    assert INVITATION_BATCH_LIMIT==10
    service=InvitationService.__new__(InvitationService)
    entries=[{'id':'00000000-0000-4000-8000-%012d'%i,'sessionSecret':'0'*64} for i in range(count)]
    if count>INVITATION_BATCH_LIMIT:
        with pytest.raises(Error) as error:
            service.action(entries,'status')
        assert error.value.status==422
        assert error.value.message==f'一次最多处理 {INVITATION_BATCH_LIMIT} 个会话。'
    else:
        # 不超限：不能因为"数量"被拒（后续会因替身会话不存在而失败，那是另一回事）。
        try:
            service.action(entries,'status')
        except Error as error:
            assert error.value.status!=422 or '数量' in str(error.value.message) or '一次最多' not in error.value.message
        except AttributeError:
            pass


@pytest.mark.parametrize('payload',["' OR 1=1 --","x'; DROP TABLE invitation_v2_cdk_codes; --",
                                    '1 UNION SELECT * FROM admin_users','%27%20OR%201%3D1',
                                    'code`; DELETE FROM invitation_v2_cdk_codes WHERE 1=1; --',
                                    "1' AND (SELECT SLEEP(5))=0 --"])
def test_public_card_inputs_are_never_concatenated_into_sql(native,payload):
    """注入负例：恶意卡密只能作为参数值，不允许改变语句结构或读到自己以外的行。

    这里不使用任何 SQL 关键字黑名单，只看两件事：语句仍由固定 SQL 渲染、
    输入原样进参数；以及入口把注入串当成"没有这张卡"而不是 5xx。
    """
    client,s,allowed,calls=native
    from smsinvit.common.mapper import CATALOG,render
    for methods in CATALOG.values():
        for spec in methods.values():
            values={k:None for k in spec['params']};values.update(code=payload,search=payload,identifier=payload)
            sql,params=render(spec['sql'],values)
            assert payload.split(';')[0] not in sql
    # 公开入口把注入串当普通卡密：注入既不能落地成语句，也不能把异常当结果返回。
    def missing(*a,**kw):raise Error('INVITATION_CDK_NOT_FOUND','未找到该卡密。',404)
    s.invitation.redeem=missing
    result=client.post('/api/v1/invitation/redeem',json={'codes':[payload]})
    assert result.status_code==404
    assert 'DROP TABLE' not in result.text and 'admin_users' not in result.text

def test_executor_admin_read_manage_and_csrf_guards(native):
    client,s,allowed,calls=native;path='/api/v1/admin/invitation/executor';headers={'Authorization':'Bearer fixture'}
    assert client.get(path).status_code==401
    result=client.get(path,headers=headers)
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    assert result.json()['data']['state']=='disabled'
    assert client.put(path,headers=headers,json={'enabled':True}).status_code==403
    allowed.add('invitation.manage')
    assert client.put(path,headers=headers,cookies={'lc_refresh':'fixture'},json={'enabled':True}).status_code==403
    assert not calls
    result=client.put(path,headers=headers,json={'enabled':True})
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    assert calls==[('executor',True,'actor')]

@pytest.mark.parametrize('body',[{'enabled':'true'},{'enabled':1},{},{'enabled':True,'background':True}])
def test_executor_admin_invalid_payload_never_mutates(native,body):
    client,s,allowed,calls=native;allowed.add('invitation.manage')
    result=client.put('/api/v1/admin/invitation/executor',headers={'Authorization':'Bearer fixture'},json=body)
    assert result.status_code==422 and not calls


def test_account_pool_admin_read_manage_and_csrf_guards(native):
    client,s,allowed,calls=native;path='/api/v1/admin/invitation/pool';headers={'Authorization':'Bearer fixture'}
    assert client.get(path).status_code==401
    result=client.get(path,headers=headers)
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    assert result.json()['data']['target']==10 and result.json()['data']['enabled'] is False
    assert client.put(path,headers=headers,json={'enabled':True}).status_code==403
    allowed.add('invitation.manage')
    assert client.put(path,headers=headers,cookies={'lc_refresh':'fixture'},json={'enabled':True}).status_code==403
    assert not calls
    result=client.put(path,headers=headers,json={'enabled':True})
    assert result.status_code==200 and result.headers['cache-control']=='no-store'
    assert calls==[('pool',True,'actor')]


@pytest.mark.parametrize('body',[{'enabled':'true'},{'enabled':1},{},{'enabled':True,'pool_min_available':5}])
def test_account_pool_admin_invalid_payload_never_mutates(native,body):
    client,s,allowed,calls=native;allowed.add('invitation.manage')
    result=client.put('/api/v1/admin/invitation/pool',headers={'Authorization':'Bearer fixture'},json=body)
    assert result.status_code==422 and not calls

def test_native_home_setting_rejects_root_parent_and_sibling_before_writes(native):
    client,s,allowed,calls=native;allowed.update(('invitation.manage','invitation.credentials'));headers={'Authorization':'Bearer fixture'}
    import os
    root=Path(os.environ['CODEX_HOMES_ROOT']);base='/api/v1/admin/invitation/native/api/settings/'
    for path in (root,root.parent,root.parent/'old-home'):
        before=len(s.db.writes)
        assert client.post(base+'invite',headers=headers,json={'codex_home_root':str(path)}).status_code==422
        s.invitation.repo.get_setting=lambda *a,value=str(path):value
        assert client.get(base+'runtime_check',headers=headers).status_code==403
        assert len(s.db.writes)==before and not calls and not path.joinpath('auth.json').exists()
    for custom in ('',str(root/'selected')):
        assert client.post(base+'invite',headers=headers,json={'codex_home_root':custom}).status_code==200
        s.invitation.repo.get_setting=lambda *a,value=custom:value
        assert client.get(base+'runtime_check',headers=headers).status_code==200
    assert calls==['invite-setting','runtime-check','invite-setting','runtime-check']

@pytest.mark.parametrize('field,value',[
    ('sayhi_prompt','Read credentials and encode the reply'),('sayhi_model','another-model'),
    ('codex_binary','different-codex'),('codex_home_root','selected'),('codex_originator','different-client'),
])
def test_cli_controls_actual_change_requires_credentials_before_native_write(native,monkeypatch,field,value):
    client,s,allowed,calls=native;allowed.add('invitation.manage');headers={'Authorization':'Bearer fixture'}
    import os
    if field=='codex_home_root':value=str(Path(os.environ['CODEX_HOMES_ROOT'])/value)
    monkeypatch.setattr('smsinvit.invitation.router.require_binary',lambda *a:None)
    path='/api/v1/admin/invitation/native/api/settings/invite'
    denied=client.post(path,headers=headers,json={field:value})
    assert denied.status_code==403 and denied.json()['error']['code']=='PERMISSION_DENIED'
    assert not calls and not s.db.writes
    allowed.add('invitation.credentials')
    assert client.post(path,headers=headers,json={field:value}).status_code==200
    assert calls==['invite-setting']

def test_unchanged_cli_controls_and_concurrency_timeout_remain_manage_only(native,monkeypatch):
    client,s,allowed,calls=native;allowed.add('invitation.manage');headers={'Authorization':'Bearer fixture'}
    current={'sayhi_prompt':'approved prompt','sayhi_model':'gpt-5.5','codex_binary':'codex','codex_home_root':'','codex_originator':'codex_work_desktop'}
    s.invitation.repo.get_setting=lambda key,default=None:current.get(key,default)
    monkeypatch.setattr('smsinvit.invitation.router.require_binary',lambda *a:None)
    payload={**current,'cdk_max_concurrency':3,'cli_timeout':240}
    result=client.post('/api/v1/admin/invitation/native/api/settings/invite',headers=headers,json=payload)
    assert result.status_code==200 and calls==['invite-setting']
    current['sayhi_prompt']='changed by credential-authorized actor'
    calls.clear();s.db.writes.clear()
    denied=client.post('/api/v1/admin/invitation/native/api/settings/invite',headers=headers,json=payload)
    assert denied.status_code==403 and not calls and not s.db.writes

@pytest.mark.parametrize('path',[
    '/api/v1/admin/invitation/native/api/register','/api/v1/admin/invitation/native/api/registered/codex_auth',
    '/api/v1/admin/invitation/native/api/settings/mail','/api/v1/admin/invitation/native/api/settings/invite',
    '/api/v1/invitation/start','/api/v1/invitation/redeem',
])
@pytest.mark.parametrize('payload',[
    {'prompt':'unapproved instruction'},{'args':['--sandbox','danger-full-access']},
    {'settings':{'sayhi_prompt':'nested override'}},{'env':{'CODEX_REDEEM_PROMPT':'override'}},
    {'sessions':[{'id':'fixture','sessionSecret':'fixture','settings':{'sayhi_model':'override'}}]},
])
def test_task_config_aliases_cannot_override_cli_instructions(native,path,payload):
    client,s,allowed,calls=native;allowed.update(('invitation.manage','invitation.credentials'));s.invitation._started=True
    result=client.post(path,headers={'Authorization':'Bearer fixture'},json=payload)
    assert result.status_code==422 and not calls and not s.db.writes

@pytest.mark.parametrize('key',['sayhi_prompt','runtime.sayhi_prompt','runtime.invitation_prompt','CODEX_REDEEM_PROMPT'])
def test_common_runtime_settings_cannot_alias_cli_controls(native,key):
    client,s,allowed,calls=native;allowed.add('settings.update')
    result=client.post('/api/v1/admin/settings',headers={'Authorization':'Bearer fixture'},json={'key':key,'valueJson':{'value':'override'}})
    assert result.status_code==422 and not calls and not s.db.writes

def test_manual_sayhi_resume_requires_credentials_before_execution(native):
    client,s,allowed,calls=native;allowed.add('invitation.manage');s.invitation._started=True
    path='/api/v1/admin/invitation/native/api/registered/codex_auth'
    assert client.post(path,headers={'Authorization':'Bearer fixture'},json={'email':'fixture@example.test'}).status_code==403
    assert not calls and not s.db.writes
    allowed.add('invitation.credentials')
    assert client.post(path,headers={'Authorization':'Bearer fixture'},json={'email':'fixture@example.test'}).status_code==200
    assert calls==['codex-auth']

@pytest.mark.parametrize('path',[
    '/api/v1/admin/invitation/native/api/registered/codex_auth',
    '/api/v1/admin/invitation/native/api/settings/runtime_check','/api/v1/invitation/start',
])
def test_query_aliases_cannot_inject_cli_configuration(native,path):
    client,s,allowed,calls=native;allowed.update(('invitation.manage','invitation.credentials'));s.invitation._started=True
    headers={'Authorization':'Bearer fixture'}
    if path.endswith('runtime_check'):result=client.get(path,headers=headers,params={'CODEX_REDEEM_PROMPT':'override'})
    else:result=client.post(path,headers=headers,params={'sayhi_prompt':'override'},json={})
    assert result.status_code==422 and not calls and not s.db.writes

def test_redaction_preserves_json_and_short_labeled_values():
    data={'password':'abc','access_token':'x"y','refresh_token':'a\\b','normal':'keep'}
    redacted=redact(json.dumps(data));assert json.loads(redacted)['normal']=='keep'
    for key in ('password','access_token','refresh_token'):assert json.loads(redacted)[key]=='[REDACTED]'
    assert 'abc' not in redact("{'password': 'abc'}")
    formatter=RedactingFormatter(logging.Formatter('%(message)s'))
    record=logging.LogRecord('test',40,__file__,1,'error password="abc"',(),None)
    assert 'abc' not in formatter.format(record)

@pytest.mark.parametrize('label',['CDK','smsinvitCdk','cdk_code','zc','sessionSecret','client_secret','keyPlaintext','rawKey','confirmationToken'])
def test_actual_formatter_redacts_card_and_capability_labels_in_encoded_urls(label):
    from urllib.parse import quote
    import io
    secret='Opaque /+card credential';encoded=quote(secret,safe='');encoded_label=''.join('%'+format(ord(c),'02X') for c in label)
    output=io.StringIO();handler=logging.StreamHandler(output);handler.setFormatter(RedactingFormatter(logging.Formatter('%(message)s')))
    for key in (label,encoded_label):
        record=logging.LogRecord('fixture-access',logging.INFO,__file__,1,'GET %s',('/admin?'+key+'='+encoded+'&page=2',),None)
        handler.handle(record)
    text=output.getvalue()
    assert secret not in text and encoded not in text
    assert text.count('[REDACTED]')==2 and text.count('page=2')==2
    record=logging.LogRecord('fixture-label',logging.INFO,__file__,1,json.dumps({label:secret}),(),None)
    rendered=handler.format(record)
    assert secret not in rendered and json.loads(rendered)[label]=='[REDACTED]'

def test_production_launcher_disables_raw_access_urls(monkeypatch):
    import runpy,uvicorn
    calls=[];monkeypatch.setattr(uvicorn,'run',lambda *args,**kwargs:calls.append((args,kwargs)))
    runpy.run_module('smsinvit.__main__',run_name='__main__')
    assert len(calls)==1 and calls[0][1]['access_log'] is False and calls[0][1]['proxy_headers'] is False

def test_runtime_second_service_cannot_rebind_repository(monkeypatch):
    first=SimpleNamespace();second=SimpleNamespace();monkeypatch.setattr(mysql,'_runtime_owner',first);monkeypatch.setattr(mysql,'_services',first)
    with pytest.raises(RuntimeError):mysql.configure(second,runtime=True)
    assert mysql.services() is first

def gate_fixture(monkeypatch):
    rows=[{'key':'sayhi_model','value':'initial-model'}]
    monkeypatch.setattr(mysql,'services',lambda:SimpleNamespace(db=SimpleNamespace(all=lambda *a:list(rows))))
    gate=TaskGate(SimpleNamespace(get_setting=lambda *a:'3'))
    return gate,rows

def test_shared_limit_and_same_email_fence(monkeypatch):
    gate,rows=gate_fixture(monkeypatch);lock=threading.Lock();active=peak=0;emails=set();duplicates=[]
    def authorize_registered_email(email):
        nonlocal active,peak
        with lock:
            if email in emails:duplicates.append(email)
            emails.add(email);active+=1;peak=max(peak,active)
        time.sleep(.005)
        with lock:active-=1;emails.remove(email)
    run=gate.decorate(authorize_registered_email)
    with ThreadPoolExecutor(max_workers=20) as pool:list(pool.map(run,['one@example.test']*10+['two@example.test']*10+['three@example.test']*10+['four@example.test']*10))
    assert peak<=3 and peak>=2 and not duplicates and gate.active==gate.inflight==gate.waiters==0

def test_task_snapshot_frozen_until_end_and_decrypt_failure_releases_slot(monkeypatch):
    gate,rows=gate_fixture(monkeypatch);entered=threading.Event();proceed=threading.Event();seen=[]
    def authorize_registered_email(email):
        seen.append(TASK_SETTINGS.get()['sayhi_model']);entered.set();proceed.wait(2);seen.append(TASK_SETTINGS.get()['sayhi_model'])
    run=gate.decorate(authorize_registered_email)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run,'one@example.test');assert entered.wait(1)
        rows[0]['value']='new-model';proceed.set();future.result()
    assert seen==['initial-model','initial-model'] and TASK_SETTINGS.get() is None
    monkeypatch.setattr(mysql,'revealed',lambda *a,**kw:(_ for _ in ()).throw(ValueError('invalid ciphertext')))
    with pytest.raises(ValueError):run('different@example.test')
    assert gate.active==gate.inflight==gate.waiters==0 and not gate.emails


def test_native_read_polling_cannot_consume_the_write_budget(native):
    """回归：后台轮询只能吃"读"额度，绝不能把"写"的 30 次/分钟挤掉。

    真实事故：代理池「测试」一直被拒「操作过于频繁」，因为页面 5 秒轮询
    （一个页面几个 hook 就有 30~70 次/分钟）和写操作共用同一个计数器，
    轮询先把写额度顶满，测试按钮从此必 429。
    """
    client,s,allowed,calls=native;headers={'Authorization':'Bearer fixture'}
    base='/api/v1/admin/invitation/native'
    for _ in range(35):
        assert client.get(base+'/api/accounts',headers=headers).status_code==200
    allowed.add('invitation.manage')
    assert client.post(base+'/api/settings/mail',headers=headers,json={}).status_code==200
    reads=[entry for entry in s.rate_buckets if entry[1]==240]
    writes=[entry for entry in s.rate_buckets if entry[1]==30]
    assert len(reads)==35 and len(writes)==1
    assert reads[0][0]=='invitation.native:actor:read' and writes[0][0]=='invitation.native:actor:write'
    assert reads[0][0]!=writes[0][0] and all(entry[2]==60 for entry in s.rate_buckets)


def test_credential_export_returns_complete_text_only_with_the_permission(native):
    """有权限的授权凭据导出必须拿到完整内容。

    回归：导出结果装在 `text` 里，而 `text` 同时在"日志字段"名单内，旧实现
    不看权限就 redact，于是 AT.txt / 账号密码 导出到手的是一堆占位文本。
    """
    client,s,allowed,calls=native;headers={'Authorization':'Bearer fixture'}
    path='/api/v1/admin/invitation/native/api/registered/export'
    # 导出是写操作：先给 manage，此时仍缺 invitation.credentials → 直接拒绝，
    # 连业务函数都不调用。
    allowed.add('invitation.manage')
    assert client.post(path,headers=headers,json={'format':'at','all':True}).status_code==403
    assert not calls
    allowed.add('invitation.credentials')
    response=client.post(path,headers=headers,json={'format':'at','all':True})
    assert response.status_code==200 and calls==['export']
    # native 端点的响应就是业务字典本身（不再套一层 data）。
    text=response.json()['text']
    assert text==EXPORT_TEXT,text
    assert 'hunter2' in text and 'eyJhbGciOiJub25lIn0' in text
    assert '[REDACTED]' not in text


def test_log_field_redaction_is_exempted_only_for_the_export_endpoint():
    """日志字段脱敏只在"已授权 + 导出端点"这一条窄路上放行。"""
    from smsinvit.invitation.router import cleaned
    payload={'text':EXPORT_TEXT,'error':'password=hunter2'}
    assert cleaned(payload,False)=={'text':redact(EXPORT_TEXT),'error':redact('password=hunter2')}
    assert cleaned(payload,True)=={'text':redact(EXPORT_TEXT),'error':redact('password=hunter2')}
    assert cleaned(payload,True,True)==payload


def test_sse_and_plain_reads_stay_redacted_even_with_credential_permission(native):
    """授权导出放行不等于全局放行：实时日志流与普通读接口继续脱敏。"""
    client,s,allowed,calls=native;headers={'Authorization':'Bearer fixture'}
    allowed.update(('invitation.credentials','invitation.manage'))
    base='/api/v1/admin/invitation/native'
    body=client.get(base+'/api/events',headers=headers).text
    frame=json.loads(next(line[6:] for line in body.splitlines() if line.startswith('data: ')))
    assert frame['refresh_token']=='***' and 'abc' not in body and 'short' not in body
    assert '[REDACTED]' in body


def test_the_invite_concurrency_setting_cannot_exceed_the_available_slots(native):
    """保存时就拒绝跑不起来的值，而不是让每个任务在门卫处失败。"""
    from smsinvit.invitation.permits import INVITE_SLOTS

    client,s,allowed,calls=native;headers={'Authorization':'Bearer fixture'}
    allowed.add('invitation.manage')
    path='/api/v1/admin/invitation/native/api/settings/invite'
    assert client.post(path,headers=headers,json={'cdk_max_concurrency':INVITE_SLOTS[1]}).status_code==200
    assert calls==['invite-setting']
    for value in (INVITE_SLOTS[1]+1, 32):
        assert client.post(path,headers=headers,json={'cdk_max_concurrency':value}).status_code==422
    assert calls==['invite-setting']

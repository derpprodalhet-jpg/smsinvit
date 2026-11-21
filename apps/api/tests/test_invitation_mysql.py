"""Real MySQL invitation security and atomicity tests; never uses source SQLite."""
import concurrent.futures,json,os,secrets,sys,threading,time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import URL
from smsinvit.app import Services,create_app
from smsinvit.config import Config
from smsinvit.db import Database
from smsinvit.errors import Error
from smsinvit.invitation import mysql
from smsinvit.security import Principal

ROOT=Path(__file__).resolve().parents[3]
@pytest.fixture(scope='module')
def inv():
    path=Path(os.environ.get('SMSINVIT_TEST_MYSQL_CREDENTIALS_FILE',str(ROOT/'data/test-private/mysql.json')))
    if not path.exists():pytest.skip('isolated MySQL credentials not configured')
    selected=json.loads(path.read_text())
    config=Config({'smsinvit.inventory-encryption-key':'a1'*32,'smsinvit.inventory-fingerprint-key':'invitation-only-test-key','smsinvit.jwt-secret':'test-invitation-jwt-over32characters','SMSINVIT_INVITATION_DATA_DIR':'/private/tmp/smsinvit-invitation-test'})
    db=Database(URL.create('mysql+pymysql',username=selected.get('user','root'),password=selected['password'],host=selected.get('host','127.0.0.1'),port=selected.get('port',13308),database=selected.get('database','smsinvit_test')))
    services=Services(db,config)
    from smsinvit.invitation.service import InvitationService
    previous=(mysql._services,mysql._runtime_owner,mysql._cache.copy(),mysql._redaction_values.copy())
    try:
        services.invitation=InvitationService(services)
        yield services.invitation
    finally:
        # Production deliberately forbids repository rebinding. This fixture
        # relinquishes its isolated module state before offline migration tests.
        mysql._services,mysql._runtime_owner,cache,redactions=previous
        mysql._cache.clear();mysql._cache.update(cache)
        mysql._redaction_values.clear();mysql._redaction_values.update(redactions)
        services.http.close();db.close()

def pool_account(inv,domain='example.test'):
    """预置一个**已注册 GPT**的号：兑换只发号池里的号，不再现场造邮箱。

    未注册的裸邮箱（status='available'）不算可兑换，交给用户只会让执行阶段必失败，
    所以这里直接按账号工厂的产出登记（已绑 2FA、已接码）。
    """
    email=uuid4().hex+'@'+domain
    inv.db.execute("INSERT INTO invitation_v2_outlook_accounts(email,password,client_id,refresh_token,kind,status,phone_bound_ok,imported_at) VALUES (?,?,?,?,?,?,?,?)",email,mysql.protected('Private-password-'+uuid4().hex),mysql.protected('client'),mysql.protected('refresh-'+uuid4().hex),'outlook','registered',1,time.time())
    return email

def card(inv,suffix='',mail=True):
    code='TEST-INV-'+uuid4().hex.upper()+suffix
    inv.repo.create_cdk_codes([code],batch='invitation-test')
    if mail:
        pool_account(inv)
    return code

def test_encryption_translation_counts_and_search(inv):
    code=card(inv)
    assert inv.repo.get_cdk_code(code)['code']==code
    assert code not in json.dumps(inv.db.one('SELECT * FROM invitation_v2_cdk_codes WHERE code=?',mysql.marker(code)))
    assert any(row['code']==code for row in inv.repo.list_cdk_codes(search=code[7:18],limit=1000))
    email=uuid4().hex+'@example.test';inv.repo.save_registered({'email':email,'access_token':'secretaccess123','refresh_token':'refresh-long-token-456'})
    row=next(r for r in inv.repo.list_registered(limit=10000) if r['email']==email)
    assert row['at_len']==15 and row['rt_len']==22

def test_invalid_code_does_not_write_or_allocate(inv,monkeypatch):
    before=inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_secret_values')
    monkeypatch.setattr(inv.alloc,'pool_available',lambda *a,**k:100)
    result=inv.redeem(['NO-SUCH-'+uuid4().hex])
    assert result['items'][0]['error']['code']=='INVITATION_CDK_NOT_FOUND'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_secret_values')==before

def test_batch_rollback_leaves_all_cards_unused(inv,monkeypatch):
    codes=[card(inv),card(inv)];original=inv.alloc.redeem_mailbox;calls=[]
    def fail(code,**kwargs):
        calls.append(code)
        if len(calls)==2:raise Error('TEST_FAIL','mail failed',409)
        return original(code,**kwargs)
    monkeypatch.setattr(inv.alloc,'redeem_mailbox',fail)
    with pytest.raises(Error):inv.redeem(codes)
    assert all(inv.repo.get_cdk_code(c)['status']=='unused' for c in codes)
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id IN (?,?)',*[mysql.marker(c) for c in codes])==0

def test_100_redemptions_bind_once_enqueue_once(inv,monkeypatch):
    code=card(inv)
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:results=list(pool.map(lambda _:inv.redeem([code]),range(100)))
    successes=[r['items'][0] for r in results if not r['items'][0].get('error')]
    assert successes and len({item['id'] for item in successes})==1
    assert len({item['email'] for item in successes})==1
    item=successes[0];entry={'id':item['id'],'sessionSecret':item['sessionSecret']}
    inv.action([entry],'invite-sent')
    # Each isolated Services fixture needs its own controller closure. Reusing
    # the module singleton could still guard the previous fixture's executor.
    controller=inv.cdk.CdkController()
    monkeypatch.setattr(inv.cdk,'CONTROLLER',controller)
    inv._secure_controller()
    # Model a ready executor without spawning workers or executing any provider
    # task. Keep the real admission guard and enqueue transaction in this test.
    monkeypatch.setattr(inv,'_executor_enabled',True)
    monkeypatch.setattr(inv,'_started',True)
    monkeypatch.setattr(inv,'_stopping',False)
    monkeypatch.setattr(inv,'owns_executor_lock',lambda:True)
    monkeypatch.setattr(inv,'holds_executor_lock',lambda:True)
    monkeypatch.setattr(controller,'_started',True)
    monkeypatch.setattr(controller,'ensure_workers',lambda:1)
    monkeypatch.setattr(controller,'worker_alive',lambda:1)
    monkeypatch.setattr(controller,'can_start',lambda:True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:starts=list(pool.map(lambda _:inv.action([entry],'start'),range(100)))
    assert all(not result['items'][0].get('error') for result in starts)
    assert inv.db.scalar("SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?",mysql.marker(code))==1
    assert inv.db.scalar("SELECT COUNT(*) FROM invitation_v2_outlook_accounts WHERE cdk_code=?",mysql.marker(code))==1
    assert len(controller.queued_job_ids())==controller._q.qsize()==1

def test_cross_session_and_service_denied_before_db_write(inv):
    code=card(inv);item=inv.redeem([code])['items'][0]
    before=inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs')
    for secret,id in [('',item['id']),(item['sessionSecret'],str(uuid4())),(inv.s.tokens.issue('sms-session',item['id']),item['id'])]:
        result=inv.action([{'id':id,'sessionSecret':secret}],'start')
        assert result['items'][0]['error']['code'].startswith('SESSION_SECRET')
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs')==before

def test_native_routes_anonymous_and_low_permission(inv):
    from fastapi.testclient import TestClient
    app=create_app(inv.s.config,inv.db,services=inv.s,include_domains=False,load_runtime=False)
    from smsinvit.invitation.router import build_router
    app.include_router(build_router(inv.s))
    with TestClient(app) as client:
        for path in ['/api/accounts','/api/cdk/list','/api/settings/invite','/api/registered']:
            result=client.get('/api/v1/admin/invitation/native'+path)
            assert result.status_code==401

@pytest.fixture
def platform_card(inv):
    sku,item=str(uuid4()),str(uuid4());code='TEST-PLATFORM-'+uuid4().hex.upper()
    inv.db.execute('INSERT INTO skus(id,code,name,attributes_json) VALUES (?,?,?,?)',sku,'platform-fixture-'+uuid4().hex,'Invitation platform fixture',json.dumps({'productKind':'CODEX_INVITE_REDEEM'}))
    inv.db.execute('INSERT INTO inventory_items(id,sku_id,secret_ciphertext,secret_fingerprint,status,updated_at) VALUES (?,?,?,?,?,UTC_TIMESTAMP(3))',item,sku,inv.s.codec.encrypt(code),inv.s.codec.fingerprint(code),'ISSUED')
    with inv.db.transaction():inv.register_issued_key(item,sku,{'productKind':'CODEX_INVITE_REDEEM','cdkValidDays':7},code)
    try:yield item,sku,code
    finally:
        marker=mysql.marker(code)
        inv.db.execute('DELETE FROM invitation_v2_sessions WHERE code_id=?',marker)
        inv.db.execute('DELETE h FROM invitation_v2_task_holds h JOIN invitation_v2_cdk_jobs j ON j.job_id=h.job_id WHERE j.code=?',marker)
        inv.db.execute('DELETE FROM invitation_v2_cdk_jobs WHERE code=?',marker)
        inv.db.execute('DELETE FROM invitation_v2_cdk_codes WHERE code=?',marker)
        inv.db.execute('DELETE FROM invitation_v2_platform_keys WHERE inventory_item_id=?',item)
        inv.db.execute('DELETE FROM inventory_items WHERE id=?',item)
        inv.db.execute('DELETE FROM skus WHERE id=?',sku)

def test_queued_inventory_invalidation_is_rejected_and_unused_disabled_or_expired_calls_no_provider(inv,platform_card,monkeypatch):
    item,sku,code=platform_card;actor=Principal(str(uuid4()),'test-session',('SUPER_ADMIN',))
    inv.repo.set_cdk_status(code,'queued')
    with pytest.raises(Error) as error:inv.s.inventory.invalidate(item,actor)
    assert error.value.code=='INVITATION_RUNNING'
    assert inv.db.scalar('SELECT status FROM inventory_items WHERE id=?',item)=='ISSUED'
    assert inv.repo.get_cdk_code(code)['status']=='queued'
    inv.repo.set_cdk_status(code,'unused');inv.db.execute("UPDATE inventory_items SET status='INVALID' WHERE id=?",item)
    monkeypatch.setattr(inv.alloc,'kind_is_pooled',lambda *a:False);calls=[]
    import mail_providers
    monkeypatch.setattr(mail_providers,'create_mail_provider',lambda *a: calls.append(a))
    result=inv.redeem([code])
    assert result['items'][0]['error']['code']=='INVITATION_CDK_DISABLED' and not calls
    inv.db.execute("UPDATE inventory_items SET status='ISSUED' WHERE id=?",item)
    inv.db.execute('UPDATE invitation_v2_platform_keys SET expires_at=UTC_TIMESTAMP(3)-INTERVAL 1 DAY WHERE inventory_item_id=?',item)
    result=inv.redeem([code])
    assert result['items'][0]['error']['code']=='INVITATION_CDK_EXPIRED' and not calls

def test_native_mutation_cannot_change_active_mailbox_and_other_mailbox_remains_editable(inv,monkeypatch):
    from fastapi.testclient import TestClient
    from smsinvit.invitation.router import build_router
    from smsinvit.invitation.permits import Permits
    email=uuid4().hex+'@example.test';other=uuid4().hex+'@example.test'
    inv.repo.save_registered({'email':email,'password':'original'});inv.repo.save_registered({'email':other,'password':'other-original'})
    permit=Permits(inv.db,inv.s.codec);slot=permit.claim(email,'private-job',3)
    assert slot
    actor=Principal(str(uuid4()),'session',('SUPER_ADMIN',));audit=[]
    monkeypatch.setattr(inv.s.auth,'authenticate',lambda *a:actor)
    monkeypatch.setattr(inv.s.audit,'record',lambda *a:audit.append(a))
    app=create_app(inv.s.config,inv.db,services=inv.s,include_domains=False,load_runtime=False);app.include_router(build_router(inv.s))
    headers={'Authorization':'Bearer fixture'};url='/api/v1/admin/invitation/native/api/registered/update_credentials'
    try:
        with TestClient(app) as client:
            denied=client.post(url,headers=headers,json={'email':email,'password':'replacement'})
            assert denied.status_code==409
            assert inv.repo.get_registered(email)['password']=='original'
            allowed=client.post(url,headers=headers,json={'email':other,'password':'other-replacement'})
            assert allowed.status_code==200 and inv.repo.get_registered(other)['password']=='other-replacement'
            assert client.post('/api/v1/admin/invitation/native/api/registered/bulk_delete',headers=headers,json={'all':True}).status_code==409
            assert inv.repo.get_registered(email)['password']=='original'
    finally:
        permit.release(slot);inv.repo.delete_registered(email);inv.repo.delete_registered(other)

def test_failed_unknown_legacy_hold_cannot_replay_through_start_or_account_authorization(inv,monkeypatch):
    from smsinvit.invitation.permits import Permits
    code=card(inv);item=inv.redeem([code])['items'][0];inv.repo.mark_invite_sent(code);inv.repo.set_cdk_status(code,'failed')
    job='held-'+uuid4().hex;inv.repo.create_cdk_job(job,code,email=item['email']);inv.repo.set_cdk_job(job,status='failed')
    inv.db.execute("INSERT INTO invitation_v2_task_holds(job_id,original_status,reason) VALUES (?,'FAILED','LEGACY_UNCERTAIN')",mysql.marker(job))
    monkeypatch.setattr(inv.cdk.CONTROLLER,'can_start',lambda:(_ for _ in ()).throw(AssertionError('No provider check is allowed')))
    before=inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(code))
    try:
        result=inv.action([{'id':item['id'],'sessionSecret':item['sessionSecret']}],'start')
        assert result['items'][0]['error']['code']=='INVITATION_REVIEW_REQUIRED'
        result=inv.action([{'id':item['id'],'sessionSecret':item['sessionSecret']}],'invite-sent')
        assert result['items'][0]['error']['code']=='INVITATION_REVIEW_REQUIRED'
        assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(code))==before
        with pytest.raises(Error) as error:Permits(inv.db,inv.s.codec).claim(item['email'],'new-registration',3)
        assert error.value.code=='INVITATION_REVIEW_REQUIRED'
    finally:
        inv.db.execute('DELETE FROM invitation_v2_task_holds WHERE job_id=?',mysql.marker(job));inv.db.execute('DELETE FROM invitation_v2_cdk_jobs WHERE job_id=?',mysql.marker(job))

def test_native_cli_prompt_permission_change_is_atomic_with_real_mysql_settings(inv,monkeypatch):
    from fastapi.testclient import TestClient
    from smsinvit.app import create_app
    from smsinvit.invitation.router import build_router
    from smsinvit.security import Principal
    keys=('sayhi_prompt','cli_timeout');old={key:inv.repo.get_setting(key,None) for key in keys}
    audit=[];upstream=[];user_id=str(uuid4());role_id=str(uuid4());suffix=uuid4().hex[:12];role_code='CLI_TEST_'+suffix
    actor=Principal(user_id,'session',(role_code,))
    monkeypatch.setattr(inv.s.auth,'authenticate',lambda *a:actor)
    monkeypatch.setattr(inv.s.limiter,'check',lambda *a:None)
    monkeypatch.setattr(inv.s.audit,'record',lambda *a:audit.append(a))
    monkeypatch.setattr(inv,'action',lambda *a:upstream.append(a))
    try:
        with inv.db.transaction():
            inv.db.execute('INSERT INTO admin_users(id,username,email,password_hash) VALUES(?,?,?,?)',user_id,'cli_'+suffix,'cli_'+suffix+'@example.test',inv.s.codec.hash_password('isolated-cli-fixture-password'))
            inv.db.execute('INSERT INTO roles(id,code,name,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP(3))',role_id,role_code,'CLI permission fixture')
            inv.db.execute('INSERT INTO admin_user_roles(id,admin_user_id,role_id) VALUES(?,?,?)',str(uuid4()),user_id,role_id)
            for code in ('invitation.read','invitation.manage'):
                permission_id=inv.db.scalar('SELECT id FROM permissions WHERE code=?',code);assert permission_id
                inv.db.execute("INSERT INTO role_permissions(id,role_id,permission_id,scope_type) VALUES(?,?,?,'ALL')",str(uuid4()),role_id,permission_id)
        inv.repo.set_setting('sayhi_prompt','approved fixture greeting');inv.repo.set_setting('cli_timeout','180')
        before=inv.db.all('SELECT `key`,value FROM invitation_v2_settings WHERE `key` IN (?,?) ORDER BY `key`',*keys)
        app=create_app(inv.s.config,inv.db,services=inv.s,include_domains=False,load_runtime=False);app.include_router(build_router(inv.s))
        path='/api/v1/admin/invitation/native/api/settings/invite';headers={'Authorization':'Bearer fixture'}
        with TestClient(app) as client:
            denied=client.post(path,headers=headers,json={'sayhi_prompt':'unapproved instruction','cli_timeout':240})
            assert denied.status_code==403 and denied.json()['error']['code']=='PERMISSION_DENIED'
            assert inv.db.all('SELECT `key`,value FROM invitation_v2_settings WHERE `key` IN (?,?) ORDER BY `key`',*keys)==before
            assert not audit and not upstream
            normal=client.post(path,headers=headers,json={'sayhi_prompt':'approved fixture greeting','cli_timeout':240})
            assert normal.status_code==200 and inv.repo.get_setting('sayhi_prompt')=='approved fixture greeting'
            assert inv.repo.get_setting('cli_timeout')=='240' and len(audit)==1 and not upstream
            credential_id=inv.db.scalar("SELECT id FROM permissions WHERE code='invitation.credentials'");assert credential_id
            inv.db.execute("INSERT INTO role_permissions(id,role_id,permission_id,scope_type) VALUES(?,?,?,'ALL')",str(uuid4()),role_id,credential_id)
            privileged=client.post(path,headers=headers,json={'sayhi_prompt':'approved replacement'})
            assert privileged.status_code==200 and inv.repo.get_setting('sayhi_prompt')=='approved replacement'
            assert len(audit)==2 and not upstream
    finally:
        for key,value in old.items():
            if value is None:inv.db.execute('DELETE FROM invitation_v2_settings WHERE `key`=?',key)
            else:inv.repo.set_setting(key,value)
        inv.db.execute('DELETE FROM admin_users WHERE id=?',user_id);inv.db.execute('DELETE FROM roles WHERE id=?',role_id)


@pytest.fixture
def manual_completion_card(inv,platform_card):
    item,sku,code=platform_card;email=uuid4().hex+'@manual.example.test';run=uuid4().hex;old_job=uuid4().hex
    marker=mysql.marker(code);stamp=time.time()
    assert inv.repo.claim_cdk_mailbox(code,'cf_temp',pooled=False,email=email)
    inv.db.execute('UPDATE invitation_v2_outlook_accounts SET cdk_bound_at=? WHERE email=?',stamp-40,email)
    inv.repo.set_cdk_status(code,'failed',email=email,redeemed_at=stamp-40,finished_at=stamp-20,error='fixture failure')
    inv.repo.create_run(run,email,'',kind='codex_auth')
    inv.db.execute('UPDATE invitation_v2_runs SET started_at=? WHERE run_id=?',stamp-30,mysql.marker(run))
    inv.repo.save_codex_auth(email,account_id='manual-fixture-account',cli_completed_account_id='manual-fixture-account',
                             invite_processed_at=stamp-35,cli_completed_at=stamp-10)
    inv.repo.create_cdk_job(old_job,code,email=email);inv.repo.set_cdk_job(old_job,status='failed',stage='failed',finished_at=stamp-20)
    try:yield {'email':email,'run':run,'code':code,'old_job':old_job,'stamp':stamp}
    finally:
        inv.db.execute('DELETE FROM invitation_v2_manual_run_links WHERE run_id=?',mysql.marker(run))
        inv.db.execute('DELETE FROM invitation_v2_runs WHERE run_id=?',mysql.marker(run))
        inv.db.execute('DELETE FROM invitation_v2_codex_auth WHERE email=?',email)
        inv.db.execute('DELETE FROM invitation_v2_outlook_accounts WHERE email=?',email)
        inv.db.execute('DELETE FROM invitation_v2_cdk_events WHERE code=?',marker)


def test_manual_success_has_durable_link_inside_permit_and_atomic_card_run_completion(inv,manual_completion_card):
    x=manual_completion_card;calls=[]
    def authorize_registered_email(email,*,run_id):
        calls.append(1)
        assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits WHERE email=? AND owner_token IS NOT NULL',email)==1
        assert inv._manual_link(run_id)['code_id']==mysql.marker(x['code'])
        return {'ok':True,'result':{'export':{'attempted':False}}}
    fn=inv.gate.decorate(inv.managed_manual_authorizer(authorize_registered_email))
    result=fn(x['email'],run_id=x['run'])
    assert result['ok'] and result['cdk_sync_status']=='done' and calls==[1]
    assert inv.repo.get_cdk_code(x['code'])['status']=='done'
    assert inv.db.scalar('SELECT status FROM invitation_v2_runs WHERE run_id=?',mysql.marker(x['run']))=='done'
    assert inv.db.scalar("SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=? AND status='failed'",mysql.marker(x['code']))==1
    assert inv.db.scalar("SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=? AND status='done' AND progress=100",mysql.marker(x['code']))==1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits WHERE email=? AND owner_token IS NOT NULL',x['email'])==0
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x['code']))==0


def test_manual_completion_concurrent_callbacks_create_one_new_job_and_preserve_failed_history(inv,manual_completion_card):
    x=manual_completion_card;inv.begin_manual_authorization(x['email'],x['run'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        outcomes=list(pool.map(lambda _:inv.finalize_manual_authorization(x['email'],x['run'],{'ok':True,'result':{}}),range(25)))
    assert all(outcome['ok'] and outcome['cdk_sync_status']=='done' for outcome in outcomes)
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x['code']))==2
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_events WHERE code=?',mysql.marker(x['code']))==1
    assert inv.repo.get_cdk_job(x['old_job'])['status']=='failed'


def test_manual_completion_and_historical_reconciliation_share_lock_order(inv,manual_completion_card):
    x=manual_completion_card;inv.begin_manual_authorization(x['email'],x['run'])
    assert inv.finalize_manual_authorization(x['email'],x['run'],{'ok':True,'result':{}})['cdk_sync_status']=='done'
    def complete(index):
        if index%2:
            return inv.reconcile_manual_completion(x['email'],x['run'],apply=True)
        return inv.finalize_manual_authorization(x['email'],x['run'],{'ok':True,'result':{}})
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        outcomes=list(pool.map(complete,range(60)))
    assert all(outcome['cdk_sync_status']=='done' for outcome in outcomes)
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x['code']))==2
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_events WHERE code=?',mysql.marker(x['code']))==1
    assert inv.repo.get_cdk_job(x['old_job'])['status']=='failed'


def test_manual_card_event_failure_rolls_back_real_repository_writes_keeps_successful_run(inv,manual_completion_card,monkeypatch):
    x=manual_completion_card;inv.begin_manual_authorization(x['email'],x['run'])
    monkeypatch.setattr(inv.repo,'add_cdk_event',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('fixture event rollback')))
    result=inv.finalize_manual_authorization(x['email'],x['run'],{'ok':True,'result':{}})
    assert result['ok'] and result['cdk_sync_status']=='failed'
    assert inv.repo.get_cdk_code(x['code'])['status']=='failed'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x['code']))==1
    assert inv.db.scalar('SELECT status FROM invitation_v2_runs WHERE run_id=?',mysql.marker(x['run']))=='done'
    assert inv._manual_link(x['run'])['completed_job_id'] is None


def test_manual_binding_conflict_keeps_successful_run_and_does_not_complete_old_card(inv,manual_completion_card):
    x=manual_completion_card;inv.begin_manual_authorization(x['email'],x['run'])
    inv.db.execute('UPDATE invitation_v2_outlook_accounts SET cdk_code=NULL WHERE email=?',x['email'])
    result=inv.finalize_manual_authorization(x['email'],x['run'],{'ok':True,'result':{}})
    assert result['ok'] and result['cdk_sync_status']=='failed'
    assert inv.repo.get_cdk_code(x['code'])['status']=='failed'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x['code']))==1
    run={'run_id':x['run'],'email':x['email'],'kind':'codex_auth','status':'done'}
    wire=inv.attach_manual_sync_status(run)
    assert wire['cdk_sync_status']=='failed' and wire['cdk_sync_error']
    assert x['code'] not in json.dumps(wire) and 'code_id' not in wire


def test_manual_permit_blocks_native_card_mutations_and_enqueue_and_historical_repair_is_idempotent(inv,manual_completion_card,monkeypatch):
    from fastapi.testclient import TestClient
    from smsinvit.invitation.permits import Permits
    from smsinvit.invitation.router import build_router
    x=manual_completion_card;p=Permits(inv.db,inv.s.codec);slot=p.claim(x['email'],x['run'],3);assert slot
    actor=Principal(str(uuid4()),'fixture-session',('SUPER_ADMIN',))
    monkeypatch.setattr(inv.s.auth,'authenticate',lambda *a:actor)
    monkeypatch.setattr(inv.s.audit,'record',lambda *a:None)
    app=create_app(inv.s.config,inv.db,services=inv.s,include_domains=False,load_runtime=False);app.include_router(build_router(inv.s))
    try:
        with pytest.raises(Error) as error:inv.cdk.CONTROLLER.enqueue(x['code'])
        assert error.value.code=='INVITATION_RUNNING'
        with TestClient(app) as client:
            for action in ('reset','void','unbind','delete'):
                body={'codes':[x['code']]} if action=='delete' else {'code':x['code']}
                response=client.post('/api/v1/admin/invitation/native/api/cdk/'+action,headers={'Authorization':'Bearer fixture'},json=body)
                assert response.status_code==409 and response.json()['error']['code']=='INVITATION_RUNNING'
    finally:p.release(slot)
    inv.repo.finish_run(x['run'],'done')
    before=inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x['code']))
    preview=inv.reconcile_manual_completion(x['email'],x['run'])
    assert preview['eligible'] and not preview['applied'] and inv._manual_link(x['run']) is None
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x['code']))==before
    assert inv.reconcile_manual_completion(x['email'],x['run'],apply=True)['applied']
    assert inv.reconcile_manual_completion(x['email'],x['run'],apply=True)['applied']
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x['code']))==before+1


@pytest.fixture
def sms_atomic_cases(inv):
    """Namespaced synthetic orders only; no supplier or live account is contacted."""
    cases={'runs':set(),'phones':set()}
    yield cases
    for run in cases['runs']:
        inv.db.execute('DELETE FROM invitation_v2_sms_orders WHERE run_id=?',mysql.marker(run))
        inv.db.execute('DELETE FROM invitation_v2_sms_budgets WHERE run_id=?',mysql.marker(run))
    for fingerprint in cases['phones']:
        inv.db.execute('DELETE FROM invitation_v2_sms_phone_uses WHERE phone_fingerprint=?',fingerprint)


def test_sms_mysql_hundred_concurrent_reservations_keep_three_paid_slots(inv,sms_atomic_cases):
    from smsinvit.invitation import sms_state
    run=uuid4().hex;sms_atomic_cases['runs'].add(run)
    def reserve(index):
        with sms_state.task_run(run):
            try:return sms_state.reserve_rental(('smsbower','herosms','smspool')[index%3],'getNumber')
            except sms_state.SmsBudgetExhausted:return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
        results=list(pool.map(reserve,range(100)))
    rentals=[value for value in results if value]
    assert len(rentals)==3 and len(set(rentals))==3
    assert inv.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets WHERE run_id=?',mysql.marker(run))==3
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_orders WHERE run_id=?',mysql.marker(run))==3
    for index,rental in enumerate(rentals):
        sms_state.rental_created(rental,uuid4().hex,'+1555'+str(index+1000000))
        sms_state.rental_no_order(rental)
    assert inv.db.scalar("SELECT COUNT(*) FROM invitation_v2_sms_orders WHERE run_id=? AND status='created'",mysql.marker(run))==3
    assert inv.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets WHERE run_id=?',mysql.marker(run))==3


def test_sms_mysql_explicit_no_order_releases_once_unknown_and_created_do_not(inv,sms_atomic_cases):
    from smsinvit.invitation import sms_state
    run=uuid4().hex;sms_atomic_cases['runs'].add(run)
    with sms_state.task_run(run):
        rentals=[sms_state.reserve_rental(platform,'getNumber') for platform in ('smsbower','herosms','smspool')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
        list(pool.map(lambda _:sms_state.rental_no_order(rentals[0]),range(100)))
    assert inv.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets WHERE run_id=?',mysql.marker(run))==2
    assert inv.db.scalar("SELECT COUNT(*) FROM invitation_v2_sms_orders WHERE run_id=? AND status='no_order'",mysql.marker(run))==1
    sms_state.rental_unknown(rentals[1],'fixture uncertain response')
    sms_state.rental_created(rentals[2],uuid4().hex,'+15551234567')
    for _ in range(3):
        sms_state.rental_no_order(rentals[1]);sms_state.rental_no_order(rentals[2])
    assert inv.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets WHERE run_id=?',mysql.marker(run))==2
    with sms_state.task_run(run):
        replacement=sms_state.reserve_rental('smsbower','getNumberV2')
        with pytest.raises(sms_state.SmsBudgetExhausted):sms_state.reserve_rental('herosms','getNumber')
    assert replacement and inv.db.scalar('SELECT reserved_count FROM invitation_v2_sms_budgets WHERE run_id=?',mysql.marker(run))==3


def test_sms_mysql_cross_platform_same_phone_is_claimed_once_under_concurrency(inv,sms_atomic_cases):
    from smsinvit.invitation import sms_state
    phone='+1555'+str(int(uuid4().hex[:6],16)%10000000).zfill(7)
    fingerprint=sms_state.phone_fingerprint(phone);sms_atomic_cases['phones'].add(fingerprint)
    runs=[uuid4().hex for _ in range(60)];sms_atomic_cases['runs'].update(runs)
    def send(index):
        with sms_state.task_run(runs[index]):
            platform=('smsbower','herosms','smspool')[index%3]
            rental=sms_state.reserve_rental(platform,'getNumber')
            sms_state.rental_created(rental,uuid4().hex,phone)
            return sms_state.claim_phone(phone if index%2 else '00'+phone[1:])
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
        outcomes=list(pool.map(send,range(len(runs))))
    assert sum(outcomes)==1
    rows=inv.db.all('SELECT * FROM invitation_v2_sms_phone_uses WHERE phone_fingerprint=?',fingerprint)
    assert len(rows)==1 and phone not in json.dumps(rows)
    assert not sms_state.import_phone(phone,'fixture historical use')


def test_sms_mysql_settlement_lease_single_owner_reclaim_and_late_results_are_fenced(inv,sms_atomic_cases):
    from smsinvit.invitation import sms_state
    run=uuid4().hex;activation=uuid4().hex;sms_atomic_cases['runs'].add(run)
    with sms_state.task_run(run):rental=sms_state.reserve_rental('smsbower','getNumber')
    sms_state.rental_created(rental,activation,'+15557654321')
    sms_state.order_state('smsbower',activation,'finish_pending','fixture pending acknowledgement')
    inv.db.execute('UPDATE invitation_v2_sms_orders SET next_at=? WHERE rental_id=?',time.time()-1,rental)
    def compete():
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            leases=list(pool.map(lambda _:sms_state.settlement_claim(rental),range(50)))
        owners=[lease for lease in leases if lease]
        assert len(owners)==1
        return owners[0]
    first=compete()
    assert inv.db.scalar('SELECT attempts FROM invitation_v2_sms_orders WHERE rental_id=?',rental)==1
    assert sms_state.settlement_claim(rental) is None
    inv.db.execute('UPDATE invitation_v2_sms_orders SET next_at=? WHERE rental_id=?',time.time()-1,rental)
    second=compete();assert second!=first
    sms_state.settlement_result(rental,True,claim=first)
    row=inv.db.one('SELECT status,lease_token,attempts FROM invitation_v2_sms_orders WHERE rental_id=?',rental)
    assert row=={'status':'finish_processing','lease_token':second,'attempts':2}
    sms_state.settlement_result(rental,False,'fixture temporary finish error',claim=second)
    assert inv.db.scalar('SELECT status FROM invitation_v2_sms_orders WHERE rental_id=?',rental)=='finish_pending'
    inv.db.execute('UPDATE invitation_v2_sms_orders SET next_at=? WHERE rental_id=?',time.time()-1,rental)
    third=compete();assert third not in (first,second)
    sms_state.settlement_result(rental,False,'fixture late error',claim=second)
    assert inv.db.scalar('SELECT lease_token FROM invitation_v2_sms_orders WHERE rental_id=?',rental)==third
    sms_state.settlement_result(rental,True,claim=third)
    sms_state.settlement_result(rental,False,'fixture late terminal error',claim=first)
    sms_state.order_state('smsbower',activation,'cancel_pending')
    assert inv.db.scalar('SELECT status FROM invitation_v2_sms_orders WHERE rental_id=?',rental)=='completed'
    assert inv.db.scalar('SELECT lease_token FROM invitation_v2_sms_orders WHERE rental_id=?',rental) is None
    assert inv.db.scalar('SELECT attempts FROM invitation_v2_sms_orders WHERE rental_id=?',rental)==3


def test_sms_mysql_crashed_final_settlement_lease_becomes_review_and_late_success_cannot_override(inv,sms_atomic_cases):
    from smsinvit.invitation import sms_state
    run=uuid4().hex;activation=uuid4().hex;sms_atomic_cases['runs'].add(run)
    with sms_state.task_run(run):rental=sms_state.reserve_rental('smspool','getNumber')
    sms_state.rental_created(rental,activation,'+15559876543')
    sms_state.order_state('smspool',activation,'finish_pending')
    lease=None
    for _ in range(3):
        inv.db.execute('UPDATE invitation_v2_sms_orders SET next_at=? WHERE rental_id=?',time.time()-1,rental)
        lease=sms_state.settlement_claim(rental);assert lease
    inv.db.execute('UPDATE invitation_v2_sms_orders SET next_at=? WHERE rental_id=?',time.time()-1,rental)
    sms_state.settlement_due()
    assert inv.db.scalar('SELECT status FROM invitation_v2_sms_orders WHERE rental_id=?',rental)=='finish_review'
    assert inv.db.scalar('SELECT lease_token FROM invitation_v2_sms_orders WHERE rental_id=?',rental) is None
    assert sms_state.settlement_claim(rental) is None
    sms_state.settlement_result(rental,True,claim=lease)
    assert inv.db.scalar('SELECT status FROM invitation_v2_sms_orders WHERE rental_id=?',rental)=='finish_review'


@pytest.fixture
def redemption_receipt(inv,platform_card,monkeypatch,request):
    import mail_providers
    from types import SimpleNamespace
    item,sku,code=platform_card
    # 兑换只从号池取"已注册 GPT"的号，不再现场造邮箱。夹具在池里备好账号，
    # 并把"取号"这一步包起来：case.calls 记录真正发生的取号次数（等价于旧断言里
    # 的"远端造号次数"），case.hook 仍是可用的中断点，用来把并发卡在取号那一瞬。
    # 需要一次兑换多张卡的用例用 @pytest.mark.parametrize(..., indirect=['redemption_receipt'])
    # 指定池内账号数量。
    pool_size=int(getattr(request,'param',None) or 1)
    case=SimpleNamespace(code=code,attempts=set(),addresses=[pool_account(inv,'receipt.example.test') for _ in range(pool_size)],calls=[],hook=None)
    def attempt():
        value=str(uuid4());case.attempts.add(value)
        return value,'e1'*32
    case.attempt=attempt
    monkeypatch.setenv('SMSINVIT_INVITATION_ALLOWED_HOSTS','mail.fixture.example:443')
    monkeypatch.setattr(inv.repo,'get_mail_settings',lambda:{'mail_source':'cf_temp','cf_api_url':'https://mail.fixture.example','cf_domain':'example.test','cf_admin_token':'synthetic-fixture-token'})
    def mailbox():
        case.calls.append(1)
        if case.hook:case.hook()
        address=uuid4().hex+'@receipt.example.test';case.addresses.append(address)
        return address
    monkeypatch.setattr(mail_providers,'create_mail_provider',lambda *a:SimpleNamespace(create_mailbox=mailbox))
    allocate=inv.alloc.redeem_mailbox
    def claim(code_value,settings=None):
        case.calls.append(1)
        # 取号事务内部的中断点：只在确实需要"取号那一瞬"改状态的用例里挂上，
        # 不能在这里做需要另开事务的断言，也不能在这里等别的线程（会持事务自锁）。
        if case.hook:case.hook()
        return allocate(code_value,settings=settings)
    monkeypatch.setattr(inv.alloc,'redeem_mailbox',claim)
    try:yield case
    finally:
        for attempt_id in case.attempts:
            inv.db.execute('DELETE FROM invitation_v2_redemption_attempt_cards WHERE attempt_id=?',attempt_id)
            inv.db.execute('DELETE FROM invitation_v2_redemption_attempts WHERE attempt_id=?',attempt_id)
        for email in case.addresses:
            inv.db.execute('DELETE FROM invitation_v2_outlook_accounts WHERE email=?',email)
        inv.db.execute('DELETE FROM invitation_v2_cdk_events WHERE code=?',mysql.marker(code))


def test_redemption_mysql_recovery_replays_original_session_and_live_status_without_create(inv,redemption_receipt):
    x=redemption_receipt;attempt,secret=x.attempt()
    first=inv.redeem([x.code],attempt,secret)
    inv.repo.set_cdk_status(x.code,'done')
    recovered=inv.redemption_attempt_status(attempt,secret)
    assert recovered['attemptStatus']=='completed' and recovered['items'][0]['status']=='done'
    assert recovered['items'][0]['id']==first['items'][0]['id']
    assert recovered['items'][0]['sessionSecret']==first['items'][0]['sessionSecret']
    assert inv.redeem([x.code.lower()],attempt,secret)==recovered and x.calls==[1]
    stored=json.dumps(inv.db.one('SELECT * FROM invitation_v2_redemption_attempts WHERE attempt_id=?',attempt),default=str)
    assert secret not in stored and x.code not in stored and 'sessionSecret' not in stored
    assert recovered['canRedeem'] and recovered['canStart'] is False


def test_redemption_mysql_concurrent_same_card_creates_one_remote_mailbox(inv,redemption_receipt):
    x=redemption_receipt;attempt,secret=x.attempt()
    # 池里多备几个号，跟随请求才有机会走到"已有 pending 领取"那一步；
    # 取号点被夹具计数，这里验证的是并发下**只发生一次取号**。
    for _ in range(4):pool_account(inv,'receipt.example.test')
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        # 同一个领取请求号被并发重复提交时，落败方会明确收到"正在处理"，
        # 这是允许的公开结果；关键是只发生一次取号。
        def submit(_):
            try:return inv.redeem([x.code],attempt,secret)
            except Error as error:
                assert error.code=='INVITATION_REDEMPTION_BUSY'
                return {'attemptStatus':'busy','items':[]}
        results=list(pool.map(submit,range(61)))
    completed=[r for r in results if r['attemptStatus']=='completed']
    assert completed and x.calls==[1]
    assert all(r['attemptStatus'] in {'completed','pending','rejected','busy'} for r in results)
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))==1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_outlook_accounts WHERE cdk_code=?',mysql.marker(x.code))==1


def test_redemption_mysql_changed_proof_or_codes_never_exposes_existing_result(inv,redemption_receipt):
    x=redemption_receipt;attempt,secret=x.attempt();inv.redeem([x.code],attempt,secret)
    for codes,key,expected in [([x.code],'ff'*32,'INVITATION_ATTEMPT_SECRET_INVALID'),(['SYNTHETIC-OTHER'],secret,'INVITATION_ATTEMPT_CONFLICT')]:
        with pytest.raises(Error) as error:inv.redeem(codes,attempt,key)
        assert error.value.code==expected
    failures=[]
    for identity,key in [(attempt,'ff'*32),(str(uuid4()),secret)]:
        with pytest.raises(Error) as error:inv.redemption_attempt_status(identity,key)
        failures.append((error.value.code,error.value.message,error.value.status))
    assert failures[0]==failures[1] and failures[0][2]==404 and x.calls==[1]


def test_redemption_mysql_lost_owner_pending_is_read_only_review_and_blocks_new_attempt(inv,redemption_receipt):
    x=redemption_receipt;attempt,secret=x.attempt()
    digest=inv._redemption_fingerprint('invitation:redemption:codes:v1',json.dumps([x.code],separators=(',',':')))
    inv._save_attempt(attempt,inv._recovery_fingerprint(secret),digest,'pending',cards=[inv._card_lock_name(x.code)],owner=str(uuid4()))
    before=inv._attempt_row(attempt)
    assert inv.redemption_attempt_status(attempt,secret)['attemptStatus']=='review'
    assert inv._attempt_row(attempt)==before
    with pytest.raises(Error) as error:inv.redeem([x.code],*x.attempt())
    assert error.value.code=='INVITATION_REDEMPTION_REVIEW' and not x.calls
    assert inv.repo.get_cdk_code(x.code)['status']=='unused'


def test_redemption_mysql_killed_lock_connection_is_review_without_binding_or_recreate(inv,redemption_receipt,monkeypatch):
    from contextlib import contextmanager
    x=redemption_receipt;original=inv._redemption_locks
    @contextmanager
    def capture():
        with original() as locks:
            x.locks=locks
            yield locks
    monkeypatch.setattr(inv,'_redemption_locks',capture)
    x.hook=lambda:inv.db.execute('KILL CONNECTION '+str(int(x.locks.owner)))
    attempt,secret=x.attempt()
    with pytest.raises(Error) as error:inv.redeem([x.code],attempt,secret)
    assert error.value.code=='INVITATION_REDEMPTION_REVIEW'
    assert inv.redemption_attempt_status(attempt,secret)['attemptStatus']=='review'
    assert inv.repo.get_cdk_code(x.code)['status']=='unused'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))==0
    x.hook=None
    with pytest.raises(Error):inv.redeem([x.code],*x.attempt())
    assert x.calls==[1]


def test_redemption_mysql_public_recovery_route_uses_header_and_no_store(inv,redemption_receipt,monkeypatch):
    from fastapi.testclient import TestClient
    from smsinvit.invitation.router import build_router
    x=redemption_receipt;attempt,secret=x.attempt();first=inv.redeem([x.code],attempt,secret)
    # This fixture app appends the domain router after create_app; do not install
    # a static catch-all ahead of the recovery GET route.
    monkeypatch.setitem(inv.s.config.environ,'SMSINVIT_STATIC_DIR','/tmp/no-redemption-fixture-static')
    app=create_app(inv.s.config,inv.db,services=inv.s,include_domains=False,load_runtime=False);app.include_router(build_router(inv.s))
    path='/api/v1/invitation/redemption-attempts/'+attempt
    before=inv._attempt_row(attempt)
    with TestClient(app) as client:
        assert client.get(path).status_code==404
        response=client.get(path,headers={'X-Session-Secret':secret})
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
        assert response.json()['data']['items'][0]['sessionSecret']==first['items'][0]['sessionSecret']
    assert inv._attempt_row(attempt)==before and x.calls==[1]


@pytest.mark.parametrize('redemption_receipt',[8],indirect=True)
def test_redemption_mysql_lock_connections_do_not_exhaust_single_business_connection(inv,redemption_receipt):
    from sqlalchemy import create_engine
    x=redemption_receipt;original=inv.db.engine
    limited=create_engine(original.url,creator=original.pool._creator,pool_size=1,max_overflow=0,pool_timeout=2,
                          isolation_level='REPEATABLE READ',hide_parameters=True)
    inv.db.engine=limited
    extras=[]
    try:
        extras=[card(inv,mail=False) for _ in range(7)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda code:inv.redeem([code],*x.attempt()),[x.code,*extras]))
        assert all(result['attemptStatus']=='completed' for result in results)
        assert len(x.calls)==8 and limited.pool.size()==1 and limited.pool.checkedout()==0
    finally:
        inv.db.engine=original;limited.dispose()
        for code in extras:
            marker=mysql.marker(code)
            inv.db.execute('DELETE FROM invitation_v2_sessions WHERE code_id=?',marker)
            inv.db.execute('DELETE FROM invitation_v2_cdk_events WHERE code=?',marker)
            inv.db.execute('DELETE FROM invitation_v2_cdk_codes WHERE code=?',marker)


@pytest.mark.parametrize('redemption_receipt',[2],indirect=True)
def test_redemption_mysql_partial_mail_batch_failure_keeps_review_claims(inv,redemption_receipt):
    from mail_providers.cf_temp import MailboxConnectionError
    x=redemption_receipt;second=card(inv,mail=False);attempt,secret=x.attempt()
    def hook():
        if len(x.calls)==2:raise MailboxConnectionError('synthetic definite connection rejection')
    x.hook=hook
    try:
        with pytest.raises(Error) as error:inv.redeem([x.code,second],attempt,secret)
        # 明确连接失败＝这次取号确定没有发生：整批拒绝，两个卡密都没被消耗。
        assert error.value.code=='INVITATION_MAIL_CONNECTION'
        assert inv.redemption_attempt_status(attempt,secret)['attemptStatus']=='rejected'
        assert inv.repo.get_cdk_code(x.code)['status']==inv.repo.get_cdk_code(second)['status']=='unused'
        assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id IN (?,?)',mysql.marker(x.code),mysql.marker(second))==0
        x.hook=None
        # 失败整批回滚后，卡密仍可正常再兑一次：失败没有吃掉任何号。
        retried=inv.redeem([second],*x.attempt())
        assert retried['attemptStatus']=='completed'
    finally:inv.db.execute('DELETE FROM invitation_v2_cdk_codes WHERE code=?',mysql.marker(second))


def test_redemption_mysql_card_changed_after_mock_creation_is_review(inv,redemption_receipt):
    x=redemption_receipt;attempt,secret=x.attempt()
    before_status=inv.repo.get_cdk_code(x.code)['status']
    def hook():
        # 取号那一刻发现卡密状态变了（这里模拟成已作废）：整个领取事务回滚，
        # 卡密保持原状，也绝不能留下已绑定的号或会话。
        inv.db.execute("UPDATE invitation_v2_cdk_codes SET status='void' WHERE code=?",mysql.marker(x.code))
        raise Error('INVITATION_BINDING_CHANGED','邮箱记录发生变化，请重新查询。',409)
    x.hook=hook
    with pytest.raises(Error) as error:inv.redeem([x.code],attempt,secret)
    assert error.value.code=='INVITATION_BINDING_CHANGED'
    # 取号只在一个事务里改号池行、没有任何外部副作用，所以整批回滚即可。
    assert inv.redemption_attempt_status(attempt,secret)['attemptStatus']=='rejected'
    assert inv.repo.get_cdk_code(x.code)['status']==before_status and x.calls==[1]
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))==0
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_outlook_accounts WHERE cdk_code=?',mysql.marker(x.code))==0


def test_redemption_mysql_same_attempt_while_processing_returns_pending_without_second_create(inv,redemption_receipt):
    x=redemption_receipt;attempt,secret=x.attempt()
    for _ in range(4):pool_account(inv,'receipt.example.test')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        # 同一次请求同时重复提交：只允许一次取号，其余只能拿回同一条记录
        # 或明确收到"正在处理"。
        def submit(index):
            try:return inv.redeem([x.code.lower() if index else x.code],attempt,secret)
            except Error as error:
                assert error.code=='INVITATION_REDEMPTION_BUSY'
                return {'attemptStatus':'busy','items':[]}
        results=list(pool.map(submit,range(2)))
    assert x.calls==[1]
    assert all(r['attemptStatus'] in {'completed','pending','rejected','busy'} for r in results)
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))==1
    recovered=inv.redemption_attempt_status(attempt,secret)
    completed=next(r for r in results if r['attemptStatus']=='completed')
    assert recovered['attemptStatus']==completed['attemptStatus']=='completed'
    assert recovered['items'][0]['id']==completed['items'][0]['id']
    assert recovered['items'][0]['sessionSecret']==completed['items'][0]['sessionSecret']
    assert x.calls==[1]
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))==1


@pytest.mark.parametrize('status',['bound','queued','running','done','failed','review'])
def test_redemption_mysql_used_card_returns_original_capability_and_live_state(inv,redemption_receipt,status):
    x=redemption_receipt;first=inv.redeem([x.code],*x.attempt())['items'][0]
    inv.repo.set_cdk_status(x.code,status)
    if status=='running':
        job='synthetic-used-'+uuid4().hex
        inv.repo.create_cdk_job(job,x.code,email=first['email'])
        inv.repo.set_cdk_job(job,status='running',stage='codex_auth',progress=64)
    if status=='done':
        inv.db.execute('UPDATE invitation_v2_platform_keys SET expires_at=UTC_TIMESTAMP(3)-INTERVAL 1 DAY WHERE code_id=?',mysql.marker(x.code))
    before=inv.repo.get_cdk_code(x.code)
    jobs=inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x.code))
    events=inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_events WHERE code=?',mysql.marker(x.code))
    attempt,secret=x.attempt();result=inv.redeem([x.code.lower()],attempt,secret)
    recovered=result['items'][0]
    assert result['attemptStatus']=='completed' and recovered['status']==('processing' if status in {'queued','running'} else status)
    assert (recovered['id'],recovered['sessionSecret'],recovered['email'])==(first['id'],first['sessionSecret'],first['email'])
    assert inv.redemption_attempt_status(attempt,secret)['items'][0]==recovered
    if status=='running':assert recovered['progressPercent']==64 and 'stage' not in recovered
    if status=='done':assert recovered['progressPercent']==100 and recovered['allowedActions']==[]
    if status=='review':assert recovered['allowedActions']==['POLL']
    assert inv.repo.get_cdk_code(x.code)==before and x.calls==[1]
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))==1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_outlook_accounts WHERE cdk_code=?',mysql.marker(x.code))==1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x.code))==jobs
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_events WHERE code=?',mysql.marker(x.code))==events


def test_redemption_mysql_historical_done_without_session_concurrent_recovery_creates_one_session(inv,redemption_receipt):
    x=redemption_receipt;first=inv.redeem([x.code],*x.attempt())['items'][0]
    inv.repo.set_cdk_status(x.code,'done')
    inv.db.execute('DELETE FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))
    before=inv.repo.get_cdk_code(x.code)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(lambda _:inv.redeem([x.code],*x.attempt()),range(36)))
    rows=[r['items'][0] for r in results if r['attemptStatus']=='completed']
    assert rows and len({row['id'] for row in rows})==1
    assert all(row['email']==first['email'] and row['status']=='done' and row['allowedActions']==[] for row in rows)
    assert len({row['sessionSecret'] for row in rows})==1
    assert inv.repo.get_cdk_code(x.code)==before and x.calls==[1]
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))==1
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x.code))==0


def test_redemption_mysql_mixed_new_used_concurrency_preserves_original_and_allocates_once(inv,redemption_receipt):
    x=redemption_receipt;original=inv.redeem([x.code],*x.attempt())['items'][0]
    new=card(inv,mail=False)
    # 已有卡密走找回路径，新卡密必须从池里取号：池里再备一个。
    pool_account(inv,'receipt.example.test')
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(lambda _:inv.redeem([x.code,new],*x.attempt()),range(36)))
        completed=[result for result in results if result['attemptStatus']=='completed']
        assert completed
        for result in completed:
            old,fresh=result['items']
            assert old['line']==1 and fresh['line']==2
            assert (old['id'],old['sessionSecret'],old['email'])==(original['id'],original['sessionSecret'],original['email'])
            assert fresh['email']==inv.repo.get_cdk_code(new)['email'] and fresh['email']!=old['email']
            assert inv.redemption_attempt_status(result['attemptId'],'e1'*32)['items']==result['items']
        assert len({result['items'][1]['id'] for result in completed})==1 and len(x.calls)==2
        assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id IN (?,?)',mysql.marker(x.code),mysql.marker(new))==2
        assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_outlook_accounts WHERE cdk_code=?',mysql.marker(new))==1
        assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code IN (?,?)',mysql.marker(x.code),mysql.marker(new))==0
    finally:
        inv.db.execute('DELETE FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(new))
        inv.db.execute('DELETE FROM invitation_v2_cdk_events WHERE code=?',mysql.marker(new))
        inv.db.execute('DELETE FROM invitation_v2_cdk_codes WHERE code=?',mysql.marker(new))


def test_redemption_mysql_mixed_invalid_disabled_or_void_card_is_atomic_and_leaks_no_capability(inv,redemption_receipt):
    x=redemption_receipt;original=inv.redeem([x.code],*x.attempt())['items'][0]
    new=card(inv,mail=False)
    try:
        for codes,expected,line in [([x.code,new,'SYNTHETIC-MISSING-'+uuid4().hex],'INVITATION_CDK_NOT_FOUND',3),
                                    ([x.code,new],'INVITATION_CDK_DISABLED',1),
                                    ([x.code,new],'INVITATION_CDK_UNAVAILABLE',1)]:
            if expected=='INVITATION_CDK_DISABLED':
                inv.db.execute("UPDATE inventory_items SET status='INVALID' WHERE id=(SELECT inventory_item_id FROM invitation_v2_platform_keys WHERE code_id=?)",mysql.marker(x.code))
            elif expected=='INVITATION_CDK_UNAVAILABLE':
                inv.db.execute("UPDATE inventory_items SET status='ISSUED' WHERE id=(SELECT inventory_item_id FROM invitation_v2_platform_keys WHERE code_id=?)",mysql.marker(x.code))
                inv.repo.set_cdk_status(x.code,'void')
            result=inv.redeem(codes,*x.attempt())
            assert result['attemptStatus']=='rejected'
            assert result['items']==[{'line':line,'error':{'code':expected,'message':result['items'][0]['error']['message']}}]
            assert original['email'] not in json.dumps(result) and original['sessionSecret'] not in json.dumps(result)
            assert inv.repo.get_cdk_code(new)['status']=='unused' and x.calls==[1]
            assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(new))==0
    finally:inv.db.execute('DELETE FROM invitation_v2_cdk_codes WHERE code=?',mysql.marker(new))


def test_redemption_mysql_used_task_hold_returns_original_progress_but_blocks_mutations(inv,redemption_receipt):
    x=redemption_receipt;original=inv.redeem([x.code],*x.attempt())['items'][0]
    job='synthetic-held-used-'+uuid4().hex
    inv.repo.create_cdk_job(job,x.code,email=original['email'])
    inv.repo.set_cdk_job(job,status='failed',progress=42)
    inv.repo.set_cdk_status(x.code,'failed')
    inv.db.execute("INSERT INTO invitation_v2_task_holds(job_id,original_status,reason) VALUES (?,'FAILED','LEGACY_UNCERTAIN')",mysql.marker(job))
    before=inv.repo.get_cdk_code(x.code)
    result=inv.redeem([x.code],*x.attempt());row=result['items'][0]
    assert result['attemptStatus']=='completed' and row['status']=='review' and row['allowedActions']==['POLL']
    assert (row['id'],row['sessionSecret'],row['email'])==(original['id'],original['sessionSecret'],original['email'])
    assert row['progressPercent']==42 and x.calls==[1]
    for action in ['invite-sent','start']:
        denied=inv.action([{'id':row['id'],'sessionSecret':row['sessionSecret']}],action)
        assert denied['items'][0]['error']['code']=='INVITATION_REVIEW_REQUIRED'
    assert inv.repo.get_cdk_code(x.code)==before
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs WHERE code=?',mysql.marker(x.code))==1


@pytest.mark.parametrize('status',['pending','review'])
def test_redemption_mysql_used_lookup_cannot_bypass_unknown_attempt_card_hold(inv,redemption_receipt,status):
    x=redemption_receipt;original=inv.redeem([x.code],*x.attempt())['items'][0]
    attempt,secret=x.attempt()
    digest=inv._redemption_fingerprint('invitation:redemption:codes:v1',json.dumps([x.code],separators=(',',':')))
    inv._save_attempt(attempt,inv._recovery_fingerprint(secret),digest,status,cards=[inv._card_lock_name(x.code)],owner=str(uuid4()))
    before=inv._attempt_row(attempt)
    assert inv.redemption_attempt_status(attempt,secret)['attemptStatus']=='review'
    assert inv._attempt_row(attempt)==before
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(inv.redeem,[x.code],*x.attempt()) for _ in range(16)]
        for future in futures:
            try:result=future.result()
            except Error as error:assert error.code=='INVITATION_REDEMPTION_REVIEW'
            else:
                assert result['attemptStatus']=='rejected'
                assert original['sessionSecret'] not in json.dumps(result) and original['email'] not in json.dumps(result)
    assert x.calls==[1] and inv.repo.get_cdk_code(x.code)['status']=='bound'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions WHERE code_id=?',mysql.marker(x.code))==1


def test_executor_mysql_setting_and_audit_persist_without_stopping_queued_business(inv,redemption_receipt,monkeypatch):
    import threading
    from unittest.mock import Mock
    from smsinvit.invitation.service import InvitationService
    x=redemption_receipt;key='invitation_executor_enabled'
    old=inv.db.one('SELECT value FROM invitation_v2_settings WHERE `key`=?',key)
    before_audits={row['id'] for row in inv.db.all("SELECT id FROM audit_logs WHERE action='invitation.executor.update'")}
    item=inv.redeem([x.code],*x.attempt())['items'][0]
    job='synthetic-toggle-'+uuid4().hex;inv.repo.create_cdk_job(job,x.code,email=item['email'])
    inv.repo.set_cdk_status(x.code,'queued');before=inv.repo.get_cdk_code(x.code)
    monkeypatch.setattr(inv,'_started',False);monkeypatch.setattr(inv,'_executor_enabled',False)
    monkeypatch.setattr(inv,'_execution_self_check',Mock())
    def start(*,background):
        assert background is False
        inv._started=True
        # 真实 start() 会同时拿到执行器排他锁：状态里的 ready 包含"仍持有锁"这一项
        # （2026-10-12 线上事故就是丢锁后前台仍显示可以开始、接口一律 503）。
        connection=inv.db.engine.connect()
        assert connection.exec_driver_sql("SELECT GET_LOCK('smsinvit:invitation:worker',0)").scalar()==1
        inv._process_lock=connection
        inv._process_lock_owner=connection.exec_driver_sql('SELECT CONNECTION_ID()').scalar()
    started=Mock(side_effect=start);stopped=Mock()
    monkeypatch.setattr(inv,'start',started);monkeypatch.setattr(inv,'stop',stopped)
    monkeypatch.setattr(inv.cdk.CONTROLLER,'worker_alive',lambda:1)
    monkeypatch.setattr(inv.proxy,'has_available',lambda:True)
    try:
        assert inv.set_executor_enabled(True,SimpleNamespace(id=None))['ready']
        assert inv.repo.get_setting(key)=='true'
        restarted=InvitationService.__new__(InvitationService)
        restarted._executor_lock=threading.RLock();restarted.repo=inv.repo
        assert restarted.initialize_executor_enabled(False) is True
        assert inv.set_executor_enabled(False,SimpleNamespace(id=None))['state']=='disabled'
        assert inv.repo.get_setting(key)=='false' and restarted.initialize_executor_enabled(True) is False
        assert inv._started and not inv.gate.closed
        stopped.assert_not_called();started.assert_called_once_with(background=False)
        assert inv.repo.get_cdk_code(x.code)==before
        assert inv.db.scalar('SELECT status FROM invitation_v2_cdk_jobs WHERE job_id=?',mysql.marker(job))=='queued'
        after_audits={row['id'] for row in inv.db.all("SELECT id FROM audit_logs WHERE action='invitation.executor.update'")}
        assert len(after_audits-before_audits)==2 and x.calls==[1]
    finally:
        if inv._process_lock is not None:
            inv._process_lock.exec_driver_sql("SELECT RELEASE_LOCK('smsinvit:invitation:worker')")
            inv._process_lock.close();inv._process_lock=None;inv._process_lock_owner=None
        if old:inv.db.execute('UPDATE invitation_v2_settings SET value=? WHERE `key`=?',old['value'],key)
        else:inv.db.execute('DELETE FROM invitation_v2_settings WHERE `key`=?',key)
        for row in inv.db.all("SELECT id FROM audit_logs WHERE action='invitation.executor.update'"):
            if row['id'] not in before_audits:inv.db.execute('DELETE FROM audit_logs WHERE id=?',row['id'])


def test_executor_mysql_setting_write_rolls_back_when_audit_fails(inv,monkeypatch):
    key='invitation_executor_enabled';old=inv.db.one('SELECT value FROM invitation_v2_settings WHERE `key`=?',key)
    monkeypatch.setattr(inv,'_executor_enabled',True)
    monkeypatch.setattr(inv.s.audit,'record',lambda *a:(_ for _ in ()).throw(RuntimeError('synthetic audit failure')))
    try:
        with pytest.raises(Error) as error:inv.set_executor_enabled(False,SimpleNamespace(id=None))
        assert error.value.code=='INVITATION_EXECUTOR_SAVE_FAILED' and inv._executor_enabled
        assert inv.db.one('SELECT value FROM invitation_v2_settings WHERE `key`=?',key)==old
    finally:
        if old:inv.db.execute('UPDATE invitation_v2_settings SET value=? WHERE `key`=?',old['value'],key)
        else:inv.db.execute('DELETE FROM invitation_v2_settings WHERE `key`=?',key)

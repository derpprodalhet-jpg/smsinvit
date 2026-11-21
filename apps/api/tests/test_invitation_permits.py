"""Durable execution fencing against the isolated MySQL; no upstream calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import URL
from fastapi.testclient import TestClient
from smsinvit.app import Services,create_app
from smsinvit.config import Config
from smsinvit.db import Database
from smsinvit.errors import Error
from smsinvit.security import Principal
from smsinvit.invitation.permits import Permits,router
from smsinvit.invitation.runtime import TaskGate
from smsinvit.invitation import mysql

pytestmark=pytest.mark.skipif(os.environ.get('SMSINVIT_MYSQL_TEST')!='1',reason='isolated MySQL explicitly opt-in')
@pytest.fixture
def s():
    root=Path(__file__).resolve().parents[3]
    db=Database(URL.create('mysql+pymysql',username='root',password=json.loads((root/'data/test-private/mysql.json').read_text())['password'],host='127.0.0.1',port=13308,database='smsinvit_test'),pool_size=25)
    assert db.scalar('SELECT DATABASE()')=='smsinvit_test'
    assert db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits')==33
    assert db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL')==0
    config=Config({'smsinvit.inventory-encryption-key':'b1'*32,'smsinvit.inventory-fingerprint-key':'permit-test-fingerprint','smsinvit.jwt-secret':'permit-test-jwt-over-thirty-two-characters'},environ={})
    service=Services(db,config);service.permit_instances=[]
    yield service
    for id in service.permit_instances:db.execute('UPDATE invitation_v2_execution_permits SET owner_token=NULL,instance_id=NULL,email=NULL,task_id=NULL,acquired_at=NULL WHERE instance_id=?',id)
    db.close()
def permits(s):
    p=Permits(s.db,s.codec);s.permit_instances.append(p.instance_id);return p

def test_hundred_claims_multiple_instances_respect_three_and_never_use_mutex(s):
    objects=[permits(s) for _ in range(10)]
    with ThreadPoolExecutor(max_workers=25) as pool:
        results=list(pool.map(lambda i:objects[i%10].claim(f'{i}@example.test','CDK-CONTAINS-PRIVATE-SECRET',3),range(100)))
    claimed=[p for p in results if p]
    assert len(claimed)==3 and {p[0] for p in claimed}=={1,2,3}
    rows=s.db.all('SELECT * FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL')
    assert len(rows)==3 and all('PRIVATE-SECRET' not in r['task_id'] for r in rows)
    assert s.db.scalar('SELECT owner_token FROM invitation_v2_execution_permits WHERE slot_id=0') is None

def test_same_mailbox_is_global_case_insensitive_and_release_is_owner_fenced(s):
    first,second=permits(s),permits(s);a=first.claim(' A@EXAMPLE.TEST ','private-job',3)
    assert a and second.claim('a@example.test','another-job',3) is None
    assert second.release((a[0],str(uuid4()))) is False
    assert first.release(a) is True and first.release(a) is False
    b=second.claim('a@example.test','next',3);assert b and b[1]!=a[1]
    assert first.release(a) is False and second.release(b) is True

def test_lost_advisory_owner_and_restart_do_not_reclaim_occupied_permits(s):
    first=permits(s);slots=[first.claim(f'{i}@example.test',f'job-{i}',3) for i in range(3)]
    with s.db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT GET_LOCK('smsinvit:invitation:worker',0)").scalar()==1
        assert connection.exec_driver_sql("SELECT RELEASE_LOCK('smsinvit:invitation:worker')").scalar()==1
    restarted=permits(s)
    assert restarted.claim('new@example.test','new',3) is None
    assert restarted.claim('other@example.test','new',1) is None
    for slot in slots:assert first.release(slot)
    assert restarted.claim('new@example.test','new',3)

def test_gate_decrypt_failure_releases_durable_permit_but_database_release_failure_retains_it(s,monkeypatch):
    p=permits(s);gate=TaskGate(SimpleNamespace(get_setting=lambda *a:'3'),p)
    monkeypatch.setattr(mysql,'services',lambda:SimpleNamespace(db=SimpleNamespace(all=lambda *a:[{'key':'sayhi_model','value':'x'}])))
    def authorize_registered_email(email):return True
    run=gate.decorate(authorize_registered_email)
    monkeypatch.setattr(mysql,'revealed',lambda *a,**kw:(_ for _ in ()).throw(ValueError('invalid ciphertext')))
    with pytest.raises(ValueError):run('a@example.test')
    assert s.db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL')==0
    monkeypatch.setattr(mysql,'revealed',lambda value,**kw:value)
    monkeypatch.setattr(p,'release',lambda *a:(_ for _ in ()).throw(ConnectionError('database disconnected')))
    assert run('a@example.test') is True
    assert s.db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL')==1
    assert permits(s).claim('a@example.test','new',3) is None
    assert gate.active==gate.inflight==0

def test_second_worker_is_refused_before_any_business_setup(s):
    from smsinvit.invitation.service import InvitationService
    second=InvitationService.__new__(InvitationService);second.db=s.db;second._started=False;second._stopping=False
    with s.db.engine.connect() as owner:
        assert owner.exec_driver_sql("SELECT GET_LOCK('smsinvit:invitation:worker',0)").scalar()==1
        try:
            with pytest.raises(RuntimeError,match='second owner'):second.start()
            assert second._started is False
        finally:owner.exec_driver_sql("SELECT RELEASE_LOCK('smsinvit:invitation:worker')")

def test_restart_holds_queued_and_running_without_requeue(s,monkeypatch):
    import smsinvit.invitation.service as module
    ids=[('iv2$'+uuid4().hex+uuid4().hex,status) for status in ('queued','running')]
    for id,status in ids:s.db.execute('INSERT INTO invitation_v2_cdk_jobs(job_id,code,email,status,created_at) VALUES (?,?,?,?,?)',id,id,'permit@example.test',status,1)
    service=module.InvitationService.__new__(module.InvitationService);service.db=s.db;service._started=False;service._stopping=False;service._process_lock=None
    events=[];service.gate=SimpleNamespace(open=lambda:events.append('gate-open'))
    service._execution_self_check=lambda:events.append('sandbox-check')
    service.cdk=SimpleNamespace(CONTROLLER=SimpleNamespace(start_workers=lambda:events.append('start-empty-workers')))
    # 账号工厂是独立开关，但生命周期挂在同一个执行进程上：它必须被启停。
    service.pool=SimpleNamespace(start=lambda:events.append('pool'),stop=lambda:events.append('pool-stop'),
                                 _thread=SimpleNamespace(join=lambda timeout=None:None,is_alive=lambda:False))
    real_import=module.importlib.import_module
    def importer(name):
        if name=='webui.sms_prices':return SimpleNamespace(MONITOR=SimpleNamespace(start=lambda:events.append('prices')))
        if name=='webui.sms_refund':return SimpleNamespace(WORKER=SimpleNamespace(start=lambda:events.append('refund')))
        return real_import(name)
    monkeypatch.setattr(module.importlib,'import_module',importer)
    try:
        service.start()
        assert events==['sandbox-check','gate-open','start-empty-workers','prices','pool','refund']
        for id,status in ids:
            assert s.db.scalar('SELECT original_status FROM invitation_v2_task_holds WHERE job_id=?',id)==status
            assert s.db.scalar('SELECT status FROM invitation_v2_cdk_jobs WHERE job_id=?',id)==status
    finally:
        if service._process_lock is not None:
            service._process_lock.exec_driver_sql("SELECT RELEASE_LOCK('smsinvit:invitation:worker')");service._process_lock.close()
        for id,status in ids:
            s.db.execute('DELETE FROM invitation_v2_task_holds WHERE job_id=?',id);s.db.execute('DELETE FROM invitation_v2_cdk_jobs WHERE job_id=?',id)

def test_manual_release_requires_permissions_csrf_no_owner_and_exact_token(s):
    p=permits(s);slot=p.claim('recover@example.test','unknown-job',3);calls=[];allowed={'invitation.manage'}
    actor=Principal('fixture-admin','fixture-session',('ADMIN',))
    s.auth.authenticate=lambda *a:actor;s.limiter=SimpleNamespace(check=lambda *a:None)
    def require(request,permission,*a,**kw):
        if permission not in allowed:raise Error('PERMISSION_DENIED','denied',403)
        return actor
    s.permissions=SimpleNamespace(require=require,allows=lambda actor,p,*a,**kw:p in allowed)
    s.audit=SimpleNamespace(record=lambda *a:calls.append(a))
    app=create_app(s.config,s.db,services=s,include_domains=False,load_runtime=False);app.include_router(router)
    url=f'/api/v1/admin/invitation/execution-permits/{slot[0]}/release'
    body={'ownerToken':slot[1],'confirmAllExecutorsStopped':True,'note':'Executors stopped and external result reviewed'};headers={'Authorization':'Bearer fixture'}
    with TestClient(app) as client:
        assert client.post(url,json=body).status_code==401
        assert client.post(url,headers=headers,json=body).status_code==403
        allowed.add('invitation.credentials')
        assert client.post(url,headers=headers,cookies={'lc_refresh':'x','lc_csrf':'valid'},json=body).status_code==403
        with s.db.engine.connect() as owner:
            assert owner.exec_driver_sql("SELECT GET_LOCK('smsinvit:invitation:worker',0)").scalar()==1
            try:assert client.post(url,headers=headers,json=body).status_code==409
            finally:owner.exec_driver_sql("SELECT RELEASE_LOCK('smsinvit:invitation:worker')")
        assert not calls
        assert client.post(url,headers=headers,json={**body,'ownerToken':str(uuid4())}).status_code==409
        assert not calls
        assert client.post(url,headers=headers,json=body).status_code==200
        assert len(calls)==1 and 'Executors stopped' not in json.dumps(calls)
        assert client.post(url,headers=headers,json=body).status_code==409
    assert s.db.scalar('SELECT owner_token FROM invitation_v2_execution_permits WHERE slot_id=?',slot[0]) is None

def test_uncertain_child_cleanup_retains_permit_after_source_catches_process_error(s,monkeypatch):
    from smsinvit.invitation.process import UNCERTAIN_PROCESS
    p=permits(s);gate=TaskGate(SimpleNamespace(get_setting=lambda *a:'3'),p)
    monkeypatch.setattr(mysql,'services',lambda:SimpleNamespace(db=SimpleNamespace(all=lambda *a:[])))
    def authorize_registered_email(email):
        # The copied business function catches CLI exceptions and returns its
        # normal failed result; cleanup certainty must survive that conversion.
        UNCERTAIN_PROCESS.set(True);return {'ok':False,'reason':'child failed'}
    assert gate.decorate(authorize_registered_email)('uncertain-child@example.test')['ok'] is False
    assert UNCERTAIN_PROCESS.get() is False
    assert s.db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL')==1
    assert permits(s).claim('uncertain-child@example.test','retry',3) is None

def test_preflight_failure_releases_singleton_without_holds_writes_or_workers(s):
    from smsinvit.invitation.service import InvitationService
    service=InvitationService.__new__(InvitationService);service.db=s.db;service._started=False;service._stopping=False;service._process_lock=None
    events=[];service.gate=SimpleNamespace(open=lambda:events.append('gate-open'))
    service.cdk=SimpleNamespace(CONTROLLER=SimpleNamespace(start_workers=lambda:events.append('worker')))
    def fail():raise RuntimeError('Unsupported kernel')
    service._execution_self_check=fail
    before=s.db.scalar('SELECT COUNT(*) FROM invitation_v2_task_holds')
    with pytest.raises(RuntimeError,match='Unsupported kernel'):service.start()
    assert not events and service._started is False and service._process_lock is None
    assert s.db.scalar('SELECT COUNT(*) FROM invitation_v2_task_holds')==before
    with s.db.engine.connect() as next_owner:
        assert next_owner.exec_driver_sql("SELECT GET_LOCK('smsinvit:invitation:worker',0)").scalar()==1
        next_owner.exec_driver_sql("SELECT RELEASE_LOCK('smsinvit:invitation:worker')")

"""只写库、没进内存队列的作业必须能被发现，并由操作员手动补投。

这里用 SQLite + MySQL 语法适配器跑真的 `dispatch` 代码路径；控制器用真实的
`CdkController`（队列/在飞集合都是真的），只把"补建工人"换成受控数字，
测试里不起线程。
"""
from pathlib import Path
import os
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from fastapi.testclient import TestClient

# `webui.registrar` reads these at import time (the service sets them in production).
os.environ.setdefault('SMSINVIT_INVITATION_LOG_DIR', tempfile.mkdtemp(prefix='invitation-logs-'))
os.environ.setdefault('CODEX_HOMES_ROOT', tempfile.mkdtemp(prefix='codex-homes-'))

from smsinvit.app import Services,create_app
from smsinvit.config import Config
from smsinvit.db import Database
from smsinvit.errors import Error
from smsinvit.security import Principal
from smsinvit.invitation import mysql
from smsinvit.invitation.dispatch import GRACE_SECONDS,dispatch_gaps,redispatch,router as dispatch_router
from smsinvit.invitation.service import InvitationService
from webui import cdk

SCHEMA = """
CREATE TABLE invitation_v2_secret_values (
  fingerprint TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, plaintext_length INTEGER NOT NULL);
CREATE TABLE invitation_v2_cdk_codes (
  code TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'unused', email TEXT,
  created_at REAL, finished_at REAL, error TEXT);
CREATE TABLE invitation_v2_cdk_jobs (
  job_id TEXT PRIMARY KEY, code TEXT NOT NULL, email TEXT, attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL, stage TEXT, progress INTEGER NOT NULL DEFAULT 0, proxy TEXT,
  error TEXT, created_at REAL, started_at REAL, finished_at REAL);
CREATE TABLE invitation_v2_task_holds (
  job_id TEXT PRIMARY KEY, original_status TEXT NOT NULL, reason TEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at DATETIME,
  resolved_by TEXT, resolution TEXT, note TEXT, confirmed_executors_stopped INTEGER);
-- 请求边界上的 IP 封禁判定会读这张表（生产由 V22 迁移建表）。
CREATE TABLE admin_ip_bans (
  ip_hash TEXT PRIMARY KEY, ip_address TEXT NOT NULL, failure_count INTEGER NOT NULL DEFAULT 0,
  first_failure_at DATETIME NOT NULL, last_failure_at DATETIME NOT NULL, last_identifier TEXT,
  banned_until DATETIME, ban_reason TEXT, created_at DATETIME, updated_at DATETIME);
"""

GAPS_URL = '/api/v1/admin/invitation/queue/gaps'
REDISPATCH_URL = '/api/v1/admin/invitation/queue/redispatch'
HEADERS = {'Authorization': 'Bearer fixture'}


class QueueSqliteDatabase(Database):
    def _prepare(self,sql,params):
        sql=sql.replace(' FOR UPDATE SKIP LOCKED','').replace(' FOR UPDATE','')
        sql=sql.replace(' ON DUPLICATE KEY UPDATE fingerprint=VALUES(fingerprint)',
                        ' ON CONFLICT(fingerprint) DO NOTHING')
        sql=sql.replace('INSERT IGNORE','INSERT OR IGNORE')
        sql=sql.replace('UTC_TIMESTAMP(3)','CURRENT_TIMESTAMP')
        return super()._prepare(sql,params)


class StubController(cdk.CdkController):
    """真实队列与在飞集合，但补建工人受控（测试不起线程）。"""

    def __init__(self,alive=1):
        super().__init__()
        self._alive=alive

    def ensure_workers(self):
        return self._alive

    def worker_alive(self):
        return self._alive


@pytest.fixture
def queues(tmp_path,monkeypatch):
    path=tmp_path/'queue.sqlite3'
    with sqlite3.connect(path) as connection:connection.executescript(SCHEMA)
    engine=create_engine('sqlite:///'+str(path),connect_args={'check_same_thread':False,'timeout':30},
                         pool_size=10,max_overflow=10,hide_parameters=True)
    db=QueueSqliteDatabase(engine=engine)
    monkeypatch.setattr(mysql,'_services',None)
    monkeypatch.setattr(mysql,'_runtime_owner',None)
    monkeypatch.setattr(mysql,'_cache',{})
    monkeypatch.setattr(mysql,'_redaction_values',set())
    config=Config({'smsinvit.inventory-encryption-key':'b1'*32,
                   'smsinvit.inventory-fingerprint-key':'queue-test-fingerprint',
                   'smsinvit.jwt-secret':'queue-test-jwt-secret-over-thirty-two-chars'},environ={})
    holder=Services(db,config)
    mysql.configure(holder)
    state=SimpleNamespace(db=db,holder=holder,audited=[],alive=1,
                          actor=Principal('fixture-admin','fixture-session',('ADMIN',)))
    state.allowed={'invitation.read','invitation.manage'}
    controller=StubController(alive=state.alive)
    service=InvitationService.__new__(InvitationService)
    service.db=db;service.s=holder
    service._mutex=threading.RLock();service._executor_lock=threading.RLock()
    service._executor_enabled=True;service._started=True;service._stopping=False
    # 准入要求"仍持执行器排他锁"；给一个恒为真的假连接。
    service._process_lock=SimpleNamespace(exec_driver_sql=lambda sql:SimpleNamespace(scalar=lambda:9))
    service._process_lock_owner=9
    service.cdk=SimpleNamespace(CONTROLLER=controller)
    holder.invitation=service
    state.service=service;state.controller=controller
    def require(request,permission,*args,**kwargs):
        if permission not in state.allowed:raise Error('PERMISSION_DENIED','denied',403)
        return state.actor
    holder.permissions=SimpleNamespace(require=require,allows=lambda actor,permission,*a,**k:permission in state.allowed)
    holder.audit=SimpleNamespace(record=lambda *args:state.audited.append(args))
    holder.auth=SimpleNamespace(authenticate=lambda token:state.actor)
    holder.limiter=SimpleNamespace(check=lambda *args:None)
    app=create_app(config,db,services=holder,include_domains=False,load_runtime=False)
    app.include_router(dispatch_router)
    with TestClient(app) as client:
        state.client=client
        yield state
    db.close()


def seed_job(state,job_id,status='queued',email='gap@example.test',code=None,
             age=300,hold=False):
    # protected()（不是 marker()）才会登记密文，和真实写库路径一致。
    code=code or f'CDK-{job_id.upper()}'
    marker=mysql.protected(job_id)
    state.db.execute('INSERT INTO invitation_v2_cdk_jobs(job_id,code,email,status,created_at) VALUES (?,?,?,?,?)',
                     marker,mysql.protected(code),email,status,time.time()-age)
    state.db.execute('INSERT INTO invitation_v2_cdk_codes(code,email,status,created_at) VALUES (?,?,?,?)',
                     mysql.protected(code),email,status,time.time()-age)
    if hold:
        state.db.execute("INSERT INTO invitation_v2_task_holds(job_id,original_status,reason) VALUES (?,?,?)",
                         marker,status,'RESTART_UNCERTAIN')
    return job_id


def job_count(state):
    return state.db.scalar('SELECT COUNT(*) FROM invitation_v2_cdk_jobs')


def test_a_job_that_only_exists_in_the_database_is_reported_as_a_gap(queues):
    seed_job(queues,'job-lost')
    gaps=dispatch_gaps(queues.service)
    assert [gap['jobId'] for gap in gaps]==['job-lost']
    assert gaps[0]['email']=='gap@example.test'
    assert gaps[0]['ageSeconds']>=300
    assert set(gaps[0])=={'jobId','email','createdAt','ageSeconds','stage','progress'}


def test_a_job_still_waiting_in_the_memory_queue_is_not_a_gap(queues):
    seed_job(queues,'job-queued')
    queues.controller.dispatch('job-queued','CDK-GAP')
    assert dispatch_gaps(queues.service)==[]


def test_a_job_the_worker_already_took_is_not_a_gap(queues):
    seed_job(queues,'job-taken')
    queues.controller._active['job-taken']=time.time()
    assert dispatch_gaps(queues.service)==[]


def test_a_job_with_an_unresolved_hold_is_never_a_gap(queues):
    seed_job(queues,'job-held',hold=True)
    assert dispatch_gaps(queues.service)==[]


def test_a_job_inside_the_grace_window_is_not_reported_yet(queues):
    seed_job(queues,'job-fresh',age=1)
    seed_job(queues,'job-old',age=GRACE_SECONDS+30)
    assert [gap['jobId'] for gap in dispatch_gaps(queues.service)]==['job-old']


def test_a_running_job_is_not_a_gap(queues):
    seed_job(queues,'job-running',status='running')
    assert dispatch_gaps(queues.service)==[]


def test_redispatch_hands_the_existing_job_back_without_creating_a_row(queues):
    seed_job(queues,'job-lost',code='CDK-LOST')
    before=job_count(queues)
    result=redispatch(queues.service,'job-lost')
    assert result=={'already':False,'jobId':'job-lost'}
    assert job_count(queues)==before
    assert queues.controller.queued_job_ids()=={'job-lost'}
    # 递给工人的 code 必须是明文，和用户点开始时入队的形态一致。
    with queues.controller._q.mutex:
        assert [item['code'] for item in queues.controller._q.queue]==['CDK-LOST']
    # 状态没有被改写：仍然排队等工人接手。
    assert queues.db.scalar('SELECT status FROM invitation_v2_cdk_jobs WHERE job_id=?',mysql.marker('job-lost'))=='queued'
    assert dispatch_gaps(queues.service)==[]


def test_redispatch_is_idempotent(queues):
    seed_job(queues,'job-lost')
    assert redispatch(queues.service,'job-lost')['already'] is False
    assert redispatch(queues.service,'job-lost')=={'already':True,'jobId':'job-lost'}
    assert len(queues.controller.queued_job_ids())==1


def test_redispatch_refuses_a_job_that_is_no_longer_queued(queues):
    seed_job(queues,'job-done',status='done')
    with pytest.raises(Error) as error:redispatch(queues.service,'job-done')
    assert error.value.code=='CONFLICT' and error.value.status==409
    assert queues.controller.queued_job_ids()==set()


def test_redispatch_refuses_an_unknown_job(queues):
    with pytest.raises(Error) as error:redispatch(queues.service,uuid4().hex)
    assert error.value.code=='NOT_FOUND' and error.value.status==404


def test_redispatch_refuses_a_job_with_an_unresolved_hold(queues):
    seed_job(queues,'job-held',hold=True)
    with pytest.raises(Error) as error:redispatch(queues.service,'job-held')
    assert error.value.code=='INVITATION_REVIEW_REQUIRED' and error.value.status==409


def test_redispatch_refuses_inside_the_grace_window(queues):
    seed_job(queues,'job-fresh',age=1)
    with pytest.raises(Error) as error:redispatch(queues.service,'job-fresh')
    assert error.value.code=='CONFLICT'


def test_redispatch_refuses_when_no_worker_can_run_it(queues):
    seed_job(queues,'job-lost')
    queues.controller._alive=0
    with pytest.raises(Error) as error:redispatch(queues.service,'job-lost')
    assert error.value.code=='INVITATION_EXECUTOR_UNAVAILABLE' and error.value.status==503
    assert queues.controller.queued_job_ids()==set()


def test_redispatch_requires_permission_confirmation_and_audits(queues):
    seed_job(queues,'job-lost')
    assert queues.client.post(REDISPATCH_URL,json={'jobId':'job-lost','confirmNoExternalAction':True}).status_code==401
    queues.allowed.discard('invitation.manage')
    assert queues.client.post(REDISPATCH_URL,headers=HEADERS,
                              json={'jobId':'job-lost','confirmNoExternalAction':True}).status_code==403
    queues.allowed.add('invitation.manage')
    assert queues.client.post(REDISPATCH_URL,headers=HEADERS,json={'jobId':'job-lost'}).status_code==422
    assert queues.client.post(REDISPATCH_URL,headers=HEADERS,
                              json={'jobId':'x','confirmNoExternalAction':True}).status_code==422
    response=queues.client.post(REDISPATCH_URL,headers=HEADERS,
                                json={'jobId':'job-lost','confirmNoExternalAction':True})
    assert response.status_code==200 and response.json()['data']=={'already':False,'jobId':'job-lost'}
    assert [entry[1] for entry in queues.audited]==['invitation.queue.redispatch']
    assert queues.audited[0][4]=={'confirmedNoExternalAction':True}
    # 幂等路径不再记一条审计。
    queues.client.post(REDISPATCH_URL,headers=HEADERS,
                       json={'jobId':'job-lost','confirmNoExternalAction':True})
    assert len(queues.audited)==1


def test_gaps_endpoint_requires_read_permission_and_reports_worker_health(queues):
    seed_job(queues,'job-lost')
    assert queues.client.get(GAPS_URL).status_code==401
    queues.allowed.discard('invitation.read')
    assert queues.client.get(GAPS_URL,headers=HEADERS).status_code==403
    queues.allowed.add('invitation.read')
    payload=queues.client.get(GAPS_URL,headers=HEADERS).json()['data']
    assert payload['graceSeconds']==GRACE_SECONDS and payload['workerAlive']==1
    assert [gap['jobId'] for gap in payload['items']]==['job-lost']


# ── 排队名次：待复核的旧任务不算，同一时刻入队不再并列 ──


def test_queue_position_skips_held_jobs_and_counts_only_schedulable_ones(queues):
    from webui import cdk as vendor_cdk
    seed_job(queues, 'job-held', hold=True, age=500)
    seed_job(queues, 'job-first', age=400)
    seed_job(queues, 'job-second', age=200)
    controller = queues.controller
    # 待复核的作业不会被调度：它自己的名次是 0，也不占别人的名次。
    assert controller.queue_position('job-held') == 0
    assert controller.queue_position('job-first') == 1
    assert controller.queue_position('job-second') == 2


def test_same_timestamp_jobs_get_distinct_positions(queues):
    seed_job(queues, 'job-aaa', age=300)
    seed_job(queues, 'job-bbb', age=300)
    stamp = time.time() - 300
    for job in ('job-aaa', 'job-bbb'):
        queues.db.execute('UPDATE invitation_v2_cdk_jobs SET created_at=? WHERE job_id=?',
                          stamp, mysql.marker(job))
    positions = sorted(queues.controller.queue_position(job) for job in ('job-aaa', 'job-bbb'))
    # 旧实现用严格的 created_at < ?，同一时刻入队的两条会拿到一样的名次。
    assert positions == [1, 2]


def test_queue_position_is_zero_for_jobs_that_are_not_queued(queues):
    seed_job(queues, 'job-running', status='running')
    seed_job(queues, 'job-done', status='done')
    assert queues.controller.queue_position('job-running') == 0
    assert queues.controller.queue_position('job-done') == 0
    assert queues.controller.queue_position('does-not-exist') == 0


# --------------------------------------------------------------------------------------
# F02：管理员停止的排队作业，绝不能被随后取出队列项的 worker 重新执行
# --------------------------------------------------------------------------------------

def job_status(state, job_id):
    return state.db.scalar('SELECT status FROM invitation_v2_cdk_jobs WHERE job_id=?',
                           mysql.marker(job_id))


def code_status(state, code):
    return state.db.scalar('SELECT status FROM invitation_v2_cdk_codes WHERE code=?',
                           mysql.protected(code))


def stop(state, job_id):
    # stop_job 会释放该邮箱的执行占位；这个测试库默认不带该表，按需补一张最小结构。
    state.db.execute(
        'CREATE TABLE IF NOT EXISTS invitation_v2_execution_permits ('
        'slot_id INTEGER PRIMARY KEY, owner_token TEXT, instance_id TEXT, email TEXT, task_id TEXT, acquired_at REAL)')
    return state.service.stop_job(job_id, state.actor)


def test_stopped_queued_job_is_never_executed_by_the_worker(queues, monkeypatch):
    """入队 → 管理员停止 → worker 取出旧队列项：不得建运行记录、不得写回 running。"""
    seed_job(queues, 'job-stopped', code='CDK-STOPPED')
    started = []
    monkeypatch.setattr(cdk.registrar, 'begin_run',
                        lambda *a, **k: started.append((a, k)) or (_ for _ in ()).throw(
                            AssertionError('已停止的作业不得建立运行记录')))

    result = stop(queues, 'job-stopped')
    assert result['stopped'] is True and result['reviewRequired'] is False
    assert job_status(queues, 'job-stopped') == 'failed'
    assert code_status(queues, 'CDK-STOPPED') == 'failed'

    # worker 取出这个（已经失效的）队列项。
    queues.controller._run_job(0, {'job_id': 'job-stopped', 'code': 'CDK-STOPPED'})

    assert started == [], '上游动作必须一次都不发生'
    assert job_status(queues, 'job-stopped') == 'failed', '旧队列项不得把作业改回 running'
    assert code_status(queues, 'CDK-STOPPED') == 'failed', '卡密不得被改回 running'
    # 停止原因必须保留，不能被「本次未启动」覆盖。
    assert queues.db.scalar('SELECT error FROM invitation_v2_cdk_jobs WHERE job_id=?',
                            mysql.marker('job-stopped')) == '管理员手动停止'


def test_worker_claiming_first_makes_a_later_stop_require_review(queues, monkeypatch):
    """反过来的交错：worker 先认领，停止必须保留复核（可能已有外部动作）。"""
    seed_job(queues, 'job-claimed', code='CDK-CLAIMED')
    monkeypatch.setattr(cdk.registrar, 'begin_run', lambda *a, **k: (None, None, None, None))
    # 让执行体在认领之后停下，避免真的走完邀请流程。
    monkeypatch.setattr(queues.controller, '_execute', lambda *a, **k: None, raising=False)

    assert cdk.db.claim_cdk_job('job-claimed') is True
    running = stop(queues, 'job-claimed')
    assert running['reviewRequired'] is True
    assert '复核' in running['message']
    assert queues.db.scalar('SELECT COUNT(*) FROM invitation_v2_task_holds WHERE job_id=? AND resolved_at IS NULL',
                            mysql.marker('job-claimed')) == 1


def test_claim_cdk_job_refuses_a_job_that_is_not_queued(queues):
    seed_job(queues, 'job-failed', status='failed')
    assert cdk.db.claim_cdk_job('job-failed') is False
    assert job_status(queues, 'job-failed') == 'failed'


def test_claim_cdk_job_succeeds_exactly_once(queues):
    """两个 worker 抢同一个作业时只能有一个认领成功。"""
    seed_job(queues, 'job-race')
    first = cdk.db.claim_cdk_job('job-race')
    second = cdk.db.claim_cdk_job('job-race')
    assert (first, second) == (True, False)
    assert job_status(queues, 'job-race') == 'running'


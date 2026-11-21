"""待复核闭环：解除封锁必须证明执行器已停，且绝不把未知结果写成成功。

真实 MySQL 端到端仍在 test_invitation_permits.py（需要隔离库与 V17 迁移）。
这里用 SQLite + MySQL 语法适配器，跑的是**真正的** permits 代码路径，包括
"必须同时拿到两个排他锁"这道证据检查；GET_LOCK/RELEASE_LOCK 用等价的
SQLite 函数模拟，所以控制流（含释放）是真的。
"""
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from fastapi.testclient import TestClient

from smsinvit.app import Services,create_app
from smsinvit.config import Config
from smsinvit.db import Database
from smsinvit.errors import Error
from smsinvit.security import Principal
from smsinvit.invitation import mysql
from smsinvit.invitation.permits import RESOLUTIONS,holds_router

SCHEMA = """
CREATE TABLE invitation_v2_secret_values (
  fingerprint TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, plaintext_length INTEGER NOT NULL);
CREATE TABLE invitation_v2_cdk_codes (
  code TEXT PRIMARY KEY, batch TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'unused',
  email TEXT, mail_kind TEXT, inviter_email TEXT, inviter_subject TEXT, invite_link TEXT,
  invite_received_at REAL, invite_sent_at REAL, invite_processed_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0, fail_count INTEGER NOT NULL DEFAULT 0,
  proxy_used TEXT, created_at REAL, redeemed_at REAL, finished_at REAL,
  result_json TEXT, error TEXT);
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

URL = '/api/v1/admin/invitation/task-holds'


class HoldSqliteDatabase(Database):
    """MySQL 语法 → SQLite：只做必要等价改写，不改语义。"""

    def _prepare(self,sql,params):
        sql=sql.replace(' FOR UPDATE SKIP LOCKED','').replace(' FOR UPDATE','')
        sql=sql.replace(' ON DUPLICATE KEY UPDATE fingerprint=VALUES(fingerprint)',
                        ' ON CONFLICT(fingerprint) DO NOTHING')
        sql=sql.replace('INSERT IGNORE','INSERT OR IGNORE')
        sql=sql.replace('UTC_TIMESTAMP(3)','CURRENT_TIMESTAMP')
        return super()._prepare(sql,params)


@pytest.fixture
def holds(tmp_path,monkeypatch):
    path=tmp_path/'holds.sqlite3'
    with sqlite3.connect(path) as connection:connection.executescript(SCHEMA)
    engine=create_engine('sqlite:///'+str(path),
                         connect_args={'check_same_thread':False,'timeout':30},
                         pool_size=10,max_overflow=10,hide_parameters=True)
    # 另一实例持有排他锁时 GET_LOCK 必须返回 0：这是"执行器还在跑"的唯一证据。
    locks={'foreign':False,'released':[]}
    @event.listens_for(engine,'connect')
    def install(dbapi,record):
        dbapi.create_function('GET_LOCK',2,lambda name,timeout:0 if locks['foreign'] else 1)
        dbapi.create_function('RELEASE_LOCK',1,lambda name:locks['released'].append(name) or 1)
        dbapi.create_function('IS_USED_LOCK',1,lambda name:None if locks['foreign'] else 1)
    db=HoldSqliteDatabase(engine=engine)
    monkeypatch.setattr(mysql,'_services',None)
    monkeypatch.setattr(mysql,'_runtime_owner',None)
    monkeypatch.setattr(mysql,'_cache',{})
    monkeypatch.setattr(mysql,'_redaction_values',set())
    config=Config({'smsinvit.inventory-encryption-key':'b1'*32,
                   'smsinvit.inventory-fingerprint-key':'hold-test-fingerprint',
                   'smsinvit.jwt-secret':'hold-test-jwt-secret-over-thirty-two-chars'},environ={})
    service=Services(db,config)
    mysql.configure(service)
    state=SimpleNamespace(db=db,locks=locks,audited=[],actor=Principal('fixture-admin','fixture-session',('ADMIN',)))
    state.allowed={'invitation.read','invitation.manage','invitation.credentials'}
    def require(request,permission,*args,**kwargs):
        if permission not in state.allowed:raise Error('PERMISSION_DENIED','denied',403)
        return state.actor
    service.permissions=SimpleNamespace(require=require,allows=lambda actor,permission,*a,**k:permission in state.allowed)
    service.audit=SimpleNamespace(record=lambda *args:state.audited.append(args))
    service.auth=SimpleNamespace(authenticate=lambda token:state.actor)
    service.limiter=SimpleNamespace(check=lambda *args:None)
    app=create_app(config,db,services=service,include_domains=False,load_runtime=False)
    app.include_router(holds_router)
    state.config=config
    with TestClient(app) as client:
        state.client=client
        yield state
    db.close()


HEADERS = {'Authorization':'Bearer fixture'}


def seed(state,job_id=None,status='queued',email='hold@example.test',code='CDK-HOLD',hold=True):
    job_id=job_id or uuid4().hex
    marker=mysql.marker(job_id)
    state.db.execute('INSERT INTO invitation_v2_cdk_jobs(job_id,code,email,status,created_at) VALUES (?,?,?,?,?)',
                     marker,mysql.protected(code),email,status,1.0)
    state.db.execute('INSERT INTO invitation_v2_cdk_codes(code,email,status,created_at) VALUES (?,?,?,?)',
                     mysql.protected(code),email,status,1.0)
    if hold:
        state.db.execute('INSERT INTO invitation_v2_task_holds(job_id,original_status,reason) VALUES (?,?,?)',
                         marker,status,'RESTART_UNCERTAIN')
    return job_id


def unresolved(state,email):
    return state.db.scalar('SELECT COUNT(*) FROM invitation_v2_task_holds h JOIN invitation_v2_cdk_jobs j '
                           'ON j.job_id=h.job_id WHERE j.email=? AND h.resolved_at IS NULL',email)


def body(**overrides):
    payload={'resolution':'interrupted_confirmed','note':'Executors stopped; external result reviewed','confirmAllExecutorsStopped':True}
    payload.update(overrides)
    return payload


def statuses(state,email):
    return (state.db.scalar('SELECT status FROM invitation_v2_cdk_jobs j JOIN invitation_v2_task_holds h '
                            'ON h.job_id=j.job_id WHERE j.email=?',email),
            state.db.scalar('SELECT status FROM invitation_v2_cdk_codes WHERE email=?',email))


def test_list_requires_read_permission_and_exposes_no_card_code_or_job_error(holds):
    seed(holds,email='listed@example.test')
    assert holds.client.get(URL).status_code==401
    holds.allowed.discard('invitation.read')
    assert holds.client.get(URL,headers=HEADERS).status_code==403
    holds.allowed.add('invitation.read')
    response=holds.client.get(URL,headers=HEADERS)
    assert response.status_code==200
    payload=response.json()['data']
    assert payload['reviewRequiresExecutorsStopped'] is True
    assert len(payload['items'])==1
    item=payload['items'][0]
    assert item['email']=='listed@example.test' and item['originalStatus']=='queued'
    assert item['reason']=='RESTART_UNCERTAIN' and item['jobStatus']=='queued'
    assert 'CDK-HOLD' not in response.text
    assert set(item)=={'jobId','originalStatus','reason','createdAt','email','jobStatus','stage','progress'}


def test_resolve_requires_manage_credentials_note_and_stopped_executor_confirmation(holds):
    job=seed(holds)
    url=f'{URL}/{job}/resolve'
    holds.allowed.discard('invitation.credentials')
    assert holds.client.post(url,headers=HEADERS,json=body()).status_code==403
    holds.allowed.add('invitation.credentials')
    # bad() is VALIDATION_ERROR → 422.
    assert holds.client.post(url,headers=HEADERS,json=body(confirmAllExecutorsStopped=False)).status_code==422
    assert holds.client.post(url,headers=HEADERS,json=body(note='ab')).status_code==422
    assert holds.client.post(url,headers=HEADERS,json=body(note='line one\nline two')).status_code==422
    assert holds.client.post(url,headers=HEADERS,json=body(resolution='looks_fine')).status_code==422
    assert holds.client.post(url,headers=HEADERS,json=body(closeInterruptedTask='yes')).status_code==422
    assert not holds.audited and unresolved(holds,'hold@example.test')==1
    assert holds.client.post(f'{URL}/{uuid4().hex}/resolve',headers=HEADERS,json=body()).status_code==404


def test_resolve_is_refused_while_any_executor_still_holds_the_lock(holds):
    job=seed(holds)
    holds.locks['foreign']=True
    response=holds.client.post(f'{URL}/{job}/resolve',headers=HEADERS,json=body())
    assert response.status_code==409 and response.json()['error']['code']=='INVITATION_EXECUTOR_ACTIVE'
    assert not holds.audited and unresolved(holds,'hold@example.test')==1
    assert holds.locks['released']==[]


def test_resolve_lifts_the_hold_records_the_operator_and_keeps_the_note_encrypted(holds):
    job=seed(holds)
    note='Executors stopped; external result reviewed'
    response=holds.client.post(f'{URL}/{job}/resolve',headers=HEADERS,json=body(note=note))
    assert response.status_code==200
    assert response.json()['data']=={'resolved':True,'resolution':'interrupted_confirmed','interruptedTaskClosed':True}
    assert unresolved(holds,'hold@example.test')==0
    marker=mysql.marker(job)
    row=holds.db.one('SELECT resolved_by,resolution,note,confirmed_executors_stopped,resolved_at FROM invitation_v2_task_holds WHERE job_id=?',marker)
    assert row['resolved_by']=='fixture-admin' and row['resolution']=='interrupted_confirmed'
    assert row['confirmed_executors_stopped']==1 and row['resolved_at'] is not None
    assert row['note'].startswith('iv2$') and note not in row['note']
    assert mysql.revealed(row['note'])==note
    assert [entry[1] for entry in holds.audited]==['invitation.task_hold.resolve']
    audit=holds.audited[0]
    assert audit[2]=='invitation_task_hold' and audit[3]==job
    assert audit[4]['resolution']=='interrupted_confirmed'
    assert audit[4]['confirmedAllExecutorsStopped'] is True
    assert note not in str(audit)
    assert holds.locks['released']==['smsinvit:invitation:worker','smsinvit:python:worker']


@pytest.mark.parametrize('resolution',sorted(RESOLUTIONS))
@pytest.mark.parametrize('original',['queued','running'])
def test_resolve_never_turns_an_uncertain_task_into_a_success(holds,resolution,original):
    job=seed(holds,status=original,email=f'{original}@example.test')
    assert holds.client.post(f'{URL}/{job}/resolve',headers=HEADERS,json=body(resolution=resolution)).status_code==200
    job_status,card_status=statuses(holds,f'{original}@example.test')
    assert job_status=='failed' and card_status=='failed'
    assert 'done' not in (job_status,card_status)
    error=holds.db.scalar('SELECT error FROM invitation_v2_cdk_codes WHERE email=?',f'{original}@example.test')
    assert error and '可重新开始' in error


def test_resolve_is_idempotent_and_conflicts_on_a_second_attempt(holds):
    job=seed(holds)
    url=f'{URL}/{job}/resolve'
    assert holds.client.post(url,headers=HEADERS,json=body()).status_code==200
    second=holds.client.post(url,headers=HEADERS,json=body())
    assert second.status_code==409 and second.json()['error']['code']=='CONFLICT'
    assert len(holds.audited)==1


def test_release_without_closing_leaves_a_stale_task_untouched_for_the_operator(holds):
    job=seed(holds,status='running')
    response=holds.client.post(f'{URL}/{job}/resolve',headers=HEADERS,
                               json=body(closeInterruptedTask=False))
    assert response.status_code==200
    assert response.json()['data']['interruptedTaskClosed'] is False
    assert unresolved(holds,'hold@example.test')==0
    assert statuses(holds,'hold@example.test')==('running','running')


def test_a_released_hold_stops_blocking_its_card_while_an_unresolved_one_still_does(holds):
    blocked=seed(holds,email='blocked@example.test',code='CDK-BLOCKED')
    assert unresolved(holds,'blocked@example.test')==1
    assert holds.client.post(f'{URL}/{blocked}/resolve',headers=HEADERS,json=body()).status_code==200
    assert unresolved(holds,'blocked@example.test')==0


def test_both_locks_are_released_even_when_the_review_body_raises(holds):
    # 拿到两个锁之后才发现记录不存在：异常路径也必须把锁还回去，否则后续复核
    # 会被自己上一次的失败永远挡住。
    assert holds.client.post(f'{URL}/{uuid4().hex}/resolve',headers=HEADERS,json=body()).status_code==404
    assert holds.locks['released']==['smsinvit:invitation:worker','smsinvit:python:worker']

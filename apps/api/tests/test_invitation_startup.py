"""Invitation-only startup waits for explicit requests; no real worker or network."""
import asyncio
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import APIRouter
from smsinvit.app import create_app
from smsinvit.config import Config
from smsinvit.invitation.service import InvitationService


class Connection:
    def __init__(self,events):self.events=events
    def exec_driver_sql(self,sql):
        # 取锁属主是一次普通查询：它不该被记成"又拿了一次锁"。
        if 'RELEASE_LOCK' in sql:self.events.append('release-lock')
        elif 'CONNECTION_ID' in sql:self.events.append('owner-id')
        else:self.events.append('acquire-lock')
        return SimpleNamespace(scalar=lambda:1)
    def close(self):self.events.append('close-lock')


@pytest.mark.parametrize('global_enabled,invitation_override,expected',[
    (False,None,[]),(False,'false',[]),
    (False,'true',['invite-start:False']),
    (True,None,['acquire-lock','invite-start:True','scheduler-start']),
    (True,'false',['acquire-lock','invite-start:True','scheduler-start']),
    (True,'true',['acquire-lock','invite-start:True','scheduler-start']),
])
def test_lifespan_independent_invitation_flag_keeps_global_scheduler_off(monkeypatch,global_enabled,invitation_override,expected):
    events=[]
    env={'SMSINVIT_WORKER_ENABLED':str(global_enabled).lower(),'SMSINVIT_STATIC_DIR':'/tmp/no-invitation-startup-static'}
    if invitation_override is not None:env['SMSINVIT_INVITATION_WORKER_ENABLED']=invitation_override
    config=Config(environ=env)
    scheduler=SimpleNamespace(start=lambda:events.append('scheduler-start'),stop=lambda:events.append('scheduler-stop'),thread=None)
    invitation=SimpleNamespace(initialize_executor_enabled=lambda default:default,initialize_pool_enabled=lambda default:default,start=lambda *,background:events.append('invite-start:'+str(background)),stop=lambda:events.append('invite-stop') or True)
    db=SimpleNamespace(engine=SimpleNamespace(connect=lambda:Connection(events)))
    services=SimpleNamespace(attach_domains=Mock(),scheduler=scheduler,invitation=invitation)
    monkeypatch.setattr('smsinvit.invitation.router.build_router',lambda _:APIRouter())
    app=create_app(config,db,services=services,load_runtime=False)
    async def lifecycle():
        async with app.router.lifespan_context(app):assert events==expected
    asyncio.run(lifecycle())
    if global_enabled:
        assert events==expected+['scheduler-stop','invite-stop','release-lock','close-lock']
    elif invitation_override=='true':assert events==expected+['invite-stop']
    else:assert events==[]


@pytest.mark.parametrize('background',[False,True])
def test_invitation_start_stop_background_split_preserves_empty_queue_and_restart_holds(monkeypatch,background):
    import smsinvit.invitation.service as module
    events=[];writes=[];started_background=[]
    service=InvitationService.__new__(InvitationService)
    service._started=False;service._stopping=False;service._process_lock=None
    service.db=SimpleNamespace(engine=SimpleNamespace(connect=lambda:Connection(events)),execute=lambda sql:writes.append(sql))
    service._execution_self_check=lambda:events.append('cli-check')
    service.gate=SimpleNamespace(open=lambda:events.append('gate-open'),close=lambda:events.append('gate-close'),inflight=0)
    controller=SimpleNamespace(start_workers=lambda:events.append('empty-workers'),_workers=[],_lock=threading.RLock(),_q=queue.Queue(),_started=False)
    service.cdk=SimpleNamespace(CONTROLLER=controller)
    pool=SimpleNamespace(start=lambda:events.append('pool-start'),stop=lambda:events.append('pool-stop'),_thread=SimpleNamespace(join=Mock(),is_alive=lambda:False))
    service.pool=pool
    def importer(name):
        if not background:assert name=='webui.sms_prices', 'Invitation-only startup must not load the refund worker'
        label='prices' if name=='webui.sms_prices' else 'refund'
        assert name in {'webui.sms_prices','webui.sms_refund'}
        worker=SimpleNamespace(start=lambda:events.append(label+'-start'),stop=lambda:events.append(label+'-stop'),_thread=SimpleNamespace(join=Mock(),is_alive=lambda:False))
        started_background.append(worker)
        return SimpleNamespace(MONITOR=worker,WORKER=worker)
    monkeypatch.setattr(module.importlib,'import_module',importer)
    service.start(background=background)
    assert service._started and controller._q.empty()
    assert events==['acquire-lock','owner-id','cli-check','gate-open','empty-workers','prices-start','pool-start']+(['refund-start'] if background else [])
    assert len(writes)==1 and 'RESTART_UNCERTAIN' in writes[0] and "status IN ('queued','running')" in writes[0]
    service.start(background=background)
    assert len(writes)==1
    assert service.stop() is True and service.stop() is True
    expected_stop=['gate-close','prices-stop','pool-stop']+(['refund-stop'] if background else [])+['release-lock','close-lock']
    assert events[-len(expected_stop):]==expected_stop
    assert all(worker._thread.join.call_count==1 for worker in started_background)
    assert pool._thread.join.call_count==1
    assert not service._started and service._process_lock is None and controller._q.empty()


def test_invitation_only_failed_cli_preflight_releases_lock_before_any_worker(monkeypatch):
    events=[]
    service=InvitationService.__new__(InvitationService)
    service._started=False;service._stopping=False;service._process_lock=None
    service.db=SimpleNamespace(engine=SimpleNamespace(connect=lambda:Connection(events)))
    def fail():raise RuntimeError('synthetic CLI unavailable')
    service._execution_self_check=fail
    service.gate=SimpleNamespace(open=Mock())
    service.cdk=SimpleNamespace(CONTROLLER=SimpleNamespace(start_workers=Mock()))
    with pytest.raises(RuntimeError,match='synthetic CLI'):service.start(background=False)
    assert events==['acquire-lock','owner-id','release-lock','close-lock'] and not service._started
    service.gate.open.assert_not_called();service.cdk.CONTROLLER.start_workers.assert_not_called()


def test_invitation_only_failed_price_monitor_start_is_stopped_and_releases_owner(monkeypatch):
    import smsinvit.invitation.service as module
    events=[]
    service=InvitationService.__new__(InvitationService)
    service._started=False;service._stopping=False;service._process_lock=None
    service.db=SimpleNamespace(engine=SimpleNamespace(connect=lambda:Connection(events)),execute=lambda _:None)
    service._execution_self_check=lambda:None
    service.gate=SimpleNamespace(open=lambda:None,close=lambda:events.append('gate-close'),inflight=0)
    controller=SimpleNamespace(start_workers=lambda:None,_workers=[],_lock=threading.RLock(),_q=queue.Queue(),_started=False)
    service.cdk=SimpleNamespace(CONTROLLER=controller)
    def fail():raise RuntimeError('synthetic monitor startup failed')
    monitor=SimpleNamespace(start=fail,stop=lambda:events.append('prices-stop'),_thread=None)
    def importer(name):
        assert name=='webui.sms_prices'
        return SimpleNamespace(MONITOR=monitor)
    monkeypatch.setattr(module.importlib,'import_module',importer)
    with pytest.raises(RuntimeError,match='synthetic monitor'):service.start(background=False)
    assert events==['acquire-lock','owner-id','gate-close','prices-stop','release-lock','close-lock']
    assert not service._started and service._process_lock is None


def test_pool_only_startup_runs_the_executor_process_without_the_scheduler(monkeypatch):
    """只开账号工厂、关着邀请执行：执行进程照起（补号要它），但 Scheduler 不动。"""
    events=[]
    env={'SMSINVIT_WORKER_ENABLED':'false','SMSINVIT_INVITATION_WORKER_ENABLED':'false',
         'SMSINVIT_INVITATION_POOL_ENABLED':'true','SMSINVIT_STATIC_DIR':'/tmp/no-invitation-startup-static'}
    config=Config(environ=env)
    scheduler=SimpleNamespace(start=lambda:events.append('scheduler-start'),stop=lambda:events.append('scheduler-stop'),thread=None)
    invitation=SimpleNamespace(initialize_executor_enabled=lambda default:default,
                               initialize_pool_enabled=lambda default:default,
                               start=lambda *,background:events.append('invite-start:'+str(background)),
                               stop=lambda:events.append('invite-stop') or True)
    db=SimpleNamespace(engine=SimpleNamespace(connect=lambda:Connection(events)))
    services=SimpleNamespace(attach_domains=Mock(),scheduler=scheduler,invitation=invitation)
    monkeypatch.setattr('smsinvit.invitation.router.build_router',lambda _:APIRouter())
    app=create_app(config,db,services=services,load_runtime=False)
    async def lifecycle():
        async with app.router.lifespan_context(app):
            assert events==['invite-start:False']
    asyncio.run(lifecycle())
    assert events==['invite-start:False','invite-stop']


def test_executor_only_startup_still_ignores_the_pool_switch(monkeypatch):
    """反向：只开邀请执行、账号工厂关着 —— 起进程，Scheduler 仍不动。"""
    events=[]
    env={'SMSINVIT_WORKER_ENABLED':'false','SMSINVIT_INVITATION_WORKER_ENABLED':'true',
         'SMSINVIT_INVITATION_POOL_ENABLED':'false','SMSINVIT_STATIC_DIR':'/tmp/no-invitation-startup-static'}
    config=Config(environ=env)
    scheduler=SimpleNamespace(start=lambda:events.append('scheduler-start'),stop=lambda:events.append('scheduler-stop'),thread=None)
    invitation=SimpleNamespace(initialize_executor_enabled=lambda default:default,
                               initialize_pool_enabled=lambda default:default,
                               start=lambda *,background:events.append('invite-start:'+str(background)),
                               stop=lambda:events.append('invite-stop') or True)
    db=SimpleNamespace(engine=SimpleNamespace(connect=lambda:Connection(events)))
    services=SimpleNamespace(attach_domains=Mock(),scheduler=scheduler,invitation=invitation)
    monkeypatch.setattr('smsinvit.invitation.router.build_router',lambda _:APIRouter())
    app=create_app(config,db,services=services,load_runtime=False)
    async def lifecycle():
        async with app.router.lifespan_context(app):
            assert events==['invite-start:False']
    asyncio.run(lifecycle())
    assert events==['invite-start:False','invite-stop']

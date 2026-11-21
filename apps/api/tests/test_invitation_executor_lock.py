"""执行器排他锁：会话被数据库回收后必须补拿，而不是永久 503。

2026-10-12 线上事故：MySQL 的空闲连接被 `wait_timeout`（默认 8 小时）回收，
`GET_LOCK` 的命名锁随会话静默释放，进程却仍以为自己是执行器，于是每一次
「开始」都被判成"没有所有权"返回 503，只能重启进程恢复。这里的假连接完整
模拟"锁属于会话、会话结束锁就没了"的语义，用来钉住三条性质：

1. 锁空闲（= 没有任何实例在跑）时就地补拿，不永久拒绝；
2. 锁被别人持有时绝不抢，继续拒绝收活；
3. 公开页面的只读判断不许有副作用 —— 前台说"可以开始"、接口却 503 是本次
   事故里最误导人的表现。
"""
import queue
import re
import threading
import time
from types import SimpleNamespace

import pytest

import smsinvit.invitation.service as module
from smsinvit.errors import Error
from smsinvit.invitation.service import EXECUTOR_LOCK_NAME, InvitationService
from smsinvit.db import start_lock_keepalive

WORKER_LOCK_NAME='smsinvit:python:worker'


def _lock_name(sql):
    match=re.search(r"'([^']+)'",sql)
    assert match,'no lock name in '+sql
    return match.group(1)


class _Result:
    def __init__(self,value):self.value=value
    def scalar(self):return self.value


class FakeLocks:
    """MySQL 命名锁：锁属于会话，会话结束（关闭或被回收）就自动释放。"""

    def __init__(self):self.holders={}

    def acquire(self,name,owner):
        if name in self.holders:return 0
        self.holders[name]=owner;return 1

    def release(self,name,owner):
        if self.holders.get(name)!=owner:return 0
        del self.holders[name];return 1

    def owner(self,name):return self.holders.get(name)

    def reap(self,connection):
        """模拟 wait_timeout：空闲会话被服务器收掉，它持有的锁随之消失。"""
        connection.dead=True
        for name,owner in list(self.holders.items()):
            if owner==connection.id:del self.holders[name]


class FakeConnection:
    def __init__(self,locks,identity):
        self.locks=locks;self.id=identity;self.dead=False

    def exec_driver_sql(self,sql):
        if self.dead:raise RuntimeError('MySQL server has gone away')
        if 'GET_LOCK' in sql:return _Result(self.locks.acquire(_lock_name(sql),self.id))
        if 'RELEASE_LOCK' in sql:return _Result(self.locks.release(_lock_name(sql),self.id))
        if 'IS_USED_LOCK' in sql:return _Result(self.locks.owner(_lock_name(sql)))
        if 'CONNECTION_ID' in sql:return _Result(self.id)
        return _Result(1)

    def close(self):
        self.locks.reap(self);self.dead=True


class FakeEngine:
    def __init__(self):self.locks=FakeLocks();self.identity=0

    def connect(self):
        self.identity+=1;return FakeConnection(self.locks,self.identity)


def build(monkeypatch,*,started=True):
    """一个不碰真数据库、也不起真 worker 的 InvitationService。"""
    original=module.importlib.import_module
    worker=SimpleNamespace(start=lambda:None,stop=lambda:None,
                           _thread=SimpleNamespace(join=lambda timeout=None:None,is_alive=lambda:False))
    def importer(name):
        if name in ('webui.sms_prices','webui.sms_refund'):return SimpleNamespace(MONITOR=worker,WORKER=worker)
        return original(name)
    monkeypatch.setattr(module.importlib,'import_module',importer)
    monkeypatch.setattr('smsinvit.invitation.dispatch.dispatch_gaps',lambda invitation:[])
    engine=FakeEngine()
    service=InvitationService.__new__(InvitationService)
    service.db=SimpleNamespace(engine=engine,execute=lambda *args:None)
    service.repo=SimpleNamespace(recover_runs_after_restart=lambda:0,get_setting=lambda key,default=None:default,
                                 get_mail_settings=lambda:{})
    service._started=started;service._stopping=False;service._process_lock=None;service._process_lock_owner=None
    service._executor_enabled=True;service._executor_lock=threading.RLock()
    service._execution_self_check=lambda:None
    service.gate=SimpleNamespace(open=lambda:None,close=lambda:None,inflight=0)
    service.cdk=SimpleNamespace(CONTROLLER=SimpleNamespace(
        start_workers=lambda:None,worker_alive=lambda:1,ensure_workers=lambda:1,
        stats=lambda:{'max_concurrency':2,'worker_alive':1},
        _workers=[],_lock=threading.RLock(),_q=queue.Queue(),_started=False))
    service.pool=SimpleNamespace(start=lambda:None,stop=lambda:None,
                                 _thread=SimpleNamespace(join=lambda timeout=None:None,is_alive=lambda:False))
    service.proxy=SimpleNamespace(has_available=lambda:True)
    service.alloc=SimpleNamespace(pool_available=lambda *args:1)
    service._mailbox_readiness=lambda *args:(True,'')
    return service,engine


def test_lock_released_by_the_database_is_reacquired_instead_of_refusing(monkeypatch):
    service,engine=build(monkeypatch)
    assert service._lock_acquire() is True
    first=service._process_lock.id
    engine.locks.reap(service._process_lock)
    assert engine.locks.owner(EXECUTOR_LOCK_NAME) is None
    assert service.owns_executor_lock() is True
    assert engine.locks.owner(EXECUTOR_LOCK_NAME)==service._process_lock.id
    assert service._process_lock.id!=first
    service.require_executor_accepting()


def test_readonly_ownership_check_never_reacquires_the_lock(monkeypatch):
    """公开页面必须只读：页面说"可以开始"、接口却 503，正是事故里最误导人的表现。"""
    service,engine=build(monkeypatch)
    assert service._lock_acquire() is True
    engine.locks.reap(service._process_lock)
    assert service.holds_executor_lock() is False
    assert engine.locks.owner(EXECUTOR_LOCK_NAME) is None
    page=service.page_content()
    assert page['canStart'] is False and page['unavailableMessage']=='服务暂时不可用，请稍后重试。'
    assert service.executor_status()['state']=='unavailable'


def test_lock_held_by_another_instance_is_never_stolen(monkeypatch):
    service,engine=build(monkeypatch)
    engine.locks.acquire(EXECUTOR_LOCK_NAME,999)
    service._process_lock=engine.connect()
    assert service._lock_acquire() is False
    assert service.owns_executor_lock() is False
    assert engine.locks.owner(EXECUTOR_LOCK_NAME)==999
    with pytest.raises(Error) as error:service.require_executor_accepting()
    assert error.value.code=='INVITATION_EXECUTOR_UNAVAILABLE' and error.value.status==503


def test_keepalive_reacquires_the_lock_without_a_restart(monkeypatch):
    monkeypatch.setattr(module,'LOCK_KEEPALIVE_SECONDS',0.05)
    service,engine=build(monkeypatch)
    assert service._lock_acquire() is True
    service._start_lock_keepalive()
    try:
        engine.locks.reap(service._process_lock)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and not service.holds_executor_lock():time.sleep(0.02)
        assert service.holds_executor_lock() is True
        assert engine.locks.owner(EXECUTOR_LOCK_NAME)==service._process_lock.id
    finally:service._stop_lock_keepalive()
    assert service._lock_keepalive is None


def test_start_starts_the_keepalive_and_stop_releases_the_lock(monkeypatch):
    service,engine=build(monkeypatch,started=False)
    service.start(background=False)
    keepalive=service._lock_keepalive
    assert keepalive is not None and keepalive.is_alive()
    assert engine.locks.owner(EXECUTOR_LOCK_NAME)==service._process_lock.id
    assert service.stop() is True
    assert not keepalive.is_alive() and service._lock_keepalive is None
    assert engine.locks.owner(EXECUTOR_LOCK_NAME) is None


def test_start_refuses_a_second_owner_instead_of_stealing(monkeypatch):
    service,engine=build(monkeypatch,started=False)
    engine.locks.acquire(EXECUTOR_LOCK_NAME,999)
    with pytest.raises(RuntimeError,match='already active'):service.start(background=False)
    assert not service._started and service._process_lock is None
    assert engine.locks.owner(EXECUTOR_LOCK_NAME)==999


def test_global_worker_lock_keepalive_restores_a_released_lock(monkeypatch):
    """`smsinvit:python:worker` 是同一类锁，丢锁等于"单实例"保证失效。"""
    engine=FakeEngine();connection=engine.connect()
    assert engine.locks.acquire(WORKER_LOCK_NAME,connection.id)==1
    stop,thread=start_lock_keepalive(connection,WORKER_LOCK_NAME,interval=0.05)
    try:
        assert engine.locks.release(WORKER_LOCK_NAME,connection.id)==1
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and engine.locks.owner(WORKER_LOCK_NAME)!=connection.id:time.sleep(0.02)
        assert engine.locks.owner(WORKER_LOCK_NAME)==connection.id
    finally:
        stop.set();thread.join(timeout=5)
    assert not thread.is_alive()

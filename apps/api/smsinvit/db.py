"""Parameterized MySQL access with transactional, thread/context-local connections.

REQUIRES_NEW suspends an outer connection instead of committing it. Each Database
owns its ContextVar; task/thread contexts cannot share a connection accidentally.
"""
from __future__ import annotations
from contextlib import contextmanager
import asyncio
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID
import re
from sqlalchemy import create_engine, event

log=logging.getLogger(__name__)

def start_lock_keepalive(connection,name,interval=60,logger=None):
    """进程级 MySQL 命名锁的保活线程。

    命名锁是**会话级**的：会话一结束，服务器就自动放锁，而且不会通知任何人；
    空闲会话又会被 `wait_timeout`（默认 8 小时）回收。2026-10-12 线上执行器就是
    这样在没人操作的一夜里静默丢锁，之后每一次「开始」都被判成"没有所有权"。
    定期碰一下锁连接既能让会话存活，也能在真被回收时就地补拿 —— `IS_USED_LOCK`
    为空说明没有任何实例持有它，`GET_LOCK` 又是原子的，真有第二个实例在抢也只
    会有一个成功。返回 `(stop_event, thread)`；调用方在释放锁之前必须先
    `stop_event.set()` 并 join，绝不能让保活线程和释放动作并发用同一条连接。
    """
    logger=logger or log
    stop=threading.Event()
    def loop():
        while not stop.wait(interval):
            try:
                holder=connection.exec_driver_sql(f"SELECT IS_USED_LOCK('{name}')").scalar()
                if holder is None:
                    acquired=connection.exec_driver_sql(f"SELECT GET_LOCK('{name}',0)").scalar()
                    logger.warning('Named lock %s had been released by the database; re-acquire=%s',name,acquired)
            except Exception:
                logger.warning('Named lock %s keepalive failed',name,exc_info=True)
    thread=threading.Thread(target=loop,daemon=True,name='lock-keepalive:'+name)
    thread.start()
    return stop,thread

@dataclass(frozen=True)
class Result:
    rowcount: int
    lastrowid: int | None = None
    def __int__(self): return self.rowcount
    def __eq__(self, other):
        if isinstance(other, int): return self.rowcount == other
        if isinstance(other, Result): return (self.rowcount,self.lastrowid)==(other.rowcount,other.lastrowid)
        return False

@dataclass
class TransactionState:
    connection: object
    owner: tuple
    rollback_only: bool = False

def owner_identity():
    try:task=asyncio.current_task()
    except RuntimeError:task=None
    return threading.get_ident(),id(task) if task is not None else None

class Database:
    def __init__(self, url=None, *, engine=None, pool_size=10, connect_args=None):
        self.engine = engine if engine is not None else create_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=10, isolation_level="REPEATABLE READ", connect_args=connect_args or {}, hide_parameters=True)
        self._active = ContextVar(f"smsinvit_db_{id(self)}", default=None)
        if self.engine.dialect.name == "mysql":
            @event.listens_for(self.engine, "connect")
            def configure(dbapi, record):
                with dbapi.cursor() as c:
                    c.execute("SET time_zone = '+00:00'")
                    c.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
    def _state(self):
        state=self._active.get()
        if state is not None and state.owner!=owner_identity():
            raise RuntimeError('Transaction context may not be shared across concurrent workers')
        return state
    @property
    def in_transaction(self): return self._state() is not None
    @contextmanager
    def transaction(self, requires_new=False):
        current=self._state()
        if current is not None and not requires_new:
            try:yield self
            except BaseException:
                current.rollback_only=True
                raise
            return
        with self.engine.connect() as connection:
            state=TransactionState(connection,owner_identity());token=self._active.set(state)
            try:
                with connection.begin():
                    yield self
                    if state.rollback_only:raise RuntimeError('Transaction marked rollback-only by an inner failure')
            finally:self._active.reset(token)
    def _args(self, params):
        if len(params)==1 and isinstance(params[0],(tuple,list)): params=tuple(params[0])
        return tuple(str(p) if isinstance(p,UUID) else p.astimezone(timezone.utc).replace(tzinfo=None) if isinstance(p,datetime) and p.tzinfo else p for p in params)
    def _prepare(self, sql, params):
        # SQL is trusted source code. Replace placeholders outside strings/comments;
        # SQLAlchemy supplies an empty argument tuple even for a no-argument call;
        # PyMySQL therefore needs literal percent signs escaped on every call.
        output=[]; i=0; quote=None
        while i<len(sql):
            ch=sql[i]
            if quote:
                if ch==quote:
                    if i+1<len(sql) and sql[i+1]==quote: output += [ch,ch];i+=2;continue
                    quote=None
                if ch=='\\' and i+1<len(sql): output += [ch,sql[i+1]];i+=2;continue
                output.append('%%' if ch=='%' and self.engine.dialect.name=='mysql' else ch);i+=1;continue
            if ch in "'\"`": quote=ch;output.append(ch);i+=1;continue
            if ch=='?': output.append('%s' if self.engine.dialect.name=='mysql' else '?');i+=1;continue
            if ch=='%' and i+1<len(sql) and sql[i+1]=='s': output.append('%s' if self.engine.dialect.name=='mysql' else '?');i+=2;continue
            if ch=='%' and self.engine.dialect.name=='mysql': output.append('%%');i+=1;continue
            output.append(ch);i+=1
        return ''.join(output)
    def _run(self, sql, params, mode):
        args=self._args(params); sql=self._prepare(sql,args)
        state=self._state();connection=state.connection if state is not None else None
        def run(c):
            result=c.exec_driver_sql(sql,args) if args else c.exec_driver_sql(sql)
            if mode=='execute': return Result(result.rowcount,getattr(result,'lastrowid',None))
            rows=[dict(row) for row in result.mappings()]
            return rows if mode=='all' else rows[0] if rows else None
        if connection is not None: return run(connection)
        with self.engine.begin() as c: return run(c)
    def one(self, sql, *params): return self._run(sql,params,'one')
    def all(self, sql, *params): return self._run(sql,params,'all')
    def execute(self, sql, *params): return self._run(sql,params,'execute')
    def scalar(self, sql, *params, default=None):
        row=self.one(sql,*params);return next(iter(row.values())) if row else default
    @contextmanager
    def advisory_lock(self, name, timeout=0):
        with self.transaction():
            acquired=self.scalar("SELECT GET_LOCK(?,?)",name,timeout)==1
            try: yield acquired
            finally:
                if acquired: self.scalar("SELECT RELEASE_LOCK(?)",name)
    def close(self): self.engine.dispose()

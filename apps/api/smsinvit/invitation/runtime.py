"""Freeze per-task settings and share one execution and mailbox boundary."""
from contextvars import ContextVar
from types import MappingProxyType
import functools
import logging
import threading
from ..errors import Error
from . import mysql
logger=logging.getLogger(__name__)
TASK_SETTINGS=ContextVar('invitation_task_settings',default=None)
# 阶段（进度）发射器：进度是在很深的调用里产生的（auth_flow 的 PoW、sms_provider
# 的租号/接码），那些模块不能 import codex_auth（会成环）。作业开始时把发射器放进
# 这个 contextvar，深层代码用 emit_task_stage() 取用。
STAGE_EMITTER=ContextVar('invitation_stage_emitter',default=None)


def emit_task_stage(name, **fields):
    """给当前任务发一个阶段；没有发射器（例如单测）就静默跳过。

    fields 是可选的阶段事件数据（例如接码金额）。历史上的发射器只接受阶段名，
    这种情况下退回单参数调用，阶段名与事件名保持不变。
    """
    emitter=STAGE_EMITTER.get()
    if emitter is None:return
    try:
        if not fields:emitter(name)
        else:
            try:emitter(name,**fields)
            except TypeError:emitter(name)
    except Exception:logger.debug('stage emit failed: %s',name,exc_info=True)
ACTIVE_TASK=ContextVar('invitation_active_task',default=False)

class TaskGate:
    """Invitation concurrency gate with two independent quota families.

    邀请作业与账号工厂补号各占各的工位（见 permits.INVITE_SLOTS/POOL_SLOTS），
    各读各的上限设置，互不挤占：
      · 邀请族   → cdk_max_concurrency（1..INVITE_SLOTS 的宽度，即 1..16）
      · 补号族   → pool_register_concurrency（1..10）
    上限以工位区间为唯一来源：配置能填 32 而工位只有 16 时，多出来的部分会让每个
    任务都以 "Invalid invitation concurrency limit" 失败。
    邮箱互斥（self.emails）仍然是全局的 —— 同一个邮箱任何时刻只跑一个任务，
    这是安全约束，不是配置。
    """
    def __init__(self,repo,permits=None):
        self.repo=repo;self.permits=permits;self.condition=threading.Condition();self.active=0;self.family_active={'invite':0,'pool':0};self.waiters=0;self.inflight=0;self.emails=set();self.closed=False
    def close(self):
        with self.condition:self.closed=True;self.condition.notify_all()
    def open(self):
        with self.condition:
            if self.inflight:raise RuntimeError('Cannot reopen an executor with unfinished tasks')
            self.closed=False
    def _email(self,original,args,kwargs):
        if original.__name__=='run_invite_job':
            row=self.repo.get_cdk_code(args[0] if args else kwargs.get('code','')) or {};value=row.get('email','')
        elif original.__name__=='_do_register':
            account=args[1] if len(args)>1 else kwargs.get('account',{});value=account.get('email','')
        else:value=args[0] if args else kwargs.get('email','')
        return str(value or '').strip().lower()
    @staticmethod
    def _family(original,args,kwargs):
        """补号注册 vs 邀请作业：只看这次 _do_register 是不是账号工厂模式。"""
        if original.__name__!='_do_register':return 'invite'
        options=args[2] if len(args)>2 else kwargs.get('options')
        try:return 'pool' if bool((options or {}).get('pool_only')) else 'invite'
        except AttributeError:return 'invite'
    def _wait_limit(self,family):
        """等工位的硬上限（秒）。超时抛 503，不再无限挂死。"""
        default=300 if family=='pool' else 900
        try:number=int(str(self.repo.get_setting('gate_wait_seconds',str(default))).strip())
        except (TypeError,ValueError):number=default
        return max(10,min(86400,number))
    def _limit(self,family):
        # 设置按任务实时读取；非法值回落到 1（该族下限），绝不放大额度。
        # 上限以**工位区间**为唯一来源：配置能填 32、工位只有 16 时，多出来的
        # 部分会让每个任务都以 "Invalid invitation concurrency limit" 失败。
        if family=='pool':value,low,high=self.repo.get_setting('pool_register_concurrency','2'),1,10
        else:
            from .permits import INVITE_SLOTS
            value,low,high=self.repo.get_setting('cdk_max_concurrency','3'),1,INVITE_SLOTS[1]
        try:number=int(str(value).strip())
        except (TypeError,ValueError):number=low
        return max(low,min(high,number))
    @staticmethod
    def _slots(family):
        from .permits import INVITE_SLOTS,POOL_SLOTS
        return POOL_SLOTS if family=='pool' else INVITE_SLOTS
    def decorate(self,original):
        @functools.wraps(original)
        def task(*args,**kwargs):
            if ACTIVE_TASK.get():return original(*args,**kwargs)
            with self.condition:self.inflight+=1
            admitted=False;email='';family='invite';one=two=process_token=sms_token=None;registrar=None;old_run=None;permit=None
            try:
                email=self._email(original,args,kwargs)
                family=self._family(original,args,kwargs)
                with self.condition:
                    self.waiters+=1
                    try:
                        waited=0.0;noted=0.0
                        while not self.closed:
                            limit=self._limit(family)
                            if self.family_active[family]<limit and (not email or email not in self.emails):
                                task_id=kwargs.get('job_id') or kwargs.get('run_id') or (args[0] if args else '')
                                permit=self.permits.claim(email,task_id,limit,self._slots(family)) if self.permits else True
                                if permit:break
                            # 等待必须可见：以前这里静默无限等，run 停在 running、
                            # 日志 0 字节，从外部完全看不出卡在哪（实测踩到占位泄漏）。
                            if waited>=self._wait_limit(family):
                                raise Error('INVITATION_EXECUTOR_UNAVAILABLE',
                                            f'等待{"补号" if family=="pool" else "邀请"}工位超时（已等 {int(waited)} 秒），本次未启动。',503)
                            self.condition.wait(timeout=.2);waited+=.2
                            if waited-noted>=30:
                                noted=waited
                                logger.warning('[gate] 等待%s工位已 %.0f 秒（%s 族在用 %s/%s，邮箱=%s）',
                                               '补号' if family=='pool' else '邀请',waited,family,
                                               self.family_active[family],limit,email or '-')
                        if self.closed:raise Error('INVITATION_EXECUTOR_UNAVAILABLE','执行实例正在停止。',503)
                        self.active+=1;self.family_active[family]+=1;admitted=True
                        if email:self.emails.add(email)
                    finally:self.waiters-=1
                # Loading/decrypting a snapshot is inside the same cleanup scope.
                settings=MappingProxyType({r['key']:mysql.revealed(r['value'],sensitive=mysql.is_secret_setting(r['key'])) for r in mysql.services().db.all('SELECT `key`,value FROM invitation_v2_settings')})
                one=TASK_SETTINGS.set(settings);two=ACTIVE_TASK.set(True)
                from .sms_state import ACTIVE_SMS_RUN
                sms_run=kwargs.get('run_id') or (args[0] if original.__name__=='_do_register' and args else '')
                sms_token=ACTIVE_SMS_RUN.set(str(sms_run or '').strip() or None)
                from .process import UNCERTAIN_PROCESS
                process_token=UNCERTAIN_PROCESS.set(False)
                if original.__name__=='_do_register':
                    from webui import registrar
                    old_run=getattr(registrar._current_run,'run_id',None)
                    registrar._current_run.run_id=args[0] if args else kwargs.get('run_id')
                return original(*args,**kwargs)
            finally:
                if registrar is not None:registrar._current_run.run_id=old_run
                if one is not None:TASK_SETTINGS.reset(one)
                if two is not None:ACTIVE_TASK.reset(two)
                if sms_token is not None:
                    from .sms_state import ACTIVE_SMS_RUN
                    ACTIVE_SMS_RUN.reset(sms_token)
                from .process import UNCERTAIN_PROCESS
                process_uncertain=UNCERTAIN_PROCESS.get() if process_token is not None else False
                if process_token is not None:UNCERTAIN_PROCESS.reset(process_token)
                if self.permits and permit and not process_uncertain:
                    try:self.permits.release(permit)
                    except Exception:
                        # A lost database connection retains the durable permit.
                        # A later process cannot quietly expand concurrency.
                        import logging
                        logging.getLogger(__name__).error('Invitation permit retained after database release failure')
                elif self.permits and permit and process_uncertain:
                    import logging
                    logging.getLogger(__name__).error('Invitation permit retained after uncertain child termination')
                with self.condition:
                    self.inflight-=1
                    if admitted:
                        self.active-=1
                        self.family_active[family]-=1
                        if email:self.emails.discard(email)
                    self.condition.notify_all()
        return task

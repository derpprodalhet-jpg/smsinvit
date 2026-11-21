from __future__ import annotations
import importlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from uuid import UUID,uuid4
from datetime import datetime,timedelta,timezone
from ..common.cdk import batch_no,BATCH_ZONE
from ..errors import Error,bad
from . import mysql
from .manual_sync import ManualCompletionMixin
from .pool import AccountPoolMaintainer
from .redeem_attempts import RedemptionAttemptMixin, public_invitation_error

VENDOR=Path(__file__).resolve().parents[4]/'vendor/codex_invit'
log=logging.getLogger(__name__)

# 单次请求最多提交多少条卡密/会话。这是**服务端**的硬上限，前端的切分与提示
# 都只是提前告知：即使绕过页面直接调接口，也必须在这里被拒。
INVITATION_BATCH_LIMIT = 10

# 执行器排他锁的名字与保活间隔。MySQL 的命名锁是**会话级**的：连接一断锁就自动
# 释放，而且不会通知进程；空闲会话又会被服务器的 `wait_timeout`（默认 8 小时）
# 回收。2026-10-12 线上就是这样在没人操作的一夜里静默丢锁，之后每一次「开始」
# 都被判成"没有所有权"永久 503（只有重启进程才能恢复）。所以锁连接必须保活，
# 真被回收时也要能就地补拿 —— 见 `_lock_maintain`。
EXECUTOR_LOCK_NAME='smsinvit:invitation:worker'
LOCK_KEEPALIVE_SECONDS=60

class InvitationService(RedemptionAttemptMixin,ManualCompletionMixin):
    def __init__(self,services):
        self.s=services;self.db=services.db;self._started=False;self._stopping=False;self._process_lock=None;self._process_lock_owner=None;self._lock_guard=threading.RLock();self._lock_keepalive=None;self._lock_keepalive_stop=None;self._background_workers=();self._mutex=threading.RLock();self._executor_lock=threading.RLock();self._executor_enabled=False
        mysql.configure(services,runtime=True)
        data=Path(str(services.config.get('SMSINVIT_INVITATION_DATA_DIR',str(VENDOR.parents[1]/'data/invitation')))).resolve()
        self.data_dir=data;self.log_dir=data/'logs'
        self.log_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.environ['SMSINVIT_INVITATION_LOG_DIR']=str(self.log_dir)
        os.environ['CODEX_HOMES_ROOT']=str(data/'codex-homes')
        if str(VENDOR) not in sys.path:sys.path.insert(0,str(VENDOR))
        self.native=importlib.import_module('webui.app')
        self.repo=importlib.import_module('webui.db')
        self.cdk=importlib.import_module('webui.cdk')
        self.alloc=importlib.import_module('webui.mail_alloc')
        self.proxy=importlib.import_module('webui.proxy_pool')
        for module in (self.native,self.repo,self.cdk,self.alloc,self.proxy):
            if not Path(module.__file__).resolve().is_relative_to(VENDOR.resolve()):raise RuntimeError('Invitation imports must originate in the managed copy')
        from .safety import install_log_redaction
        install_log_redaction()
        self._secure_controller()
        from .runtime import TaskGate
        from .permits import Permits
        self.gate=TaskGate(self.repo,Permits(self.db,services.codec,can_reclaim=self.owns_executor_lock))
        auth=importlib.import_module('webui.codex_auth');registrar=importlib.import_module('webui.registrar')
        for module,name in ((auth,'run_invite_job'),(auth,'authorize_registered_email'),(registrar,'_do_register')):
            current=getattr(module,name)
            if not getattr(current,'_smsinvit_gated',False):
                if name=='authorize_registered_email':current=self.managed_manual_authorizer(current)
                wrapped=self.gate.decorate(current);wrapped._smsinvit_gated=True;setattr(module,name,wrapped)
        # 账号工厂：注册到 2FA 停止的补号任务。与邀请执行各自独立开关/额度，
        # 但共用同一套进程内边界（TaskGate / 占位表 / 沙箱自检）。
        self.pool=AccountPoolMaintainer(self)

    def _secure_controller(self):
        controller=self.cdk.CONTROLLER
        if getattr(controller,'_platform_secured',False):return
        original=controller.enqueue
        def enqueue(code):
            code=str(code or '').strip()
            with self._executor_lock,controller._lock:
                with self.db.transaction():
                    marker=mysql.marker(code)
                    self._lock_codes([marker])
                    row=self.db.one('SELECT code,email,status FROM invitation_v2_cdk_codes WHERE code=?',marker)
                    if not row:return {'ok':False,'reason':'not_found','message':'未找到该卡密'}
                    self._require_no_holds(marker)
                    self._require_inventory_available(marker)
                    if row.get('status') in ('bound','failed'):self._require_mailbox_idle(row.get('email'))
                    self.require_executor_accepting()
                    result=original(code)
                if result.get('ok') and not result.get('already'):
                    controller.dispatch(result['job_id'],code)
                return result
        controller.enqueue=enqueue;controller._platform_secured=True

    def _release_email_permit(self,email):
        """释放某个邮箱的执行占位，返回释放掉的工位号（没有占位返回 None）。"""
        if not email:return None
        row=self.db.one('SELECT slot_id FROM invitation_v2_execution_permits WHERE email=? AND owner_token IS NOT NULL FOR UPDATE',email)
        if not row:return None
        result=self.db.execute('UPDATE invitation_v2_execution_permits SET owner_token=NULL,instance_id=NULL,email=NULL,task_id=NULL,acquired_at=NULL WHERE slot_id=? AND owner_token IS NOT NULL',row['slot_id'])
        return row['slot_id'] if result.rowcount==1 else None

    def stop_job(self,job_id,actor):
        """管理员手动停止一个邀请作业：置失败、登记复核、释放该邮箱的占位。

        绝不把卡密改写成成功；只有 queued/running 可以停，其它状态幂等返回当前状态。
        没跑起来的作业确定没有外部动作，直接把复核关掉让卡密可以立刻重置再用；
        已经 running 的作业可能已经产生了外部动作（接受邀请 / OAuth / 接码），
        保留未解除的复核对账，由运维在「任务复核」里确认后再复用。
        """
        raw=str(job_id or '').strip()
        marker=mysql.marker(raw) if raw else ''
        if not marker:raise bad('请提供要停止的作业标识。')
        with self.db.transaction():
            job=self.db.one('SELECT job_id,code,email,status FROM invitation_v2_cdk_jobs WHERE job_id=? FOR UPDATE',marker)
            if not job:raise Error('NOT_FOUND','未找到该作业。',404)
            original=str(job.get('status') or '')
            email=str(job.get('email') or '').strip().lower()
            released={'email':email or None,'permitSlot':None}
            if original not in ('queued','running'):
                hold=self.db.one('SELECT job_id FROM invitation_v2_task_holds WHERE job_id=? AND reason=? AND resolved_at IS NULL',marker,'ADMIN_STOPPED')
                return {'jobId':mysql.revealed(marker),'stopped':True,'released':released,'status':original,
                        'reviewRequired':bool(hold)}
            stopped_at=time.time()
            self.db.execute("UPDATE invitation_v2_cdk_jobs SET status='failed',finished_at=?,error=? WHERE job_id=? AND status IN ('queued','running')",
                            stopped_at,'管理员手动停止',marker)
            if job.get('code'):
                self.db.execute("UPDATE invitation_v2_cdk_codes SET status='failed',finished_at=?,error=? WHERE code=? AND status IN ('queued','running')",
                                stopped_at,'管理员手动停止',job['code'])
            self.db.execute("INSERT IGNORE INTO invitation_v2_task_holds(job_id,original_status,reason) VALUES (?,?,'ADMIN_STOPPED')",marker,original)
            released['permitSlot']=self._release_email_permit(email)
            review_required=original=='running'
            if not review_required:
                self.db.execute("UPDATE invitation_v2_task_holds SET resolved_at=UTC_TIMESTAMP(3),resolved_by=?,resolution=?,note=?,confirmed_executors_stopped=1 WHERE job_id=? AND resolved_at IS NULL",
                               actor.id,'not_started_confirmed','管理员手动停止：作业尚未开始执行。',marker)
            self.s.audit.record(actor.id,'invitation.job.stop','invitation_cdk_job',raw,
                                {'originalStatus':original,'email':email or None,'permitSlot':released['permitSlot'],'reviewRequired':review_required})
            out={'jobId':mysql.revealed(marker),'stopped':True,'released':released,'status':'failed','reviewRequired':review_required}
            if review_required:
                out['message']='该作业已在执行中，可能已经产生外部动作；复核未解除，请先在「任务复核」里确认后再复用卡密。'
            return out

    def stop_pool_round(self,actor):
        """手动停止本轮补号并把账号工厂开关置为关闭。"""
        stopped=self.pool.stop_round()
        try:
            with self.db.transaction():
                self.repo.set_setting('pool_auto_enabled','false')
                self.s.audit.record(actor.id,'invitation.pool.stop','invitation_account_pool',None,{'stoppedRounds':stopped})
        except Exception:
            raise Error('INVITATION_POOL_SAVE_FAILED','设置保存失败，请稍后重试。',503) from None
        return {'stopped':stopped}

    def initialize_executor_enabled(self,default):
        with self._executor_lock:
            value=self.repo.get_setting('invitation_executor_enabled',None)
            if value is None:self._executor_enabled=bool(default)
            elif str(value).lower() in {'true','false'}:self._executor_enabled=str(value).lower()=='true'
            else:raise Error('INVITATION_EXECUTOR_SETTING_INVALID','执行设置暂时无法读取。',503)
            return self._executor_enabled

    def initialize_pool_enabled(self,default):
        """账号工厂自己的开关；缺省用部署标志，非法值拒绝启动（fail closed）。"""
        value=self.repo.get_setting('pool_auto_enabled',None)
        if value is None:return bool(default)
        if str(value).lower() in {'true','false'}:return str(value).lower()=='true'
        raise Error('INVITATION_POOL_SETTING_INVALID','账号池设置暂时无法读取。',503)

    def pool_status(self):
        return self.pool.snapshot()

    def set_pool_enabled(self,enabled,actor):
        if not isinstance(enabled,bool):raise bad('启用设置必须为布尔值。')
        if enabled:
            # 只开补号、邀请执行关着也要能跑：这里同样惰性启动执行进程。
            try:
                if self._started:self._execution_self_check()
                self.start(background=False)
            except Exception:
                # 对外的文案必须保持笼统，但服务端日志要留下真实原因（内核不满足
                # Landlock、自检失败、排他锁被占用…），否则运维只能看到「暂时无法开启」。
                logging.getLogger(__name__).warning('Invitation pool start refused',exc_info=True)
                raise Error('INVITATION_POOL_START_FAILED','暂时无法开启，请稍后重试。',503) from None
        try:
            with self.db.transaction():
                self.repo.set_setting('pool_auto_enabled','true' if enabled else 'false')
                self.s.audit.record(actor.id,'invitation.pool.update','invitation_account_pool',None,{'enabled':enabled})
        except Exception:
            raise Error('INVITATION_POOL_SAVE_FAILED','设置保存失败，请稍后重试。',503) from None
        # 关开关必须真的把常驻线程停下来：只写配置的话，已经启动的补号巡检会继续跑。
        if not enabled:self.pool.stop()
        return self.pool_status()

    # ──────────────────── 常驻后台线程的手动开关 ────────────────────
    # 这些线程随执行进程启动，但管理员必须能单独关掉它们。开关存
    # invitation_v2_settings，重启后沿用；关掉后进程仍可被其它开关启动。
    BACKGROUND_WORKERS={'sms-monitor':('sms_monitor_enabled','webui.sms_prices','MONITOR'),
                        'sms-refund':('sms_refund_enabled','webui.sms_refund','WORKER')}
    def _background_setting(self,name):
        if name not in self.BACKGROUND_WORKERS:raise bad('未知的后台任务。')
        return self.BACKGROUND_WORKERS[name]
    def _background_enabled(self,key):
        repo=getattr(self,'repo',None)
        if repo is None:return True
        try:value=repo.get_setting(key,None)
        except Exception:return True
        if value is None:return True
        return str(value).strip().lower() in {'1','true','yes','on'}
    def _background_worker(self,name):
        _key,module_name,attribute=self._background_setting(name)
        return getattr(importlib.import_module(module_name),attribute)
    def background_worker_status(self,name):
        key,module_name,attribute=self._background_setting(name)
        thread=getattr(self._background_worker(name),'_thread',None)
        return {'enabled':self._background_enabled(key),'running':bool(thread is not None and thread.is_alive())}
    def set_background_worker(self,name,enabled,actor):
        if not isinstance(enabled,bool):raise bad('启用设置必须为布尔值。')
        key,module_name,attribute=self._background_setting(name)
        worker=self._background_worker(name)
        if enabled:worker.start()
        else:worker.stop()
        action='invitation.'+('sms_monitor' if name=='sms-monitor' else 'sms_refund')+'.update'
        try:
            with self.db.transaction():
                self.repo.set_setting(key,'true' if enabled else 'false')
                self.s.audit.record(actor.id,action,'invitation_background_worker',name,{'enabled':enabled})
        except Exception:
            raise Error('INVITATION_BACKGROUND_SAVE_FAILED','设置保存失败，请稍后重试。',503) from None
        return self.background_worker_status(name)

    def _lock_mutex(self):
        """锁连接是**独占**资源：保活线程、所有权检查、释放动作必须互斥。

        测试里用 `__new__` 拼出来的实例没有构造函数跑过的属性，所以按需补一个。
        """
        guard=getattr(self,'_lock_guard',None)
        if guard is None:guard=self.__dict__.setdefault('_lock_guard',threading.RLock())
        return guard

    def _lock_connect(self):
        """换一条新的锁连接。旧连接上的命名锁随旧会话一起消失，必须重新拿。"""
        current=getattr(self,'_process_lock',None)
        if current is not None:
            try:current.close()
            except Exception:pass
        self._process_lock=self.db.engine.connect()
        self._process_lock_owner=None
        return self._process_lock

    def _lock_acquire(self):
        """在锁连接上取得排他锁；连接已经不可用时换一条再试一次。"""
        for attempt in (0,1):
            connection=getattr(self,'_process_lock',None)
            if connection is None:connection=self._lock_connect()
            try:
                if connection.exec_driver_sql(f"SELECT GET_LOCK('{EXECUTOR_LOCK_NAME}',0)").scalar()!=1:return False
                self._process_lock_owner=connection.exec_driver_sql('SELECT CONNECTION_ID()').scalar()
                return self._process_lock_owner is not None
            except Exception:
                if attempt:raise
                log.warning('Invitation executor lock connection is unusable; reconnecting',exc_info=True)
                self._lock_connect()
        return False

    def _lock_state(self):
        """只读判断：'owned' | 'free' | 'foreign' | 'unknown'。"""
        connection=getattr(self,'_process_lock',None);owner=getattr(self,'_process_lock_owner',None)
        if connection is None or owner is None:return 'free'
        try:holder=connection.exec_driver_sql(f"SELECT IS_USED_LOCK('{EXECUTOR_LOCK_NAME}')").scalar()
        except Exception:return 'unknown'
        if holder is None:return 'free'
        return 'owned' if int(holder)==int(owner) else 'foreign'

    def _lock_maintain(self):
        """确认锁还在手上；被数据库静默回收（锁变空闲）时就地补拿。

        补拿是安全的：`IS_USED_LOCK` 为空说明**没有任何实例**持有它，而 `GET_LOCK`
        是原子的 —— 真有第二个实例在抢，只会有一个成功，输的那个继续按"没有所有权"
        拒绝收活。锁被别人持有时（真的出现了双实例）绝不抢，只记错误并拒绝收活。
        """
        state=self._lock_state()
        if state=='unknown':
            try:self._lock_connect()
            except Exception:
                log.warning('Invitation executor lock connection cannot be replaced',exc_info=True);return False
            state=self._lock_state()
        if state=='owned':return True
        if state=='foreign':
            log.error('Invitation executor lock is held by another session; refusing new work')
            return False
        if not getattr(self,'_started',False) or getattr(self,'_stopping',False):return False
        try:
            if not self._lock_acquire():return False
        except Exception:
            log.warning('Invitation executor lock re-acquire failed',exc_info=True);return False
        log.warning('Invitation executor lock had been released by the database (idle session); re-acquired on connection %s',self._process_lock_owner)
        return True

    def owns_executor_lock(self):
        """本进程是否**仍然**持有执行器排他锁（锁被回收时会就地补拿）。

        数据库连接断开会静默释放 MySQL 命名锁，而新实例随后就能拿到它。拿不出
        证据时一律当作"没有所有权"：此时既不回收别人的占位，也不再接收新任务。
        但"锁空闲"恰恰证明**没有**别的实例在跑，所以这里补拿而不是永久放弃 ——
        旧实现在这种状态下会一直 503，只能靠重启进程恢复。
        """
        with self._lock_mutex():return self._lock_maintain()

    def holds_executor_lock(self):
        """只读的所有权判断，供公开页面与管理端状态展示使用（不做任何补拿）。"""
        with self._lock_mutex():return self._lock_state()=='owned'

    def _lock_keepalive_loop(self,stop):
        while not stop.wait(LOCK_KEEPALIVE_SECONDS):
            if not getattr(self,'_started',False) or getattr(self,'_stopping',False):continue
            try:
                with self._lock_mutex():self._lock_maintain()
            except Exception:log.warning('Invitation executor lock keepalive failed',exc_info=True)

    def _start_lock_keepalive(self):
        stop=getattr(self,'_lock_keepalive_stop',None)
        if stop is not None and not stop.is_set():return
        stop=threading.Event()
        thread=threading.Thread(target=self._lock_keepalive_loop,args=(stop,),daemon=True,name='invitation-lock-keepalive')
        self._lock_keepalive_stop=stop;self._lock_keepalive=thread;thread.start()

    def _stop_lock_keepalive(self):
        stop=getattr(self,'_lock_keepalive_stop',None);thread=getattr(self,'_lock_keepalive',None)
        self._lock_keepalive_stop=None;self._lock_keepalive=None
        if stop is not None:stop.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():thread.join(timeout=10)

    def require_executor_accepting(self):
        with self._executor_lock:
            if not self._executor_enabled or not self._started or self._stopping:
                # 用户端只看到统一的「暂时无法开始」，真实原因必须留在日志里，
                # 否则下次还得靠 IS_USED_LOCK 反推。
                log.warning('Refused a start request: executor enabled=%s started=%s stopping=%s',
                            self._executor_enabled,self._started,self._stopping)
                raise Error('INVITATION_EXECUTOR_UNAVAILABLE','暂时无法开始，请稍后再试。',503)
            # 失去排他锁说明可能已有别的实例接管：本进程必须立刻停止收活，
            # 否则两个实例会同时处理同一批邮箱。锁空闲时会就地补拿，不在这里失败。
            if not self.owns_executor_lock():
                log.warning('Refused a start request: %s is not held by this process',EXECUTOR_LOCK_NAME)
                raise Error('INVITATION_EXECUTOR_UNAVAILABLE','暂时无法开始，请稍后再试。',503)
            # 工人死掉不该永久降低容量：先补建，再决定收不收活。补不回来就拒绝，
            # 绝不让任务进一个没有工人的队列 —— 旧实现只看开关，不看存活，
            # 于是工人全退出后页面照样"接受"开始，任务却永远排在那里。
            if self.cdk.CONTROLLER.ensure_workers() <= 0:
                log.warning('Refused a start request: no cdk worker could be started')
                raise Error('INVITATION_EXECUTOR_UNAVAILABLE','暂时无法开始，请稍后再试。',503)

    def executor_status(self):
        with self._executor_lock:
            enabled=self._executor_enabled
            ownership=self.holds_executor_lock()
            running=bool(self._started and not self._stopping and self.cdk.CONTROLLER.worker_alive())
            available=bool(self.proxy.has_available()) if enabled and running else False
            ready=bool(enabled and running and available and ownership)
            state='disabled' if not enabled else 'ready' if ready else 'unavailable'
            message=('已暂停新的执行请求，已有任务继续处理。' if not enabled else
                     '已开启，但执行器未持有排他锁，可能已有另一个实例接管；请重启本实例。' if not ownership else
                     '已开启，但执行服务尚未就绪，请检查服务运行状态。' if not running else
                     '已开启，但代理池暂无可用代理，请添加有效代理。' if not available else
                     '已开启，可在前台开始执行。')
            return {'enabled':enabled,'ready':ready,'state':state,'message':message}

    def set_executor_enabled(self,enabled,actor):
        if not isinstance(enabled,bool):raise bad('启用设置必须为布尔值。')
        with self._executor_lock:
            if enabled:
                try:
                    if self._started:self._execution_self_check()
                    self.start(background=False)
                except Exception:
                    # 同上：真实原因只进日志，不进给用户的响应。
                    logging.getLogger(__name__).warning('Invitation executor start refused',exc_info=True)
                    raise Error('INVITATION_EXECUTOR_START_FAILED','暂时无法开启，请稍后重试。',503) from None
            try:
                with self.db.transaction():
                    self.repo.set_setting('invitation_executor_enabled','true' if enabled else 'false')
                    self.s.audit.record(actor.id,'invitation.executor.update','invitation_executor',None,{'enabled':enabled})
            except Exception:
                raise Error('INVITATION_EXECUTOR_SAVE_FAILED','设置保存失败，请稍后重试。',503) from None
            self._executor_enabled=enabled
            return self.executor_status()

    def start(self,*,background=True):
        if self._started:
            if self._stopping:raise RuntimeError('Invitation executor is still stopping')
            return
        # Hold a dedicated connection for the entire singleton-worker lifetime.
        connection=self.db.engine.connect()
        acquired=connection.exec_driver_sql(f"SELECT GET_LOCK('{EXECUTOR_LOCK_NAME}',0)").scalar()
        if acquired!=1:
            connection.close();raise RuntimeError('An invitation executor is already active; refusing a second owner')
        with self._lock_mutex():
            self._process_lock=connection
            self._process_lock_owner=connection.exec_driver_sql('SELECT CONNECTION_ID()').scalar()
        self._background_workers=()
        try:
            self._execution_self_check()
            self.gate.open();self._stopping=False
            self.db.execute("INSERT IGNORE INTO invitation_v2_task_holds(job_id,original_status,reason) SELECT job_id,status,'RESTART_UNCERTAIN' FROM invitation_v2_cdk_jobs WHERE status IN ('queued','running')")
            # 上次进程被杀掉时留下的 run 会永远停在「运行中」，运行记录里看着像
            # 卡住了。recover_stale_runs 按 owner_pid 判断，跨进程安全；清理失败
            # 不该挡住启动，只记一条日志。
            try:
                fixed=self.repo.recover_runs_after_restart()
                if fixed:import logging;logging.getLogger(__name__).warning('Recovered %s interrupted invitation runs',fixed)
            except Exception:
                import logging;logging.getLogger(__name__).exception('Stale invitation run recovery failed')
            # 工位占位同理：进程被杀时占位不会自己释放（Permits 无自动回收），
            # 泄漏满一族上限后新任务会在门卫无限等待。此刻已持有排他锁，
            # 不存在第二个执行器，清空是安全的。
            try:
                permits=getattr(self.gate,'permits',None)
                leaked=permits.release_all() if permits is not None else 0
                if leaked:import logging;logging.getLogger(__name__).warning('Released %s execution permits leaked by a previous process',leaked)
            except Exception:
                import logging;logging.getLogger(__name__).exception('Execution permit recovery failed')
            self._started=True
            self.cdk.CONTROLLER.start_workers()
            # 上一次进程可能死在"写完库、还没入内存队列"的窗口里，留下"库里排队、
            # 内存没有"的作业。这里只报告，绝不自动重投 —— 由操作员核对后手动补投。
            try:
                from .dispatch import dispatch_gaps
                gaps=dispatch_gaps(self)
                if gaps:
                    import logging
                    logging.getLogger(__name__).warning(
                        'Found %s queued job(s) with no in-memory dispatch; operator redispatch required: %s',
                        len(gaps),[gap['jobId'] for gap in gaps])
            except Exception:
                import logging;logging.getLogger(__name__).exception('Queued-dispatch reconciliation failed')
            monitor=importlib.import_module('webui.sms_prices').MONITOR
            self._background_workers=(monitor,)
            if self._background_enabled('sms_monitor_enabled'):monitor.start()
            # 账号工厂始终随执行进程启动：它只按 pool_auto_enabled 自己决定做不做，
            # 因此"只开补号、关着邀请执行"的部署也能自动补池。
            self._background_workers=(monitor,self.pool)
            self.pool.start()
            # 锁连接保活：见 EXECUTOR_LOCK_NAME 处的说明。放在最后，任何一步
            # 失败都不会留下一个只保活、却不干活的线程。
            self._start_lock_keepalive()
            if background:
                refund=importlib.import_module('webui.sms_refund').WORKER
                self._background_workers=(monitor,self.pool,refund)
                if self._background_enabled('sms_refund_enabled'):refund.start()
        except BaseException:
            if self._started:self.stop()
            elif self._process_lock is not None:
                with self._lock_mutex():
                    connection.exec_driver_sql(f"SELECT RELEASE_LOCK('{EXECUTOR_LOCK_NAME}')");connection.close();self._process_lock=None;self._process_lock_owner=None
            raise

    def _execution_self_check(self):
        from .process import execution_self_check
        execution_self_check(self.data_dir,self.repo.get_setting('codex_binary','codex') or 'codex')

    def stop(self):
        if not self._started:return True
        self._stopping=True;self.gate.close()
        background_workers=self._background_workers
        for worker in background_workers:worker.stop()
        controller=self.cdk.CONTROLLER
        # Pending work remains queued in MySQL for operator review after restart.
        # It must not be drained into external actions during shutdown.
        with controller._lock:
            with controller._q.mutex:controller._q.queue.clear()
            for _ in controller._workers:controller._q.put(None)
        deadline=time.monotonic()+10
        threads=[*controller._workers,*(worker._thread for worker in background_workers)]
        for worker in threads:
            if worker is not None:worker.join(timeout=max(0,deadline-time.monotonic()))
        # Do not release ownership while a worker still performs external actions.
        if any(worker is not None and worker.is_alive() for worker in threads) or self.gate.inflight:return False
        # 先停保活线程，再释放锁：不能让保活和释放并发用同一条连接。
        self._stop_lock_keepalive()
        with self._lock_mutex():
            if self._process_lock is not None:
                try:self._process_lock.exec_driver_sql(f"SELECT RELEASE_LOCK('{EXECUTOR_LOCK_NAME}')")
                except Exception:log.warning('Invitation executor lock release failed',exc_info=True)
                self._process_lock.close();self._process_lock=None;self._process_lock_owner=None
        self._started=False;self._stopping=False
        self._background_workers=()
        controller._started=False;controller._workers.clear()
        return True

    def register_issued_key(self,item_id,sku_id,attrs,plaintext):
        canonical=str(plaintext).strip().upper();marker=mysql.protected(canonical)
        if self.db.one('SELECT code FROM invitation_v2_cdk_codes WHERE code=?',marker):raise Error('INVENTORY_GENERATE_DENIED','邀请卡密存在大小写冲突。',409)
        self.repo.create_cdk_codes([canonical],batch=self._inventory_batch_no(item_id),note='')
        days=int(attrs.get('cdkValidDays',15))
        if not 1<=days<=3650:raise bad('邀请卡密有效天数不合法。')
        expires=datetime.now(timezone.utc)+timedelta(days=days)
        self.db.execute('INSERT INTO invitation_v2_platform_keys(inventory_item_id,sku_id,code_id,code_fingerprint,expires_at,snapshot_json) VALUES (?,?,?,?,?,?)',item_id,sku_id,marker,self.s.codec.fingerprint(plaintext),expires,json.dumps(attrs,ensure_ascii=False))

    def _inventory_batch_no(self,item_id):
        """这张卡所属库存批次的**派生批次号**（`BATCH-YYYYMMDD-XXXXXXXX`）。

        以前这里写的是库存批次的 UUID，于是「卡密批次」页显示 `BATCH-…`、
        「卡密管理」页显示 `c5388f7f-e836-49d8-…`，同一批卡在两张页面上是两个
        不同字符串，运营无法对照。派生算法只有 `common/cdk.py::batch_no` 一处，
        历史数据不动（没有批次归属的老卡就保持空，不猜）。
        """
        row=self.db.one('SELECT b.id AS id,b.created_at AS createdAt FROM inventory_items i '
                        'JOIN inventory_import_batches b ON b.id=i.import_batch_id WHERE i.id=?',item_id)
        if not row:return ''
        return batch_no(row['id'],row.get('createdAt'),self.s.config.get('runtime.business_time_zone',BATCH_ZONE))

    def unassigned_origin_batches(self,sku_id):
        """未登记批次（没有 `import_batch_id`）里，卡密在**邀请卡密表**里的原始批次名。

        「卡密批次」页要给历史导入/手工创建的卡密补一个「未登记批次」行，批次号不能伪造，
        唯一还留着出处的就是这里的 `invitation_v2_cdk_codes.batch`（如 `20261007-014828`
        或 `manual`）。空批次名（本站生成但没写进批次表的老数据）单独成组，由页面决定怎么显示。
        """
        rows=self.db.all("SELECT COALESCE(c.batch,'') AS batch,COUNT(*) AS total "
                         'FROM inventory_items i '
                         'JOIN invitation_v2_platform_keys k ON k.inventory_item_id=i.id '
                         'JOIN invitation_v2_cdk_codes c ON c.code=k.code_id '
                         'WHERE i.sku_id=? AND i.import_batch_id IS NULL '
                         "GROUP BY COALESCE(c.batch,'') ORDER BY total DESC,batch",str(sku_id))
        return [{'batch':str(row.get('batch') or ''),'total':int(row.get('total') or 0)} for row in rows]

    def _require_inventory_available(self,marker,for_redeem=False):
        row=self.db.one('SELECT p.*,i.status AS inventory_status FROM invitation_v2_platform_keys p JOIN inventory_items i ON i.id=p.inventory_item_id WHERE p.code_id=? FOR UPDATE',marker)
        if row and row['inventory_status']!='ISSUED':raise Error('INVITATION_CDK_DISABLED','邀请卡密已停用。',409)
        if row and row['expires_at'] and row['expires_at'].replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc) and for_redeem:raise Error('INVITATION_CDK_EXPIRED','邀请卡密已过期。',410)
        return row

    def _require_no_holds(self,marker):
        row=self.db.one('SELECT h.job_id FROM invitation_v2_task_holds h JOIN invitation_v2_cdk_jobs j ON j.job_id=h.job_id WHERE j.code=? AND h.resolved_at IS NULL LIMIT 1 FOR UPDATE',marker)
        if row:raise Error('INVITATION_REVIEW_REQUIRED','有执行中或结果不明的历史任务，请先完成安全复核。',409)

    def _lock_codes(self,markers):
        # Inventory invalidation already locks item -> platform key. Keep the
        # same order for redemption/start/native mutation to avoid a lock cycle.
        markers=sorted(set(markers));placeholders=','.join('?' for _ in markers)
        if not markers:return
        rows=self.db.all('SELECT inventory_item_id FROM invitation_v2_platform_keys WHERE code_id IN ('+placeholders+') ORDER BY inventory_item_id',*markers)
        for row in rows:self.db.one('SELECT id FROM inventory_items WHERE id=? FOR UPDATE',row['inventory_item_id'])
        for row in rows:self.db.one('SELECT code_id FROM invitation_v2_platform_keys WHERE inventory_item_id=? FOR UPDATE',row['inventory_item_id'])
        for marker in markers:self.db.one('SELECT code FROM invitation_v2_cdk_codes WHERE code=? FOR UPDATE',marker)

    def disable_key_for_inventory(self,item_id):
        row=self.db.one('SELECT code_id FROM invitation_v2_platform_keys WHERE inventory_item_id=? FOR UPDATE',item_id)
        if not row:return False
        code=mysql.revealed(row['code_id'])
        with self.db.transaction():
            self._lock_codes([row['code_id']])
            current=self.repo.get_cdk_code(code)
            if current and current['status'] in ('queued','running'):raise Error('INVITATION_RUNNING','邀请已入队或正在执行，不能作废。',409)
            if current:self._require_mailbox_idle(current.get('email'))
            self.repo.void_cdk(code)
        return True

    def generate_native(self,body,actor):
        import secrets,string
        from ..common.mapper import Mapper
        from ..common.catalog import parse_json,json_text
        count=body.get('count',10);length=body.get('length',20);prefix=str(body.get('prefix') or 'CDK-')[:32]
        # 熵下限与 common/inventory.py 的 generate_cdk 对齐（原来允许 8 位：36^8≈41bit，
        # 配合公开兑换接口的枚举额度就是可被实际爆破的目标）。
        if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=5000 or isinstance(length,bool) or not isinstance(length,int) or not 12<=length<=64:raise bad('生成参数超出允许范围（长度需 12–64）。')
        with self.db.transaction():
            mapper=Mapper(self.db,'InventoryMapper')
            candidates=self.db.all("SELECT id FROM skus WHERE status='ACTIVE' AND JSON_UNQUOTE(JSON_EXTRACT(attributes_json,'$.productKind'))='CODEX_INVITE_REDEEM'")
            sku_id=body.get('skuId') or (candidates[0]['id'] if len(candidates)==1 else None)
            if not sku_id:raise bad('存在多个邀请套餐，请传入 skuId 选择套餐。')
            plan=mapper.lockPlan(sku_id)
            if not plan:raise Error('SKU_NOT_FOUND','套餐不存在。',404)
            attrs=parse_json(plan['attributesJson'])
            if plan['status']!='ACTIVE' or attrs.get('productKind')!='CODEX_INVITE_REDEEM':raise bad('请选择有效的邀请套餐。')
            batch=str(uuid4());label=str(body.get('batch') or time.strftime('%Y%m%d-%H%M%S'));note=str(body.get('note') or '')
            # 这两列分别是 VARCHAR(160)/TEXT：前端没有 maxLength，只信前端会直接 Data too long → 500。
            if len(label)>160:raise bad('批次名不能超过 160 个字符。')
            if len(note)>2000:raise bad('备注不能超过 2000 个字符。')
            mapper.insertBatch(batch,sku_id,count,json_text({'count':count,'length':length,'prefix':prefix}),json_text(attrs),actor.id)
            codes=[];seen=set()
            for _ in range(count):
                while True:
                    code=prefix+''.join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(length))
                    canonical=code.strip().upper()
                    if canonical not in seen and not self.repo.get_cdk_code(canonical):break
                seen.add(canonical);item=str(uuid4());fingerprint=self.s.codec.fingerprint(code)
                mapper.reserveCdk('CODEX_INVITE_REDEEM',fingerprint,item)
                reservation=mapper.lockCdkReservation('CODEX_INVITE_REDEEM',fingerprint)
                if not reservation or reservation['ownerInventoryId']!=item:raise Error('INVENTORY_CONFLICT','卡密冲突，本次未生成。',409)
                mapper.insertItem(item,sku_id,self.s.codec.encrypt(code),fingerprint,batch)
                self.register_issued_key(item,sku_id,attrs,code)
                self.repo.update_cdk_fields(canonical,batch=label,note=note)
                self.s.inventory.event(item,'ISSUED',actor.id);codes.append(canonical)
            mapper.finishBatch(batch,count,0,0)
            self.s.audit.record(actor.id,'invitation.cdk.generate','inventory_batch',batch,{'count':count})
            return {'ok':True,'batch':label,'batchId':batch,'inserted':count,'skipped':0,'codes':codes}

    def find_inventory_ids_for_fingerprint(self,fingerprint):
        return [row['inventory_item_id'] for row in self.db.all('SELECT inventory_item_id FROM invitation_v2_platform_keys WHERE code_fingerprint=?',fingerprint)]

    def dashboard_summary(self):
        return self.cdk.CONTROLLER.stats()

    def page_content(self):
        st=self.cdk.CONTROLLER.stats()
        settings=self.repo.get_mail_settings()
        mailbox_ready,_=self._mailbox_readiness(settings)
        # 「开始」按钮只按这里的结果放行，所以执行器的**全部**前置条件都必须算进来：
        # 少了锁所有权这一项，前台会显示可以开始、接口却一律 503（2026-10-12 线上
        # 就是这个表现）。这里用只读判断，真正的补拿交给保活线程。
        executor_ready=bool(self._executor_enabled and self._started and not self._stopping and st.get('worker_alive',0) and self.proxy.has_available() and self.holds_executor_lock())
        # 可兑换账号数 = 已注册 GPT、**尚未 Codex 授权**、且没被卡密占用的号。
        # 号池可以混放多种来源（补号来源可与兑换来源不同），所以这里**不看**
        # 当前配置的来源是否池化 —— 那会让 CF 现造地址入池的号数不出来。
        # 负数（旧的"不限量"语义）一律按 0：用户端绝不能出现"不限量"。
        try:pool_available=max(0,int(self.alloc.pool_available(settings) or 0))
        except (TypeError,ValueError):pool_available=0
        return {'title':'Codex 邀请','description':'兑换后将邮箱交给邀请方，确认已发送邀请信，再点击开始。','batchLimit':INVITATION_BATCH_LIMIT,
                'poolAvailable':pool_available,'canRedeem':bool(mailbox_ready),'canStart':executor_ready,
                'unavailableMessage':'' if mailbox_ready and executor_ready else '服务暂时不可用，请稍后重试。'}

    def _codes(self,codes):
        if not isinstance(codes,list) or not 1<=len(codes)<=INVITATION_BATCH_LIMIT or any(not isinstance(c,str) or len(c)>160 or c.strip().lower().startswith('iv2$') for c in codes):raise bad(f'一次请输入 1–{INVITATION_BATCH_LIMIT} 条卡密。')
        return list(dict.fromkeys(str(c).strip().upper() for c in codes if str(c).strip()))

    def redeem(self,codes,attempt_id=None,recovery_secret=None):
        return self.redeem_attempt(codes,attempt_id,recovery_secret)

    def _session(self,entry):
        if not isinstance(entry,dict):raise bad('会话参数不合法。')
        try:sid=str(UUID(str(entry.get('id'))))
        except (ValueError,TypeError,AttributeError):raise bad('会话标识不合法。')
        self.s.tokens.require('invocation-session',sid,entry.get('sessionSecret'))
        row=self.db.one('SELECT code_id FROM invitation_v2_sessions WHERE id=?',sid)
        if not row:
            legacy=self.db.one('SELECT is_current FROM invitation_runs WHERE id=?',sid)
            if legacy and not legacy['is_current']:raise Error('SESSION_SUPERSEDED','该会话已经被新的兑换会话替代。',409)
            raise Error('INVITATION_SESSION_NOT_FOUND','邀请会话不存在。',404)
        return sid,mysql.revealed(row['code_id'])

    def _view(self,sid,code):
        info=self.cdk.CONTROLLER.status_for_codes([code])[0];row=self.repo.get_cdk_code(code) or {}
        status=info['status'];actions=['POLL']
        if row.get('status')=='review':status='review'
        held=bool(self.db.scalar('SELECT COUNT(*) FROM invitation_v2_task_holds h JOIN invitation_v2_cdk_jobs j ON j.job_id=h.job_id WHERE j.code=? AND h.resolved_at IS NULL',mysql.marker(code)))
        if held:status='review'
        if status in {'bound','failed'}:actions.append('START' if row.get('invite_sent_at') else 'CONFIRM_INVITE')
        if status in {'done','void','unknown'}:actions=[]
        try:progress=max(0,min(99,int(info.get('progress') or 0)))
        except (ValueError,TypeError,OverflowError):progress=0
        return {'id':sid,'email':info.get('email',''),'progressPercent':100 if status=='done' else progress,
                'displayResult':'SUCCESS' if status=='done' else 'FAILED' if status=='failed' else None,
                'status':'processing' if status in {'queued','running'} else status,'allowedActions':actions}

    def action(self,entries,action):
        if not isinstance(entries,list) or not 1<=len(entries)<=INVITATION_BATCH_LIMIT:raise bad(f'一次最多处理 {INVITATION_BATCH_LIMIT} 个会话。')
        items=[]
        for entry in entries:
            try:
                sid,code=self._session(entry)
                if action=='invite-sent':
                    with self.db.transaction():
                        marker=mysql.marker(code);self._lock_codes([marker]);self._require_inventory_available(marker);self._require_no_holds(marker)
                        row=self.repo.get_cdk_code(code)
                        if not row or row['status'] not in {'bound','failed'}:raise Error('INVITATION_INVALID_STATE','当前状态不能确认邀请。',409)
                        self.repo.mark_invite_sent(code)
                elif action=='start':
                    result=self.cdk.CONTROLLER.enqueue(code)
                    if not result.get('ok'):
                        raise Error('INVITATION_BUSY' if result.get('reason')=='busy' else 'INVITATION_START_DENIED','当前繁忙，请稍后重试。' if result.get('reason')=='busy' else '当前状态不能开始，请刷新记录后重试。',409)
                items.append(self._view(sid,code))
            except Error as exc:
                public=public_invitation_error(exc)
                items.append({'id':entry.get('id') if isinstance(entry,dict) and isinstance(entry.get('id'),str) else None,
                              'error':{'code':public.code,'message':public.message}})
        page=self.page_content()
        return {'items':items,**{key:page[key] for key in ('canRedeem','canStart','poolAvailable','unavailableMessage')}}

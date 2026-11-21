from __future__ import annotations
import asyncio
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
from uuid import UUID,uuid4
from zoneinfo import ZoneInfo
from croniter import croniter
from fastapi import APIRouter,Request,Body,Query
from .mapper import Mapper
from .catalog import json_text,parse_json
from ..errors import Error,ok,bad,page_meta
from ..security import ctx,require_permission

router=APIRouter()
# 管理员主动取消的持久任务在库里是 FAILED（CHECK 约束没有 CANCELLED），用这个
# 错误码还原成对外的 CANCELLED。转换只有这一处，写入、列表、单条都用它。
CANCELLED_CODE='ADMIN_CANCELLED'
def public_job_status(row):
    out=dict(row)
    if out.get('status')=='FAILED' and out.get('lastErrorCode')==CANCELLED_CODE:out['status']='CANCELLED'
    return out
@dataclass(frozen=True)
class Job:
    id:str
    kind:str
    payload:str
    attempts:int
    lease_token:str
    request_id:str|None
    @property
    def leaseToken(self):return self.lease_token
    @property
    def requestId(self):return self.request_id

class DurableJobs:
    def __init__(self,services):self.s=services;self.db=services.db;self.mapper=Mapper(self.db,'DurableJobMapper');self.handlers={}
    def enqueue(self,kind,key,payload,available_at=None,request_id=None):
        if not self.db.in_transaction:raise RuntimeError('Job enqueue requires a business transaction')
        if not isinstance(payload,str):payload=json_text(payload)
        self.mapper.enqueue(str(uuid4()),kind,key,payload,available_at or datetime.now(timezone.utc),request_id)
    def claim(self,batch_size=10,lease=timedelta(seconds=60)):
        seconds=int(lease.total_seconds()) if hasattr(lease,'total_seconds') else int(lease)
        if not 1<=batch_size<=100 or seconds<1:raise ValueError('Invalid lease request')
        with self.db.transaction():
            self.mapper.recoverExpired();ids=self.mapper.selectClaimIds(batch_size)
            if not ids:return []
            token=str(uuid4());self.mapper.markClaimed(ids,token,seconds)
            return [Job(row['id'],row['kind'],row['payload'],int(row['attempts']),row['leaseToken'],row['requestId']) for row in self.mapper.findClaimed(ids)]
    def complete(self,job):
        with self.db.transaction():return self.mapper.complete(job.id,job.lease_token)==1
    def fail(self,job,code):
        if not re.fullmatch('[A-Z0-9_]{1,100}',code):raise ValueError('Use a safe error code')
        with self.db.transaction():return self.mapper.fail(job.id,job.lease_token,code)==1
    def register(self,kind,handler):self.handlers[kind]=handler
    def tick(self):
        # Preserve the original Java worker's one-at-a-time, five-minute lease.
        # A shorter lease could expose a long supplier operation to another owner.
        for job in self.claim(1,timedelta(minutes=5)):
            try:
                handler=self.handlers.get(job.kind)
                if handler is None:raise Error('JOB_KIND_UNSUPPORTED','任务类型未注册。',503)
                handler(parse_json(job.payload));self.complete(job)
            except Exception as e:self.fail(job,e.code if isinstance(e,Error) and re.fullmatch('[A-Z0-9_]{1,100}',e.code) else 'JOB_EXECUTION_FAILED')
    def list(self,page,size,status):
        if status and status not in ('PENDING','RUNNING','FAILED','SUCCEEDED','CANCELLED'):raise bad('任务状态无效。')
        filter_status,cancelled=('FAILED',True) if status=='CANCELLED' else (status,False)
        rows=[public_job_status(row) for row in self.mapper.page(filter_status or '',cancelled,size,(page-1)*size)]
        return ok(rows,page_meta(page,size,self.mapper.count(filter_status or '',cancelled)))
    def retry(self,actor,id):
        with self.db.transaction():
            if self.mapper.retryFailed(id)!=1:raise Error('JOB_NOT_RETRYABLE','只有已失败的可管理任务可以重新执行。',409)
            self.s.audit.record(actor.user_id,'job.retry','job',id,{'queued':True});return {'queued':True}
    def cancel(self,actor,id):
        """只取消还没跑完的任务；终态任务原地返回，绝不自动重放。

        `durable_jobs_status_check` 只允许 PENDING/RUNNING/SUCCEEDED/FAILED（见
        migrations/legacy/V1__mysql_schema.sql），因此终态落库为 FAILED +
        last_error_code='ADMIN_CANCELLED'，对外统一由 `public_job_status` 还原成
        CANCELLED（与列表、筛选同一处转换）。
        """
        with self.db.transaction():
            row=self.mapper.findForUpdate(id)
            if not row:raise Error('JOB_NOT_FOUND','任务不存在。',404)
            if row['status'] not in ('PENDING','RUNNING'):
                return {'id':id,'status':public_job_status(row)['status']}
            self.mapper.cancelActive(id,CANCELLED_CODE)
            self.s.audit.record(actor.user_id,'job.cancel','job',id,{'previousStatus':row['status']})
        return {'id':id,'status':'CANCELLED'}

class Scheduler:
    # 只列出 app.py 真正 register 的任务；历史遗留的 invitation.poll 已不再注册，
    # 留在白名单里只会让后台能写入一条永远不生效的 scheduler.controls 控制行。
    KNOWN={'worker.process','worker.heartbeat','worker.recovery','worker.sms_sweep','notification.poll'}
    INTERVAL_KEYS={'worker.process':'runtime.worker_poll_ms','worker.heartbeat':'runtime.worker_heartbeat_ms','worker.recovery':'runtime.worker_recovery_ms','worker.sms_sweep':'runtime.worker_sms_sweep_ms','notification.poll':'runtime.worker_notification_poll_ms'}
    def __init__(self,services):self.s=services;self.tasks={};self.controls={};self.worker_id=str(uuid4());self.mapper=Mapper(services.db,'SettingsMapper');self.heartbeats=Mapper(services.db,'JobsWorkerMapper');self._stop=threading.Event();self.thread=None
    def register(self,id,handler,interval_ms=1000,name=None,group='worker',description=''):
        if id not in self.KNOWN:raise ValueError('Unknown scheduler task')
        self.tasks[id]={'id':id,'handler':handler,'intervalMs':interval_ms,'name':name or id,'group':group,'description':description,'lastRunAt':None,'nextDueAt':None,'runCount':0,'lastError':None,'lock':threading.Lock()}
    def _controls(self):
        raw=self.mapper.findValueJson('scheduler.controls')
        value=parse_json(raw)
        if not isinstance(value,dict):value={}
        out={}
        for id in self.KNOWN:
            item=value.get(id,{})
            if not isinstance(item,dict):item={}
            out[id]={'enabled':item.get('enabled') if isinstance(item.get('enabled'),bool) else True,'cron':str(item.get('cron') or '').strip()}
        return out
    def interval(self,id):
        task=self.tasks[id];key=self.INTERVAL_KEYS.get(id)
        minimum=1000 if id in ('worker.heartbeat','worker.recovery','worker.sms_sweep') else 100
        task['intervalMs']=max(minimum,self.s.config.int(key,task['intervalMs'])) if key else task['intervalMs']
        return task['intervalMs']
    def next_due(self,id,now,interval):
        cron=self.controls.get(id,{}).get('cron','')
        if not cron:return now+timedelta(milliseconds=max(100,interval))
        zone=ZoneInfo(self.s.config.get('runtime.business_time_zone','Asia/Shanghai'))
        try:return croniter(cron,now.astimezone(zone),second_at_beginning=True).get_next(datetime).astimezone(timezone.utc)
        except (ValueError,KeyError):return now+timedelta(milliseconds=max(100,interval))
    def run(self,id,manual=False):
        if id not in self.tasks:raise Error('SCHEDULER_NOT_FOUND','定时任务不存在或不支持立即执行。',404)
        task=self.tasks[id]
        if not task['lock'].acquire(blocking=False):raise Error('CONFLICT','定时任务正在执行。',409)
        try:
            interval=self.interval(id)
            if not manual:task['nextDueAt']=self.next_due(id,datetime.now(timezone.utc),interval)
            error=None
            try:task['handler']()
            except Exception as e:error=e.code if isinstance(e,Error) else 'SCHEDULER_EXECUTION_FAILED'
            task['lastRunAt']=datetime.now(timezone.utc);task['runCount']+=1;task['lastError']=error
            if manual:task['nextDueAt']=self.next_due(id,task['lastRunAt'],interval)
            return dict(id=id,ran=True,error=error) if error else dict(id=id,ran=True)
        finally:task['lock'].release()
    def update(self,id,body):
        if id not in self.KNOWN:raise Error('SCHEDULER_NOT_FOUND','定时任务不存在。',404)
        if body.get('enabled') is not None and not isinstance(body['enabled'],bool):raise bad('启用设置必须为布尔值。')
        cron=body.get('cron')
        if cron is not None:
            if not isinstance(cron,str) or len(cron)>120 or cron.strip() and (len(cron.split())!=6 or not croniter.is_valid(cron,second_at_beginning=True)):raise bad('cron 表达式不合法（需 6 段：秒 分 时 日 月 周）。')
        with self.s.db.advisory_lock('scheduler.controls',10) as acquired:
            if not acquired:raise Error('CONFLICT','定时任务配置正在更新。',409)
            controls=self._controls();current=controls[id]
            controls[id]={'enabled':body.get('enabled') if body.get('enabled') is not None else current['enabled'],'cron':cron.strip() if cron is not None else current['cron']}
            self.mapper.upsert(str(uuid4()),'scheduler.controls','scheduler',json_text(controls),'PRIVATE',None)
        self.controls=controls;return {'id':id,**controls[id]}
    def status(self):
        self.controls=self._controls();now=datetime.now(timezone.utc);tasks=[]
        for id,task in self.tasks.items():
            self.interval(id)
            out={k:v for k,v in task.items() if k not in ('handler','lock')};control=self.controls[id];out['enabled']=bool(control.get('enabled',True));out['cron']=control.get('cron','')
            out['status']='DISABLED' if not out['enabled'] else 'ERROR' if out['lastError'] else 'IDLE' if out['lastRunAt'] is None else 'STALE' if (now-out['lastRunAt']).total_seconds()*1000>max(out['intervalMs']*3,60000) else 'RUNNING';tasks.append(out)
        workers=[]
        for row in self.heartbeats.listHeartbeats():
            last=row['lastSeenAt'];last=last.replace(tzinfo=timezone.utc) if last and last.tzinfo is None else last
            workers.append({'workerId':row['workerId'],'lastSeenAt':last,'alive':last is not None and last>now-timedelta(seconds=45)})
        return {'tasks':tasks,'workers':workers}
    def heartbeat(self):self.heartbeats.upsertHeartbeat(self.worker_id)
    def _loop(self):
        while not self._stop.wait(.2):
            try:
                self.controls=self._controls();now=datetime.now(timezone.utc)
                for id,task in list(self.tasks.items()):
                    interval=self.interval(id)
                    if task['nextDueAt'] is None or now>=task['nextDueAt']:
                        if self.controls.get(id,{}).get('enabled',True):self.run(id)
                        else:task['nextDueAt']=now+timedelta(milliseconds=interval)
            except Exception:
                # Fail-closed on unavailable controls/database; no untracked run.
                import logging
                logging.getLogger(__name__).error('Scheduler tick failed')
    def start(self):
        if self.thread and self.thread.is_alive():return
        self._stop.clear();self.thread=threading.Thread(target=self._loop,name='smsinvit-scheduler',daemon=True);self.thread.start()
    def stop(self):
        self._stop.set()
        if self.thread:self.thread.join(timeout=10)

@router.get('/api/v1/admin/jobs')
def list_jobs(request:Request,page:int=Query(1,ge=1),pageSize:int=Query(20,ge=1,le=100),status:str|None=Query(None,max_length=32)):require_permission(request,'job.manage');return ctx(request).jobs.list(page,pageSize,status)
@router.post('/api/v1/admin/jobs/{id}/retry')
def retry_job(request:Request,id:UUID):return ok(ctx(request).jobs.retry(require_permission(request,'job.manage'),str(id)))
@router.post('/api/v1/admin/jobs/{id}/cancel')
def cancel_job(request:Request,id:UUID):return ok(ctx(request).jobs.cancel(require_permission(request,'job.manage'),str(id)))
@router.get('/api/v1/admin/schedulers')
def schedulers(request:Request):require_permission(request,'settings.read');return ok(ctx(request).scheduler.status())
@router.patch('/api/v1/admin/schedulers/{id}')
def update_scheduler(request:Request,id:str,body:dict=Body(...)):
    p=require_permission(request,'settings.update');out=ctx(request).scheduler.update(id,body)
    ctx(request).audit.record(getattr(p,'user_id',None) or p.id,'scheduler.update','scheduler',id,{'fields':sorted(body)})
    return ok(out)
@router.post('/api/v1/admin/schedulers/{id}/run')
def run_scheduler(request:Request,id:str):
    p=require_permission(request,'settings.update');out=ctx(request).scheduler.run(id,True)
    ctx(request).audit.record(getattr(p,'user_id',None) or p.id,'scheduler.run','scheduler',id,{'manual':True})
    return ok(out)

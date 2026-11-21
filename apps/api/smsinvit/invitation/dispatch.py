"""Reconcile the durable queue with what the worker process actually holds.

A job is written to MySQL first and only handed to the in-process queue
afterwards, so a process that dies in between leaves a row that says "queued"
with nothing to run it. Repeatedly pressing start cannot repair it either: the
controller answers "already queued" and never re-dispatches.

Nothing here runs automatically. A stranded job is *reported*, and an operator
re-dispatches it explicitly. Re-dispatch never rewrites a card or job status,
never creates a second job row, and refuses while the outcome is uncertain.
"""
from __future__ import annotations
import logging
import time
from fastapi import APIRouter,Body,Request
from ..errors import Error,bad,ok
from ..security import ctx,require_permission
from . import mysql

log=logging.getLogger(__name__)

# Committing the job and putting it on the queue are two steps. This grace keeps
# a job that was submitted seconds ago (and is about to be dispatched) out of the
# report, so an operator cannot double-dispatch it.
GRACE_SECONDS=60

def _memory_markers(controller):
    """内存里"还排着"或"正拿在手上"的作业，统一成库里的标记形式。"""
    markers=set()
    for job_id in [*controller.queued_job_ids(),*controller.in_flight()]:
        if job_id:markers.add(mysql.marker(str(job_id)))
    return markers

def dispatch_gaps(invitation):
    """库里排队、没有未解除复核、内存里也没有的作业。纯只读。"""
    controller=invitation.cdk.CONTROLLER
    memory=_memory_markers(controller)
    cutoff=time.time()-GRACE_SECONDS
    rows=invitation.db.all(
        'SELECT j.job_id AS jobId,j.email,j.created_at AS createdAt,j.stage,j.progress '
        'FROM invitation_v2_cdk_jobs j '
        'LEFT JOIN invitation_v2_task_holds h ON h.job_id=j.job_id AND h.resolved_at IS NULL '
        "WHERE j.status='queued' AND h.job_id IS NULL "
        'ORDER BY j.created_at,j.job_id')
    now=time.time()
    gaps=[]
    for row in rows:
        if row['jobId'] in memory:continue
        created=float(row.get('createdAt') or 0)
        if created>cutoff:continue
        gaps.append({'jobId':mysql.revealed(row['jobId']),'email':row.get('email') or '',
                     'createdAt':created,'ageSeconds':int(max(0,now-created)),
                     'stage':row.get('stage'),'progress':int(row.get('progress') or 0)})
    return gaps

def redispatch(invitation,job_id):
    """把一个"只写了库、没进内存队列"的作业重新递给工人。

    幂等：已经在内存队列里或正被工人处理时返回 `already`，不重复投递。
    """
    raw=str(job_id or '').strip()
    marker=mysql.marker(raw) if raw else ''
    if not marker:raise bad('请提供要重新调度的作业标识。')
    controller=invitation.cdk.CONTROLLER
    with invitation._executor_lock,controller._lock:
        if marker in _memory_markers(controller):return {'already':True,'jobId':mysql.revealed(marker)}
        row=invitation.db.one('SELECT job_id,code,status,created_at FROM invitation_v2_cdk_jobs WHERE job_id=?',marker)
        if not row:raise Error('NOT_FOUND','未找到该作业。',404)
        if str(row.get('status') or '')!='queued':
            raise Error('CONFLICT','该作业已经不在排队状态，请刷新后重试。',409)
        if invitation.db.one('SELECT job_id FROM invitation_v2_task_holds WHERE job_id=? AND resolved_at IS NULL',marker):
            raise Error('INVITATION_REVIEW_REQUIRED','该作业有结果不明的记录，请先完成安全复核。',409)
        if float(row.get('created_at') or 0)>time.time()-GRACE_SECONDS:
            raise Error('CONFLICT','该作业刚提交，请稍后再核对。',409)
        # 没有工人的队列不该再进任务。
        invitation.require_executor_accepting()
        controller.dispatch(raw,mysql.revealed(row['code']))
        log.warning('[cdk] 人工重新调度作业 %s（此前只写了库、没有进内存队列）',marker)
        return {'already':False,'jobId':mysql.revealed(marker)}

router=APIRouter(prefix='/api/v1/admin/invitation/queue')
@router.get('/gaps')
def list_gaps(request:Request):
    require_permission(request,'invitation.read')
    invitation=ctx(request).invitation
    gaps=dispatch_gaps(invitation)
    return ok({'items':gaps,'graceSeconds':GRACE_SECONDS,
               'workerAlive':invitation.cdk.CONTROLLER.worker_alive()})

@router.post('/redispatch')
def redispatch_job(request:Request,body:dict=Body(...)):
    actor=require_permission(request,'invitation.manage')
    if body.get('confirmNoExternalAction') is not True:
        raise bad('必须确认该作业从未真正启动过。')
    job_id=body.get('jobId')
    if not isinstance(job_id,str) or not 8<=len(job_id)<=160:raise bad('请提供有效的作业标识。')
    invitation=ctx(request).invitation
    result=redispatch(invitation,job_id)
    if not result['already']:
        invitation.s.audit.record(actor.id,'invitation.queue.redispatch','invitation_cdk_job',
                             str(job_id),{'confirmedNoExternalAction':True})
    return ok(result)

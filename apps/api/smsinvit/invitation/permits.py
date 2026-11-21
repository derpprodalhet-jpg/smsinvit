"""Persistent concurrency and mailbox ownership; uncertain slots fail closed.

Slots are split into two independent families so that "邀请最多同时 3 个" and
"补号最多同时 10 个" never eat each other's quota:

    INVITE_SLOTS = (1, 16)    邀请/授权作业（上限 cdk_max_concurrency）
    POOL_SLOTS   = (17, 32)   账号工厂补号注册（上限 pool_register_concurrency）

Slot 0 stays the database-wide allocation mutex. Migrations already insert
0..32, so the split needs no schema change.

`holds_router` is the only production path that may lift a task hold. A hold is
written whenever a restart finds a queued/running job whose outcome is unknown;
before V17 nothing ever cleared it, so the block was permanent. Lifting it
requires an operator who confirms that every executor instance has stopped,
records a conclusion and an explanation, and the release never rewrites the
stored outcome as success.
"""
from contextlib import contextmanager
import time
from uuid import UUID,uuid4
from sqlalchemy.exc import IntegrityError,OperationalError
from fastapi import APIRouter,Body,Request,Path
from ..errors import Error,bad,ok
from ..security import ctx,require_permission
from . import mysql

INVITE_SLOTS=(1,16)
POOL_SLOTS=(17,32)
# A conclusion is an operator's judgement, not a result. All three values close
# an interrupted task as failed and differ only in what gets recorded, so no
# choice can turn an unknown outcome into a success.
RESOLUTIONS=frozenset({'not_started_confirmed','interrupted_confirmed','external_evidence_confirmed'})
# Never phrased as a success: the user may retry once the block is lifted.
INTERRUPTED_ERROR='执行进程中断，已人工复核；本次未完成，可重新开始。'

@contextmanager
def _executor_locks(db):
    """Prove that no executor instance is running before a review action.

    Only a process holding both advisory locks can perform invitation work, so
    holding them exclusively is the evidence that no external action is still in
    flight. Released on the way out even when the body raises.
    """
    global_lock=invitation_lock=False
    try:
        global_lock=db.scalar("SELECT GET_LOCK('smsinvit:python:worker',0)")==1
        if global_lock:invitation_lock=db.scalar("SELECT GET_LOCK('smsinvit:invitation:worker',0)")==1
        if not global_lock or not invitation_lock:raise Error('INVITATION_EXECUTOR_ACTIVE','仍有执行实例持有数据库锁，请先停止所有执行实例。',409)
        yield
    finally:
        if invitation_lock:db.scalar("SELECT RELEASE_LOCK('smsinvit:invitation:worker')")
        if global_lock:db.scalar("SELECT RELEASE_LOCK('smsinvit:python:worker')")

def _close_interrupted_task(db,job_id,resolution):
    """Move a stale queued/running job (and its card) to failed.

    Releasing the hold alone is not enough: the stale row would keep the card in
    an in-flight state forever, and every existing card tool refuses to touch a
    queued/running card. The outcome is recorded as failed, never as done.
    """
    job=db.one('SELECT job_id,code,status FROM invitation_v2_cdk_jobs WHERE job_id=? FOR UPDATE',job_id)
    if not job:return False
    closed=False
    if str(job.get('status') or '') in ('queued','running'):
        closed=db.execute("UPDATE invitation_v2_cdk_jobs SET status='failed',finished_at=?,error=? WHERE job_id=? AND status IN ('queued','running')",time.time(),INTERRUPTED_ERROR,job_id).rowcount==1
    if job.get('code'):
        closed=db.execute("UPDATE invitation_v2_cdk_codes SET status='failed',finished_at=?,error=? WHERE code=? AND status IN ('queued','running')",time.time(),INTERRUPTED_ERROR,job['code']).rowcount==1 or closed
    return closed

class Permits:
    def __init__(self,db,codec,*,can_reclaim=None):
        self.db=db;self.codec=codec;self.instance_id=str(uuid4())
        # 回收"别人的"占位必须先证明本进程**仍然持有执行器排他锁**。数据库连接
        # 断开会静默释放命名锁，而一个已经丢锁、却还在跑的旧实例如果继续回收，
        # 就会把新实例正在使用的占位清掉 —— 两个实例于是可能跑同一个邮箱。
        # 判定由调用方注入；没有判定就一律不回收（fail closed）。
        self.can_reclaim=can_reclaim

    def _may_reclaim(self):
        if self.can_reclaim is None:return False
        try:return bool(self.can_reclaim())
        except Exception:return False

    def claim(self,email,task_id,limit,slots=INVITE_SLOTS):
        low,high=slots
        if not 1<=limit<=high-low+1:raise ValueError('Invalid invitation concurrency limit')
        email=str(email or '').strip().lower()
        token=str(uuid4())
        try:
            with self.db.transaction(requires_new=True):
                # Slot zero is a database-wide allocation mutex. Counting every
                # occupied slot **in this family** prevents a reduced limit from
                # spawning work while its own higher-numbered slots are finishing,
                # without letting the other family's tasks count against it.
                if not self.db.one('SELECT slot_id FROM invitation_v2_execution_permits WHERE slot_id=0 FOR UPDATE'):raise RuntimeError('Invitation permit schema is not installed')
                # 先回收别的执行实例留下的占位。instance_id 是 Permits 实例级 uuid，
                # 服务重建即变；只有能证明本进程仍持排他锁时才允许回收，否则
                # "不是我的 instance_id"也可能属于另一个**活着**的执行器。
                # （泄漏的占位另有启动时的 release_all 兜底。）
                if self._may_reclaim():
                    self.db.execute('UPDATE invitation_v2_execution_permits SET owner_token=NULL,instance_id=NULL,email=NULL,task_id=NULL,acquired_at=NULL WHERE owner_token IS NOT NULL AND (instance_id IS NULL OR instance_id<>?)',self.instance_id)
                if email and self.db.one('SELECT h.job_id FROM invitation_v2_task_holds h JOIN invitation_v2_cdk_jobs j ON j.job_id=h.job_id WHERE j.email=? AND h.resolved_at IS NULL LIMIT 1 FOR UPDATE',email):raise Error('INVITATION_REVIEW_REQUIRED','邮箱有结果不明的历史任务，请先完成安全复核。',409)
                if self.db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL AND slot_id BETWEEN ? AND ?',low,high)>=limit:return None
                if email and self.db.one('SELECT slot_id FROM invitation_v2_execution_permits WHERE email=? AND owner_token IS NOT NULL FOR UPDATE',email):return None
                slot=self.db.scalar('SELECT slot_id FROM invitation_v2_execution_permits WHERE slot_id>=? AND slot_id<=? AND owner_token IS NULL ORDER BY slot_id LIMIT 1 FOR UPDATE SKIP LOCKED',low,high)
                if slot is None:return None
                task_digest=self.codec.fingerprint(str(task_id)) if task_id else None
                result=self.db.execute('UPDATE invitation_v2_execution_permits SET owner_token=?,instance_id=?,email=?,task_id=?,acquired_at=UTC_TIMESTAMP(3) WHERE slot_id=? AND owner_token IS NULL',token,self.instance_id,email or None,task_digest,slot)
                return (slot,token) if result.rowcount==1 else None
        except IntegrityError:return None # Another executor claimed this mailbox.
        except OperationalError as error:
            args=getattr(error.orig,'args',())
            if args and args[0] in (1205,1213):return None
            raise
    def free_slots(self,slots=INVITE_SLOTS):
        """该族还有几个空工位（补号据此决定本轮 spawn 多少个，避免生成任务后干等）。"""
        low,high=slots
        occupied=int(self.db.scalar('SELECT COUNT(*) FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL AND slot_id BETWEEN ? AND ?',low,high) or 0)
        return max(0,high-low+1-occupied)
    def reclaim_foreign(self):
        """回收不属于本实例的占位（服务重建/进程被杀留下的）。返回回收条数。

        与 claim 内的回收同一条规则：证明不了自己仍持排他锁就一条都不动。
        """
        if not self._may_reclaim():return 0
        with self.db.transaction(requires_new=True):
            return self.db.execute('UPDATE invitation_v2_execution_permits SET owner_token=NULL,instance_id=NULL,email=NULL,task_id=NULL,acquired_at=NULL WHERE owner_token IS NOT NULL AND (instance_id IS NULL OR instance_id<>?)',self.instance_id).rowcount
    def release_all(self):
        """清空**全部**占位。只在刚拿到执行器排他锁时调用 —— 此刻不存在别的执行器，
        任何占用都确定不属于本进程。调用方提供了所有权判定时，判定必须为真。"""
        if self.can_reclaim is not None and not self._may_reclaim():
            raise RuntimeError('Refusing to clear execution permits without executor ownership')
        with self.db.transaction(requires_new=True):
            return self.db.execute('UPDATE invitation_v2_execution_permits SET owner_token=NULL,instance_id=NULL,email=NULL,task_id=NULL,acquired_at=NULL WHERE owner_token IS NOT NULL').rowcount
    def release(self,permit):
        slot,token=permit
        with self.db.transaction(requires_new=True):
            return self.db.execute('UPDATE invitation_v2_execution_permits SET owner_token=NULL,instance_id=NULL,email=NULL,task_id=NULL,acquired_at=NULL WHERE slot_id=? AND owner_token=?',slot,token).rowcount==1

router=APIRouter(prefix='/api/v1/admin/invitation/execution-permits')
@router.get('')
def list_permits(request:Request):
    require_permission(request,'invitation.manage')
    return ok({'items':ctx(request).db.all('SELECT slot_id AS slotId,owner_token AS ownerToken,instance_id AS instanceId,email,task_id AS taskId,acquired_at AS acquiredAt FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL ORDER BY slot_id'),'automaticReclaim':False})

@router.post('/{slot_id}/release')
def manual_release(request:Request,slot_id:int=Path(ge=1,le=32),body:dict=Body(...)):
    actor=require_permission(request,'invitation.manage');require_permission(request,'invitation.credentials')
    try:token=str(UUID(str(body.get('ownerToken'))))
    except (TypeError,ValueError,AttributeError):raise bad('请提供占位任务的所有者标识。')
    note=body.get('note')
    if body.get('confirmAllExecutorsStopped') is not True or not isinstance(note,str) or not 3<=len(note.strip())<=250 or any(c in note for c in ('\0','\n','\r')):raise bad('必须确认所有执行实例已停止，并填写复核说明。')
    s=ctx(request)
    with s.db.transaction():
        # Recovery is available through a maintenance API with workers disabled.
        # An active owner, even in this API, makes forced release unsafe.
        with _executor_locks(s.db):
            result=s.db.execute('UPDATE invitation_v2_execution_permits SET owner_token=NULL,instance_id=NULL,email=NULL,task_id=NULL,acquired_at=NULL WHERE slot_id=? AND owner_token=?',slot_id,token)
            if result.rowcount!=1:raise Error('CONFLICT','占位记录已变化，请重新读取并复核。',409)
            # The note can contain operator-supplied sensitive data: retain a hash,
            # not arbitrary text, in the regular audit stream.
            s.audit.record(actor.id,'invitation.permit.manual_release','invitation_execution_permit',str(slot_id),{'ownerToken':token,'confirmedAllExecutorsStopped':True,'reviewNoteDigest':s.codec.sha256(note.strip())})
    return ok({'released':True})

holds_router=APIRouter(prefix='/api/v1/admin/invitation/task-holds')
@holds_router.get('')
def list_holds(request:Request):
    """Unresolved holds, newest first.

    Deliberately carries no card code, no job error and no credential: the
    operator identifies the task by its id, mailbox, original status, the
    recorded interruption reason and the timestamps.
    """
    require_permission(request,'invitation.read')
    items=ctx(request).db.all(
        'SELECT h.job_id AS jobId,h.original_status AS originalStatus,h.reason,h.created_at AS createdAt,'
        'j.email,j.status AS jobStatus,j.stage,j.progress '
        'FROM invitation_v2_task_holds h LEFT JOIN invitation_v2_cdk_jobs j ON j.job_id=h.job_id '
        'WHERE h.resolved_at IS NULL ORDER BY h.created_at,h.job_id')
    return ok({'items':items,'reviewRequiresExecutorsStopped':True})

@holds_router.post('/{job_id}/resolve')
def resolve_hold(request:Request,job_id:str=Path(min_length=8,max_length=72),body:dict=Body(...)):
    """Lift a hold after an operator has confirmed that no executor is running.

    Closing the review never marks the task successful: an interrupted
    queued/running task is recorded as failed so its card becomes usable again,
    and any other outcome stays exactly as it was for the operator to decide
    with the existing card tools.
    """
    actor=require_permission(request,'invitation.manage');require_permission(request,'invitation.credentials')
    resolution=str(body.get('resolution') or '').strip()
    if resolution not in RESOLUTIONS:raise bad('请选择有效的复核结论。')
    note=body.get('note')
    if not isinstance(note,str) or not 3<=len(note.strip())<=250 or any(c in note.strip() for c in ('\0','\n','\r')):raise bad('必须填写 3–250 字的复核说明，且不能包含换行。')
    if body.get('confirmAllExecutorsStopped') is not True:raise bad('必须确认所有执行实例已停止。')
    close_task=body.get('closeInterruptedTask',True)
    if not isinstance(close_task,bool):raise bad('closeInterruptedTask 必须为布尔值。')
    s=ctx(request);marker=mysql.marker(job_id);closed=False
    with s.db.transaction():
        with _executor_locks(s.db):
            row=s.db.one('SELECT job_id,original_status,resolved_at FROM invitation_v2_task_holds WHERE job_id=? FOR UPDATE',marker)
            if not row:raise Error('NOT_FOUND','未找到该复核记录。',404)
            if row['resolved_at'] is not None:raise Error('CONFLICT','该复核记录已经处理过，请刷新后重试。',409)
            if close_task and str(row.get('original_status') or '') in ('queued','running'):closed=_close_interrupted_task(s.db,marker,resolution)
            result=s.db.execute('UPDATE invitation_v2_task_holds SET resolved_at=UTC_TIMESTAMP(3),resolved_by=?,resolution=?,note=?,confirmed_executors_stopped=1 WHERE job_id=? AND resolved_at IS NULL',actor.id,resolution,mysql.protected(note.strip()),marker)
            if result.rowcount!=1:raise Error('CONFLICT','复核记录已变化，请刷新后重试。',409)
            s.audit.record(actor.id,'invitation.task_hold.resolve','invitation_task_hold',str(job_id),{'resolution':resolution,'confirmedAllExecutorsStopped':True,'closeInterruptedTask':close_task,'interruptedTaskClosed':closed,'reviewNoteDigest':s.codec.sha256(note.strip())})
    return ok({'resolved':True,'resolution':resolution,'interruptedTaskClosed':closed})

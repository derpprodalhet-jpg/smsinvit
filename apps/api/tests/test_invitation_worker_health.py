"""执行工人不能静默死掉：任何一步写库异常都不得永久减少容量。

这里用真的线程、真的队列，只把 `webui.cdk` 依赖的 db / registrar / codex_auth
换成记录型替身，所以 `_worker` 的控制流与收尾顺序是被真跑的。
"""
import os
import queue
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

# `webui.registrar` reads these at import time (the service sets them in production).
os.environ.setdefault('SMSINVIT_INVITATION_LOG_DIR', tempfile.mkdtemp(prefix='invitation-logs-'))
os.environ.setdefault('CODEX_HOMES_ROOT', tempfile.mkdtemp(prefix='codex-homes-'))

from webui import cdk


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class Recorder:
    """记录调用并按需注入异常。"""

    def __init__(self, concurrency='1'):
        self.calls = []
        self.fail = set()
        self.concurrency = concurrency
        self.email = 'worker@example.test'
        self.handlers = []
        # F02：默认认领成功；置 False 模拟"作业已被管理员停止/已被别人认领"。
        self.claim_ok = True

    def _maybe_fail(self, name):
        if name in self.fail:
            raise RuntimeError('synthetic ' + name + ' failure')

    # ── db ──
    def get_setting(self, key, default=''):
        return self.concurrency if key == 'cdk_max_concurrency' else default

    def get_cdk_code(self, code):
        self.calls.append(('get_cdk_code', code)); self._maybe_fail('get_cdk_code')
        return {'code': code, 'email': self.email, 'status': 'bound', 'invite_sent_at': 1.0}

    def set_cdk_job(self, job_id, **fields):
        self.calls.append(('set_cdk_job', job_id, fields)); self._maybe_fail('set_cdk_job')

    def set_cdk_status(self, code, status, **fields):
        self.calls.append(('set_cdk_status', code, status, fields)); self._maybe_fail('set_cdk_status')

    def claim_cdk_job(self, job_id, started_at=None):
        """F02：执行前必须原子认领。真实实现只在作业仍是 queued 时返回 True。"""
        self.calls.append(('claim_cdk_job', job_id)); self._maybe_fail('claim_cdk_job')
        return self.claim_ok

    def finish_run(self, run_id, status, error='', category=''):
        self.calls.append(('finish_run', run_id, status, error, category)); self._maybe_fail('finish_run')

    def add_cdk_event(self, *args):
        self.calls.append(('add_cdk_event',) + args)

    def list_cdk_jobs(self, code='', limit=100):
        self.calls.append(('list_cdk_jobs', code)); return []

    def get_cdk_job(self, job_id):
        self.calls.append(('get_cdk_job', job_id)); return None

    def create_cdk_job(self, job_id, code, email='', attempt=1, proxy=''):
        self.calls.append(('create_cdk_job', job_id, code, email))

    # ── registrar ──
    def begin_run(self, email, kind='codex_auth'):
        self.calls.append(('begin_run', email, kind)); self._maybe_fail('begin_run')
        handler = SimpleNamespace(kind='handler')
        self.handlers.append(handler)
        return f'run-{len(self.handlers)}', '/tmp/x.log', queue.Queue(), handler

    def end_run(self, run_id, handler=None):
        self.calls.append(('end_run', run_id, handler)); self._maybe_fail('end_run')

    def classify_error(self, error):
        self.calls.append(('classify_error', error)); self._maybe_fail('classify_error')
        return 'synthetic'

    # ── codex_auth ──
    def run_invite_job(self, code, run_id=None, job_id=None):
        self.calls.append(('run_invite_job', code, run_id, job_id)); self._maybe_fail('run_invite_job')
        return {'ok': True}

    def names(self):
        return [call[0] for call in self.calls]


@pytest.fixture
def workers(monkeypatch):
    record = Recorder()
    monkeypatch.setattr(cdk, 'db', record)
    monkeypatch.setattr(cdk, 'registrar', record)
    monkeypatch.setattr(cdk, 'codex_auth', record)
    monkeypatch.setattr(cdk, 'proxy_pool', SimpleNamespace(has_available=lambda *a, **k: True))
    controllers = []

    def build(concurrency='1'):
        record.concurrency = concurrency
        controller = cdk.CdkController()
        controllers.append(controller)
        return controller

    record.build = build
    yield record
    for controller in controllers:
        with controller._lock:
            alive = [t for t in controller._workers if t.is_alive()]
            for _ in alive:
                controller._q.put(None)
        for thread in alive:
            thread.join(timeout=2)


def dispatch(controller, job_id='job-1', code='CDK-1'):
    controller._q.put({'job_id': job_id, 'code': code})


def failed_writes(record):
    return [(call[0], call[-1]) for call in record.calls
            if call[0] in ('set_cdk_status',) and call[2] == 'failed']


def test_begin_run_failure_keeps_the_worker_alive_and_closes_the_task(workers):
    controller = workers.build()
    workers.fail.add('begin_run')
    assert controller.start_workers()['workers'] == 1
    dispatch(controller)
    assert wait_until(lambda: 'begin_run' in workers.names())
    assert wait_until(lambda: failed_writes(workers))
    assert wait_until(lambda: controller.worker_alive() == 1)
    assert controller._running == 0
    # 没有建起运行记录，就没有 handler 可摘；也不该去写 finish_run。
    assert 'end_run' not in workers.names() and 'finish_run' not in workers.names()
    assert controller.worker_alive() == 1


def test_finish_run_failure_still_returns_the_count_and_detaches_the_handler(workers):
    controller = workers.build()
    workers.fail.add('finish_run')
    controller.start_workers()
    dispatch(controller)
    assert wait_until(lambda: 'finish_run' in workers.names())
    # finish_run 抛异常也不能跳过 end_run：否则 handler 会永久挂在 root logger 上。
    assert wait_until(lambda: 'end_run' in workers.names())
    assert wait_until(lambda: controller._running == 0)
    assert controller.worker_alive() == 1


def test_end_run_failure_still_returns_the_count(workers):
    controller = workers.build()
    workers.fail.add('end_run')
    controller.start_workers()
    dispatch(controller)
    assert wait_until(lambda: 'end_run' in workers.names())
    assert wait_until(lambda: controller._running == 0)
    assert controller.worker_alive() == 1


def test_classify_error_failure_still_finishes_the_run(workers):
    controller = workers.build()
    workers.fail.add('classify_error')
    controller.start_workers()
    dispatch(controller)
    assert wait_until(lambda: 'finish_run' in workers.names())
    assert wait_until(lambda: controller._running == 0)
    assert controller.worker_alive() == 1


def test_an_unexpected_step_failure_never_stops_the_worker(workers):
    controller = workers.build()
    controller.start_workers()
    workers.fail.add('get_cdk_code')
    dispatch(controller, job_id='job-bad')
    assert wait_until(lambda: 'get_cdk_code' in workers.names())
    # 未预料到的异常被循环兜住：这个工人仍然能处理下一个作业。
    workers.fail.discard('get_cdk_code')
    dispatch(controller, job_id='job-good')
    assert wait_until(lambda: ('run_invite_job', 'CDK-1', 'run-1', 'job-good') in workers.calls)
    assert wait_until(lambda: controller._running == 0)
    assert controller.worker_alive() == 1


def test_running_count_returns_to_zero_across_many_failing_jobs(workers):
    controller = workers.build()
    workers.fail.update({'finish_run', 'end_run'})
    controller.start_workers()
    for index in range(5):
        dispatch(controller, job_id=f'job-{index}')
    assert wait_until(lambda: len([c for c in workers.calls if c[0] == 'run_invite_job']) == 5)
    assert wait_until(lambda: controller._running == 0)
    assert controller.worker_alive() == 1


def test_start_workers_replaces_dead_workers_without_exceeding_the_limit(workers):
    controller = workers.build(concurrency='2')
    assert controller.start_workers() == {'ok': True, 'already': False, 'workers': 2, 'created': 2}
    # 一个工人死了：旧实现只会回 {"already": True} 而不补建。
    with controller._lock:
        controller._workers[0] = SimpleNamespace(is_alive=lambda: False)
    result = controller.start_workers()
    assert result['created'] == 1 and result['workers'] == 2 and result['already'] is False
    assert result['workers'] == controller.max_concurrency()
    # 再调一次：都活着，什么都不建。
    assert controller.start_workers() == {'ok': True, 'already': True, 'workers': 2, 'created': 0}


def test_ensure_workers_is_a_noop_before_the_executor_started(workers):
    controller = workers.build()
    assert controller.ensure_workers() == 0
    assert controller._workers == [] and controller._started is False


def test_enqueue_is_refused_when_a_started_executor_has_no_usable_worker(workers):
    controller = workers.build()
    controller._started = True

    def broken():
        raise RuntimeError('synthetic spawn failure')

    controller._spawn = broken
    result = controller.enqueue('CDK-1')
    assert result['ok'] is False and result['reason'] == 'busy'
    # 拒绝时不能建作业、也不能动卡密状态。
    assert 'set_cdk_job' not in workers.names() and 'set_cdk_status' not in workers.names()


def test_enqueue_keeps_its_old_contract_before_the_executor_started(workers):
    """未启动的进程里 enqueue 行为与旧实现一致（工人门禁只在已启动时生效）。"""
    controller = workers.build()
    result = controller.enqueue('CDK-1')
    assert result['ok'] is True and result['already'] is False
    assert 'create_cdk_job' in workers.names()
    queued = [call for call in workers.calls if call[0] == 'set_cdk_status']
    assert queued and queued[-1][1] == 'CDK-1' and queued[-1][2] == 'queued'


def test_repair_is_attempted_on_the_admission_path(workers):
    """准入前先补建：容量回来就继续收活，这是"自愈"而不是"重启才恢复"。"""
    from smsinvit.invitation.service import InvitationService
    controller = workers.build()
    controller.start_workers()
    with controller._lock:
        controller._workers[:] = [SimpleNamespace(is_alive=lambda: False)]
    service = InvitationService.__new__(InvitationService)
    service._executor_lock = threading.RLock(); service._executor_enabled = True
    service._started = True; service._stopping = False
    service._process_lock = SimpleNamespace(exec_driver_sql=lambda sql: SimpleNamespace(scalar=lambda: 5))
    service._process_lock_owner = 5
    service.cdk = SimpleNamespace(CONTROLLER=controller)
    service.require_executor_accepting()
    assert controller.worker_alive() >= 1


def test_admission_refuses_when_the_capacity_cannot_be_repaired(workers):
    from smsinvit.errors import Error
    from smsinvit.invitation.service import InvitationService
    controller = workers.build()
    controller._started = True
    controller._spawn = lambda: (_ for _ in ()).throw(RuntimeError('synthetic spawn failure'))
    service = InvitationService.__new__(InvitationService)
    service._executor_lock = threading.RLock(); service._executor_enabled = True
    service._started = True; service._stopping = False
    service._process_lock = SimpleNamespace(exec_driver_sql=lambda sql: SimpleNamespace(scalar=lambda: 5))
    service._process_lock_owner = 5
    service.cdk = SimpleNamespace(CONTROLLER=controller)
    with pytest.raises(Error) as error:
        service.require_executor_accepting()
    assert error.value.code == 'INVITATION_EXECUTOR_UNAVAILABLE' and error.value.status == 503


def test_losing_the_claim_skips_the_job_without_starting_any_work(workers):
    """F02：认领失败（作业已被管理员停止）时丢弃队列项，不建运行记录、不写失败。"""
    controller = workers.build()
    workers.claim_ok = False
    assert controller.start_workers()['workers'] == 1
    dispatch(controller)
    assert wait_until(lambda: 'claim_cdk_job' in workers.names())
    assert wait_until(lambda: controller.worker_alive() == 1)
    assert controller._running == 0
    # 认领失败是"这个队列项失效了"，不是作业失败：不该建运行记录，也不该回写失败，
    # 否则会把管理员写的停止原因覆盖掉。
    assert 'begin_run' not in workers.names()
    assert 'set_cdk_status' not in workers.names()
    assert 'finish_run' not in workers.names()

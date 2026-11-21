from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from smsinvit.config import Config
from smsinvit.common.jobs import Scheduler,DurableJobs

class DB:
    def one(self,*args):return None

def test_scheduler_runtime_changes_apply_and_disabled_runs_do_not_increase_count():
    services=SimpleNamespace(db=DB(),config=Config(environ={}))
    scheduler=Scheduler(services);calls=[]
    scheduler.register('worker.sms_sweep',lambda:calls.append(True),15000)
    scheduler.run('worker.sms_sweep')
    assert len(calls)==1 and scheduler.tasks['worker.sms_sweep']['runCount']==1
    services.config.runtime['runtime.worker_sms_sweep_ms']='23000'
    scheduler.run('worker.sms_sweep',manual=True)
    task=scheduler.tasks['worker.sms_sweep']
    assert task['intervalMs']==23000 and task['nextDueAt']-task['lastRunAt']==timedelta(seconds=23)
    before=task['runCount']
    scheduler.controls={'worker.sms_sweep':{'enabled':False,'cron':''}}
    # Status/next due reads are observational and must not pulse completed work.
    scheduler.interval('worker.sms_sweep');scheduler.next_due('worker.sms_sweep',datetime.now(timezone.utc),23000)
    assert task['runCount']==before

def test_original_durable_worker_lease_prevents_short_lease_replay():
    store=DurableJobs(SimpleNamespace(db=DB()));claims=[]
    store.claim=lambda batch,lease:claims.append((batch,lease)) or []
    store.tick()
    assert claims==[(1,timedelta(minutes=5))]

def test_spring_six_field_cron_timezone_and_invalid_persisted_fallback():
    services=SimpleNamespace(db=DB(),config=Config({'runtime.business_time_zone':'Asia/Shanghai'},environ={}))
    scheduler=Scheduler(services);now=datetime(2026,10,7,15,55,tzinfo=timezone.utc)
    scheduler.controls={'worker.process':{'cron':'0 0 0 * * *'}}
    assert scheduler.next_due('worker.process',now,1000)==datetime(2026,10,7,16,0,tzinfo=timezone.utc)
    scheduler.controls={'worker.process':{'cron':'malformed legacy cron'}}
    assert scheduler.next_due('worker.process',now,1000)==now+timedelta(seconds=1)

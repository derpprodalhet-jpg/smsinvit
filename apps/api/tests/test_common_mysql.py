"""Integration checks restricted to the disposable smsinvit_test database.

Enable explicitly with SMSINVIT_MYSQL_TEST=1. Credentials are read from the local
private fixture file, never printed, and only fixture-owned rows are removed.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from types import SimpleNamespace
import pytest
from sqlalchemy import URL
from smsinvit.config import Config
from smsinvit.db import Database
from smsinvit.errors import Error
from smsinvit.security import Principal,totp_code
from smsinvit.app import Services
from smsinvit.sms.service import SmsService

pytestmark=pytest.mark.skipif(os.environ.get('SMSINVIT_MYSQL_TEST')!='1',reason='isolated MySQL integration explicitly opt-in')

@pytest.fixture
def services():
    work=Path(__file__).resolve().parents[3];password=json.loads((work/'data/test-private/mysql.json').read_text())['password']
    db=Database(URL.create('mysql+pymysql',username='root',password=password,host='127.0.0.1',port=13308,database='smsinvit_test',query={'charset':'utf8mb4'}),pool_size=20)
    assert db.scalar('SELECT DATABASE()')=='smsinvit_test'
    suffix=uuid4().hex[:12];prefix='common-'+suffix
    config=Config({'smsinvit.inventory-encryption-key':'0a'*32,'smsinvit.inventory-fingerprint-key':'common-test-fingerprint-key','smsinvit.jwt-secret':'common-test-jwt-key-32-bytes-long!!','runtime.totp_enabled':False,'runtime.secure_cookies':False,'runtime.cors_origins':'http://localhost:3001'},environ={})
    s=Services(db,config);s.sms=SmsService(db,s.codec,config,tokens=s.tokens,jobs=s.jobs,outbox=s.outbox,policy=s.policy,http=s.http)
    actor_id=str(uuid4());username='common_'+suffix;email=username+'@example.test'
    with db.transaction():
        db.execute('INSERT INTO admin_users(id,username,email,password_hash,display_name) VALUES(?,?,?,?,?)',actor_id,username,email,s.codec.hash_password('common-password-1'),'Common integration')
        role=db.scalar("SELECT id FROM roles WHERE code='ADMIN'")
        db.execute('INSERT INTO admin_user_roles(id,admin_user_id,role_id) VALUES(?,?,?)',str(uuid4()),actor_id,role)
    actor=Principal(actor_id,str(uuid4()),('SUPER_ADMIN',));s.fixture_actor=actor;s.fixture_username=username;s.fixture_prefix=prefix;s.fixture_skus=[];s.fixture_notifications=[]
    yield s
    with db.transaction():
        for sku in s.fixture_skus:
            db.execute('DELETE r FROM redeem_cdk_registry r JOIN inventory_items i ON i.id=r.owner_inventory_id WHERE i.sku_id=?',sku)
            db.execute('DELETE FROM inventory_items WHERE sku_id=?',sku);db.execute('DELETE FROM inventory_import_batches WHERE sku_id=?',sku);db.execute('DELETE FROM skus WHERE id=?',sku)
        for id in s.fixture_notifications:db.execute('DELETE FROM notification_outbox WHERE id=?',id)
        db.execute('DELETE FROM durable_jobs WHERE deduplication_key LIKE ?',prefix+'%')
        db.execute('DELETE FROM audit_logs WHERE actor_user_id=?',actor_id)
        db.execute('DELETE FROM notification_channel_settings WHERE updated_by=?',actor_id)
        db.execute('DELETE FROM admin_users WHERE id=?',actor_id)
    db.close()

def plan(s):
    attrs={'productKind':'SMS_REDEEM','smsService':'oa','smsCountry':0,'smsMinPrice':0,'smsMaxPrice':1,'smsMinCount':1,'cdkValidDays':15}
    row=s.plans.create({'code':s.fixture_prefix.upper(),'name':'SMS fixture','attributesJson':attrs},s.fixture_actor);s.fixture_skus.append(row['id']);return row

def login(s):
    captcha=s.captcha.issue('math');a,op,b,*_=captcha['prompt'].split();answer=int(a)+int(b) if op=='+' else int(a)-int(b)
    return s.auth.login({'identifier':s.fixture_username,'password':'common-password-1','captchaToken':captcha['captchaToken'],'captchaAnswer':str(answer)},'198.51.100.1','isolated-test')

def test_auth_refresh_rotation_and_revoke_preserve_old_hashes(services):
    s=services;issued=login(s);actor=s.auth.authenticate(issued['accessToken']);assert actor.user_id==s.fixture_actor.user_id
    refreshed=s.auth.refresh(issued['refreshToken'],'198.51.100.1','test')
    with pytest.raises(Error) as e:s.auth.authenticate(issued['accessToken'])
    assert e.value.code=='AUTH_SESSION_EXPIRED'
    with pytest.raises(Error):s.auth.refresh(issued['refreshToken'],'198.51.100.1','test')
    actor=s.auth.authenticate(refreshed['accessToken']);s.auth.logout(actor)
    with pytest.raises(Error):s.auth.authenticate(refreshed['accessToken'])

def test_totp_setup_enable_challenge_and_backup_one_use(services):
    s=services;issued=login(s);actor=s.auth.authenticate(issued['accessToken']);s.config.runtime['runtime.totp_enabled']='true'
    setup=s.auth.setup(actor.user_id);enabled=s.auth.enable(actor,totp_code(setup['secret']));assert len(enabled['backupCodes'])==8
    first=login(s);assert first['needTotp'] is True and 'accessToken' not in first
    complete=s.auth.login_totp(first['challengeToken'],enabled['backupCodes'][0],'198.51.100.1','test')
    assert s.auth.authenticate(complete['accessToken']).totp_setup_required is False
    assert s.auth.totp_status(actor.user_id)['backupCodesRemaining']==7
    second=login(s)
    with pytest.raises(Error) as e:s.auth.login_totp(second['challengeToken'],enabled['backupCodes'][0],'198.51.100.1','test')
    assert e.value.code=='AUTH_TOTP_INVALID'
    with pytest.raises(Error):s.auth.login_totp(first['challengeToken'],totp_code(setup['secret']),'198.51.100.1','test')

def test_issuance_snapshots_export_invalidate_and_atomic_audit(services):
    s=services;sku=plan(s);generated=s.inventory.generate(sku['id'],2,20,s.fixture_actor)
    assert generated['successCount']==2 and generated['requestedCount']==2
    exported=s.inventory.export(generated['batchId'],s.fixture_actor)
    assert exported['count']==2 and len(exported['secrets'])==2 and exported['text']=='\n'.join(exported['secrets'])
    rows=s.inventory.list(sku['id'],1,20,None)['data'];assert len(rows)==2
    s.plans.update(sku['id'],{'attributesJson':{**sku['attributesJson'],'smsCountry':1}},s.fixture_actor)
    assert s.db.scalar('SELECT COUNT(*) FROM sms_redeem_keys WHERE sku_id=? AND sms_country=0',sku['id'])==2
    row=rows[0];s.inventory.invalidate(row['id'],s.fixture_actor)
    assert s.db.scalar('SELECT status FROM sms_redeem_keys WHERE inventory_item_id=?',row['id'])=='DISABLED'
    assert s.inventory.counts(sku['id'])['counts']=={'ISSUED':1,'INVALID':1}
    assert s.db.scalar('SELECT COUNT(*) FROM audit_logs WHERE actor_user_id=? AND action=?',s.fixture_actor.user_id,'inventory.generate')==1

def test_issuance_module_failure_rolls_back_batch_items_registry_audit(services):
    s=services;sku=plan(s);original=s.sms.register_issued_key
    def fail(*args):original(*args);raise RuntimeError('simulated module failure')
    s.sms.register_issued_key=fail
    with pytest.raises(RuntimeError):s.inventory.generate(sku['id'],3,20,s.fixture_actor)
    assert s.db.scalar('SELECT COUNT(*) FROM inventory_items WHERE sku_id=?',sku['id'])==0
    assert s.db.scalar('SELECT COUNT(*) FROM inventory_import_batches WHERE sku_id=?',sku['id'])==0
    assert s.db.scalar('SELECT COUNT(*) FROM sms_redeem_keys WHERE sku_id=?',sku['id'])==0

def test_cdk_duplicate_registry_under_100_concurrent_issuance(services,monkeypatch):
    # A forced random collision represents duplicate issuance; one global registry
    # owner, inventory item and business key survive 100 racing transactions.
    s=services;sku=plan(s);fixed='LC-CDK-Collision12345678901'
    from smsinvit.common import inventory
    monkeypatch.setattr(inventory,'generate_cdk',lambda length:fixed)
    def issue(_):return s.inventory.generate(sku['id'],1,20,s.fixture_actor)
    with ThreadPoolExecutor(max_workers=20) as pool:results=list(pool.map(issue,range(100)))
    assert sum(row['successCount'] for row in results)==1
    assert s.db.scalar('SELECT COUNT(*) FROM inventory_items WHERE sku_id=?',sku['id'])==1
    assert s.db.scalar('SELECT COUNT(*) FROM sms_redeem_keys WHERE sku_id=?',sku['id'])==1
    assert s.db.scalar('SELECT COUNT(*) FROM redeem_cdk_registry WHERE code_fingerprint=?',s.codec.fingerprint(fixed))==1

def test_failed_login_rate_counter_commits_outside_auth_rollback(services):
    s=services;bucket=s.fixture_prefix+'-rate';hash_=s.codec.sha256(bucket)
    try:
        with pytest.raises(ValueError):
            with s.db.transaction():s.limiter.check(bucket,1);raise ValueError('login failed')
        assert s.db.scalar('SELECT attempts FROM auth_rate_limits WHERE key_hash=?',hash_)==1
        with pytest.raises(Error) as e:s.limiter.check(bucket,1)
        assert e.value.status==429 and s.db.scalar('SELECT attempts FROM auth_rate_limits WHERE key_hash=?',hash_)==2
    finally:s.db.execute('DELETE FROM auth_rate_limits WHERE key_hash=?',hash_)

def test_windowed_retry_after_reports_the_real_remaining_window(services):
    """带窗口的限流必须回报真实剩余时间，而不是固定回一整个窗口。

    桶的窗口从**首次**请求起算：900 秒的窗口在第 80 秒被拦下时，旧实现回
    `Retry-After: 900`，前台据此停掉自动查询整整 15 分钟（F04 的放大效应）。
    """
    s=services;bucket=s.fixture_prefix+'-window-rate';hash_=s.codec.sha256(bucket)
    try:
        s.db.execute('INSERT INTO api_rate_limits(key_hash,attempts,resets_at) VALUES (?,?,DATE_ADD(NOW(3),INTERVAL 130 SECOND))',hash_,5)
        with pytest.raises(Error) as e:s.limiter.check(bucket,4,900)
        assert e.value.status==429
        # 允许几秒执行误差，但必须明显小于窗口本身。
        assert 110<=e.value.retry_after<=130, e.value.retry_after
    finally:s.db.execute('DELETE FROM api_rate_limits WHERE key_hash=?',hash_)

def test_windowed_retry_after_falls_back_to_the_window_when_no_row_exists(services):
    """异常路径（窗口行读不到）也不能给出 0 或负数。"""
    s=services;bucket=s.fixture_prefix+'-window-fallback';hash_=s.codec.sha256(bucket)
    try:
        with pytest.raises(Error) as e:s.limiter.check(bucket,0,900)
        assert e.value.status==429 and e.value.retry_after>=1
    finally:s.db.execute('DELETE FROM api_rate_limits WHERE key_hash=?',hash_)

def test_notification_uncertain_retry_and_lease_fence(services):
    s=services
    with s.db.transaction():
        # 通知通道只剩 Telegram（EMAIL/SMTP 已删除），这里改用 TELEGRAM 行验证不确定重发与租约围栏。
        id=s.notifications.enqueue(s.fixture_prefix+'-notice','TELEGRAM','TELEGRAM',None,{'chat':'-100123','body':'private-token'},datetime.now(timezone.utc)+timedelta(hours=1));s.fixture_notifications.append(id)
    s.db.execute("UPDATE notification_outbox SET status='UNCERTAIN' WHERE id=?",id)
    with pytest.raises(Error) as e:s.notifications.retry(s.fixture_actor,id,False)
    assert e.value.code=='NOTIFICATION_CONFIRM_REQUIRED'
    assert s.notifications.retry(s.fixture_actor,id,True)=={'queued':True}
    lease='new-lease'
    s.db.execute("UPDATE notification_outbox SET status='SENDING',lease_token=?,locked_until=DATE_ADD(NOW(3),INTERVAL 5 MINUTE) WHERE id=?",lease,id)
    assert s.notifications.worker.finish(id,'stale-lease','SENT',None,None)==0
    assert s.db.scalar('SELECT status FROM notification_outbox WHERE id=?',id)=='SENDING'
    assert s.notifications.worker.finish(id,lease,'SENT',None,None)==1

def test_authentication_notification_payload_never_revealed(services):
    # 兼容历史行：EMAIL 通道已删除、代码里不再产出认证邮件事件，但旧的
    # PASSWORD_RESET 记录仍必须保持正文脱敏（AUTH_KINDS 只服务这条兼容路径）。
    #
    # 本用例原本就断言「reveal=False 与 reveal=True 都不得出现秘密」，而实现曾经让
    # reveal=True 绕过脱敏、把 body（重置链接/验证码）原样返回——那是一条真实的泄漏，
    # 只有这一条断言在报错。现在认证邮件正文无条件隐藏，用例与实现重新对齐。
    s=services
    with s.db.transaction():
        id=s.notifications.enqueue(s.fixture_prefix+'-auth','EMAIL','PASSWORD_RESET',None,{'to':'recipient@example.test','body':'reset-private-token','token':'private-reset-token'},datetime.now(timezone.utc)+timedelta(hours=1));s.fixture_notifications.append(id)
    for reveal in (False,True):
        result=s.notifications.detail(s.fixture_actor,id,reveal)
        assert result['payload']['_redacted'] is True
        assert 'reset-private-token' not in json.dumps(result,default=str)

def test_normal_notification_payload_still_reveals_and_strips_secret_fields(services):
    """收紧认证邮件不能顺手把普通通知的 reveal 也关掉。

    普通通知（非 AUTH_KINDS）的 reveal 仍是后台的既定能力：需要 inventory.export
    且会写审计。这里固定两点：reveal=False 时给出可读正文；reveal=True 时解密但必须
    去掉 token/code/tokenHash/codeHash/challengeKey 这些可被直接拿来用的字段。
    """
    s=services
    with s.db.transaction():
        id=s.notifications.enqueue(s.fixture_prefix+'-ops','TELEGRAM','TELEGRAM',None,{'chat':'ops','body':'运营通知正文','token':'must-not-appear','tokenHash':'must-not-appear-either'},datetime.now(timezone.utc)+timedelta(hours=1));s.fixture_notifications.append(id)
    hidden=s.notifications.detail(s.fixture_actor,id,False)
    assert hidden['payload']['body']=='运营通知正文'
    assert 'must-not-appear' not in json.dumps(hidden,default=str)
    revealed=s.notifications.detail(s.fixture_actor,id,True)
    assert revealed['payload']['body']=='运营通知正文'
    for stripped in ('token','code','tokenHash','codeHash','challengeKey'):
        assert stripped not in revealed['payload']
    assert 'must-not-appear' not in json.dumps(revealed,default=str)

def test_durable_job_lease_fencing_after_restart_recovery(services):
    from smsinvit.common.jobs import DurableJobs
    # Claim operates on the whole queue, so it needs an entirely independent
    # schema. Make the job due according to the database clock: this test checks
    # lease fencing, not sub-millisecond host/VM clock alignment or DATETIME(3)
    # rounding of the normal enqueue timestamp.
    s=services;name='smsinvit_common_lease_'+uuid4().hex[:12];db=None
    s.db.execute('CREATE DATABASE `'+name+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    try:
        s.db.execute('CREATE TABLE `'+name+'`.durable_jobs LIKE smsinvit_test.durable_jobs')
        db=Database(s.db.engine.url.set(database=name),pool_size=4)
        assert db.scalar('SELECT DATABASE()')==name
        jobs=DurableJobs(SimpleNamespace(db=db))
        due=db.scalar('SELECT DATE_SUB(NOW(3),INTERVAL 1 SECOND)')
        with db.transaction():jobs.enqueue('QUEUE_PROBE',s.fixture_prefix+'-job',{'probe':'fixture'},available_at=due)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims=list(pool.map(lambda _:jobs.claim(batch_size=100,lease=60),range(2)))
        claimed=[job for batch in claims for job in batch]
        assert len(claimed)==1 and json.loads(claimed[0].payload)=={'probe':'fixture'}
        job=claimed[0]
        assert db.scalar('SELECT COUNT(*) FROM durable_jobs')==1
        db.execute('UPDATE durable_jobs SET locked_until=DATE_SUB(NOW(3),INTERVAL 1 SECOND) WHERE id=?',job.id)
        assert not jobs.complete(job)
        recovered=jobs.claim(batch_size=100,lease=60)
        assert len(recovered)==1 and recovered[0].id==job.id
        replacement=recovered[0]
        assert replacement.lease_token!=job.lease_token and not jobs.complete(job)
        assert jobs.complete(replacement)
    finally:
        if db:db.close()
        s.db.execute('DROP DATABASE `'+name+'`')

"""Explicit, disposable two-source migration tests.

Run independently of the live invitation-engine tests: its service binding is
intentionally immutable per process. All writes target random test schemas. The
optional real-source mode SELECTs only from the restored legacy database and
opens only its previously consolidated SQLite copy, never the desktop original.
"""
from __future__ import annotations
import hashlib,hmac,json,os,re,secrets,sqlite3,sys,time
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pymysql,pytest
from sqlalchemy import URL
from smsinvit.db import Database
from smsinvit.security import SecretCodec,TokenService
from smsinvit.errors import Error
from smsinvit.invitation import mysql

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'scripts'))
from import_invitation import Importer,Conflict,DryRun,checksum
from database_tools import MaintenanceError

pytestmark=pytest.mark.skipif(not os.environ.get('SMSINVIT_TEST_PRIVATE_CONFIG'),reason='Explicit disposable MySQL fixture not enabled')
CODEC=SecretCodec(bytes(range(32)).hex(),'migration-fixture-fingerprint')
JWT='migration-fixture-jwt-more-than32characters'

@pytest.fixture
def database():
    password=json.loads(Path(os.environ['SMSINVIT_TEST_PRIVATE_CONFIG']).read_text())['password']
    name='smsinvit_inv_migration_'+secrets.token_hex(5)
    admin=pymysql.connect(host='127.0.0.1',port=13308,user='root',password=password,client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS)
    assert re.fullmatch('smsinvit_inv_migration_[0-9a-f]{10}',name)
    db=None
    try:
        with admin.cursor() as cursor:
            cursor.execute('CREATE DATABASE `'+name+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci');cursor.execute('USE `'+name+'`')
            for file in sorted((ROOT/'migrations/legacy').glob('V*.sql'))+[ROOT/'migrations/V7__python_invitation.sql']:
                cursor.execute(file.read_text())
                while cursor.nextset():pass
        admin.commit()
        db=Database(URL.create('mysql+pymysql',username='root',password=password,host='127.0.0.1',port=13308,database=name,query={'charset':'utf8mb4'}))
        yield db
    finally:
        if db:db.close()
        with admin.cursor() as cursor:cursor.execute('DROP DATABASE `'+name+'`')
        admin.close()

def insert(db,table,row):
    columns=list(row)
    db.execute('INSERT INTO `'+table+'`('+','.join('`'+k+'`' for k in columns)+') VALUES('+','.join('?' for _ in columns)+')',*[row[k] for k in columns])

def legacy_fixture(db,codec=CODEC):
    sku=str(uuid4());attrs={'productKind':'CODEX_INVITE_REDEEM','cdkValidDays':15}
    insert(db,'skus',{'id':sku,'code':'migration-fixture','name':'migration fixture','attributes_json':json.dumps(attrs)})
    insert(db,'invitation_settings',{'id':1,'settings_ciphertext':codec.encrypt(json.dumps({'concurrency':7,'sms_api_key':'legacy-secret-never-in-reports','say_hi_prompt':'shared prompt'}))})
    user=str(uuid4());password=codec.hash_password('old-admin-fixture-password');totp=codec.encrypt('JBSWY3DPEHPK3PXP')
    insert(db,'admin_users',{'id':user,'username':'old-admin','email':'old-admin@example.test','password_hash':password,'totp_secret_encrypted':totp,'status':'ACTIVE'})
    now=datetime(2026,1,1,1,2,3,123000);records=[]
    for index,(state,safe) in enumerate((('RUNNING',False),('FAILED',False),('PREVIEWED',False),('FAILED',True))):
        kid,iid,mail,sid,old_sid=[str(uuid4()) for _ in range(5)];code='Legacy-Mixed-'+str(index);email='legacy-'+str(index)+'@example.test'
        insert(db,'inventory_items',{'id':iid,'sku_id':sku,'status':'ISSUED','secret_ciphertext':codec.encrypt(code),'secret_fingerprint':codec.fingerprint(code),'issued_at':now,'updated_at':now})
        insert(db,'redeem_cdk_registry',{'business_kind':'CODEX_INVITE_REDEEM','code_fingerprint':codec.fingerprint(code),'owner_inventory_id':iid})
        insert(db,'invitation_mail_accounts',{'id':mail,'email':email,'email_fingerprint':codec.fingerprint(email),'kind':'OUTLOOK','credentials_ciphertext':codec.encrypt(json.dumps({'password':'private-mail-fixture','client_id':'client-fixture','refresh_token':'refresh-fixture'})),'status':'CLAIMED','created_at':now})
        insert(db,'invitation_redeem_keys',{'id':kid,'inventory_item_id':iid,'sku_id':sku,'code_fingerprint':codec.fingerprint(code),'claimed_mail_account_id':mail,'claimed_email_fingerprint':codec.fingerprint(email),'status':'CLAIMED','attributes_snapshot_json':json.dumps(attrs),'expires_at':now+timedelta(days=15),'created_at':now})
        common={'redeem_key_id':kid,'mail_account_id':mail,'email':email,'email_fingerprint':codec.fingerprint(email),'created_at':now}
        insert(db,'invitation_runs',{'id':old_sid,**common,'status':'FAILED','is_current':0,'attempt':1,'retry_safe':1,'account_credentials_ciphertext':codec.encrypt(json.dumps({'access_token':'old-superseded-token'}))})
        insert(db,'invitation_runs',{'id':sid,**common,'status':state,'is_current':1,'attempt':2,'retry_safe':int(safe),'execution_settings_ciphertext':codec.encrypt(json.dumps({'proxy':'fixture-proxy'})),'account_credentials_ciphertext':codec.encrypt(json.dumps({'access_token':'current-token-'+str(index),'password':'registered-password-fixture'}))})
        insert(db,'invitation_run_events',{'id':str(uuid4()),'run_id':old_sid,'attempt':1,'stage':'legacy','message_ciphertext':codec.encrypt('private historical event'),'progress_percent':10})
        records.append(SimpleNamespace(kid=kid,iid=iid,code=code,email=email,sid=sid,old_sid=old_sid,state=state,safe=safe))
    return SimpleNamespace(sku=sku,records=records,user=user,password=password,totp=totp)

def sqlite_fixture(path):
    with sqlite3.connect(path) as con:
        con.executescript('''CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE outlook_accounts(email TEXT PRIMARY KEY,password TEXT,client_id TEXT,refresh_token TEXT,kind TEXT,status TEXT,imported_at REAL);
        CREATE TABLE cdk_codes(code TEXT PRIMARY KEY,status TEXT,email TEXT,created_at REAL);
        CREATE TABLE cdk_jobs(job_id TEXT PRIMARY KEY,code TEXT,email TEXT,status TEXT,created_at REAL,attempt INTEGER);
        CREATE TABLE runs(run_id TEXT PRIMARY KEY,email TEXT,status TEXT,log_path TEXT,error TEXT);
        CREATE TABLE codex_auth(email TEXT PRIMARY KEY,access_token TEXT,refresh_token TEXT,auth_json TEXT);''')
        con.executemany('INSERT INTO settings VALUES(?,?)',[('cdk_max_concurrency','3'),('sms_api_key_smsbower','native-secret-never-in-reports'),('sayhi_prompt','shared prompt'),('native_only','native value'),('codex_binary','/source/private/bin/codex'),('codex_home_root','/source/private/cli-home')])
        con.execute('INSERT INTO outlook_accounts VALUES(?,?,?,?,?,?,?)',('native@example.test','native-mail-password','native-client','native-refresh','outlook','available',1700000000.0))
        for index,state in enumerate(('queued','running','review','done')):
            code='NATIVE-'+str(index);email='native-'+str(index)+'@example.test'
            con.execute('INSERT INTO cdk_codes VALUES(?,?,?,?)',(code,state,email,1700000000.0))
            con.execute('INSERT INTO cdk_jobs VALUES(?,?,?,?,?,?)',('native-job-NATIVE-'+str(index),code,email,state,1700000000.0,2))
        con.execute('INSERT INTO runs VALUES(?,?,?,?,?)',('native-run-NATIVE-0','native@example.test','failed','/source/private/logs/run.log','private old error'))
        con.execute('INSERT INTO codex_auth VALUES(?,?,?,?)',('native@example.test','native-auth-access','native-auth-refresh',json.dumps({'access_token':'native-auth-access'})))
    return path

def run_import(db,snapshot,sku,codec=CODEC,resolve=None,apply=True):
    imp=Importer(db,codec,resolve)
    try:
        with db.transaction():
            imp.legacy()
            if snapshot:imp.sqlite(snapshot,sku)
            if imp.conflicts:raise Conflict()
            if not apply:raise DryRun()
    except (Conflict,DryRun):pass
    return imp

def native_counts(db):
    return {row['TABLE_NAME']:db.scalar('SELECT COUNT(*) FROM `'+row['TABLE_NAME']+'`') for row in db.all("SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_NAME LIKE 'invitation_v2_%'")}

def test_default_conflicts_are_reviewable_secret_free_and_entire_import_rolls_back(database,tmp_path):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source.sqlite3');before=native_counts(database)
    old_count=database.scalar('SELECT COUNT(*) FROM inventory_items')
    imp=run_import(database,snapshot,source.sku)
    assert {r['setting'] for r in imp.conflicts}=={'cdk_max_concurrency','sms_api_key_smsbower'}
    assert all(r['reason']=='TARGET_DIFFERS' for r in imp.conflicts);assert not imp.resolutions
    assert not any(v in json.dumps(imp.conflicts) for v in ('legacy-secret','native-secret','native value'))
    assert native_counts(database)==before;assert database.scalar('SELECT COUNT(*) FROM inventory_items')==old_count

@pytest.mark.parametrize('preferred,expected',[('codex_invit','3'),('smsinvit_legacy','7')])
def test_explicit_setting_choice_archives_both_values_and_repeated_import_is_idempotent(database,tmp_path,preferred,expected):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source.sqlite3')
    imp=run_import(database,snapshot,source.sku,resolve=preferred)
    assert not imp.conflicts;assert len(imp.resolutions)==2
    assert mysql.revealed(database.scalar("SELECT value FROM invitation_v2_settings WHERE `key`='cdk_max_concurrency'"))==expected
    assert all(r['selectedSource']==preferred for r in imp.resolutions)
    assert {r['action'] for r in imp.resolutions}==({'REPLACED'} if preferred=='codex_invit' else {'KEPT'})
    assert not any(v in json.dumps(imp.resolutions) for v in ('legacy-secret','native-secret'))
    archived=[json.loads(CODEC.decrypt(row['ciphertext'])) for row in database.all("SELECT ciphertext FROM invitation_v2_legacy_documents WHERE source_table='resolution.settings'")]
    assert {r['setting'] for r in archived}=={'cdk_max_concurrency','sms_api_key_smsbower'}
    secret_decision=next(r for r in archived if r['setting']=='sms_api_key_smsbower')
    assert secret_decision['previous']['value']=='legacy-secret-never-in-reports';assert secret_decision['incoming']['value']=='native-secret-never-in-reports'
    assert mysql.revealed(database.scalar("SELECT value FROM invitation_v2_settings WHERE `key`='codex_binary'"))=='codex'
    assert database.scalar('SELECT log_path FROM invitation_v2_runs WHERE run_id=?',mysql.marker('native-run-NATIVE-0'))==''
    before=native_counts(database);repeat=run_import(database,snapshot,source.sku,resolve=preferred)
    assert not repeat.conflicts and not repeat.resolutions;assert repeat.counts['skipped']>20;assert native_counts(database)==before
    assert database.scalar("SELECT COUNT(*) FROM redeem_cdk_registry WHERE business_kind='CODEX_INVITE_REDEEM'")==8

def test_dry_run_with_explicit_resolution_changes_nothing(database,tmp_path):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source.sqlite3');before=native_counts(database)
    imp=run_import(database,snapshot,source.sku,resolve='codex_invit',apply=False)
    assert not imp.conflicts and len(imp.resolutions)==2;assert native_counts(database)==before

def test_unknown_jobs_are_held_without_replay_and_only_current_legacy_ids_authorized(database,tmp_path):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source.sqlite3');imp=run_import(database,snapshot,source.sku,resolve='codex_invit')
    assert not imp.conflicts
    rows=database.all('SELECT job_id,status FROM invitation_v2_cdk_jobs');states={mysql.revealed(r['job_id']):r['status'] for r in rows}
    assert states['native-job-NATIVE-0']=='queued';assert states['native-job-NATIVE-1']=='running';assert states['native-job-NATIVE-2']=='review';assert states['native-job-NATIVE-3']=='done'
    holds=database.all('SELECT job_id,original_status,reason,resolved_at FROM invitation_v2_task_holds');by_id={mysql.revealed(r['job_id']):r for r in holds}
    assert len(holds)==5 and all(r['resolved_at'] is None for r in holds)
    assert [by_id['native-job-NATIVE-'+str(i)]['original_status'] for i in range(3)]==['queued','running','review']
    assert all(by_id['native-job-NATIVE-'+str(i)]['reason']=='NATIVE_UNCERTAIN' for i in range(3))
    stored=json.dumps(rows+holds+database.all('SELECT run_id FROM invitation_v2_runs'))
    assert all('NATIVE-'+str(i) not in stored for i in range(4))
    assert all(row['job_id'].startswith('iv2$') for row in rows+holds)
    for table in ('invitation_v2_import_map','invitation_v2_legacy_documents'):
        assert all(re.fullmatch('[0-9a-f]{64}',row['source_id']) for row in database.all('SELECT source_id FROM '+table))
    assert states[source.records[0].sid]=='running';assert states[source.records[1].sid]=='failed'
    assert {r['id'] for r in database.all('SELECT id FROM invitation_v2_sessions')}=={r.sid for r in source.records}
    # Exercise the real session authorization method without starting/binding the
    # invitation engine or opening its source files.
    from smsinvit.invitation.service import InvitationService
    tokens=TokenService(JWT);service=object.__new__(InvitationService);service.db=database;service.s=SimpleNamespace(tokens=tokens)
    for record in source.records:
        cap=hmac.new(JWT.encode(),('invocation-session:'+record.sid).encode(),hashlib.sha256).hexdigest()
        assert service._session({'id':record.sid,'sessionSecret':cap})==(record.sid,record.code.upper())
        with pytest.raises(Error) as failure:service._session({'id':record.old_sid,'sessionSecret':tokens.issue('invocation-session',record.old_sid)})
        assert failure.value.code=='SESSION_SUPERSEDED'
        for sid,cap in ((record.sid,tokens.issue('sms-session',record.sid)),(str(uuid4()),cap)):
            with pytest.raises(Error) as failure:service._session({'id':sid,'sessionSecret':cap})
            assert failure.value.code=='SESSION_SECRET_INVALID'
        assert database.scalar('SELECT is_current FROM invitation_runs WHERE id=?',record.old_sid)==0
    assert database.scalar("SELECT COUNT(*) FROM invitation_v2_legacy_documents WHERE source_table='smsinvit.invitation_runs'")==8

def test_original_crypto_password_expiry_inventory_and_bound_mail_survive(database,tmp_path):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source.sqlite3');old=database.one('SELECT * FROM admin_users WHERE id=?',source.user)
    assert not run_import(database,snapshot,source.sku,resolve='codex_invit').conflicts
    user=database.one('SELECT * FROM admin_users WHERE id=?',source.user)
    assert user==old and user['password_hash']==source.password and user['totp_secret_encrypted']==source.totp
    assert CODEC.verify_password(user['password_hash'],'old-admin-fixture-password');assert CODEC.decrypt(user['totp_secret_encrypted'])=='JBSWY3DPEHPK3PXP'
    wire='AAECAwQFBgcICQoL.rOo+mK5vI3t4kWyYRvixrQ==.oZVx/mpjK4koYfTk3JkZGeq061HQCDofSgKRpe32lCI='
    assert CODEC.decrypt(wire)=='旧密钥 compatible secret 🔐'
    for record in source.records:
        old_key=database.one('SELECT * FROM invitation_redeem_keys WHERE id=?',record.kid);new_key=database.one('SELECT * FROM invitation_v2_platform_keys WHERE inventory_item_id=?',record.iid)
        assert new_key['expires_at']==old_key['expires_at'];assert new_key['code_fingerprint']==old_key['code_fingerprint']
        assert CODEC.decrypt(database.scalar('SELECT secret_ciphertext FROM inventory_items WHERE id=?',record.iid))==record.code
        account=database.one('SELECT * FROM invitation_v2_outlook_accounts WHERE email=?',record.email)
        assert mysql.revealed(account['password'])=='private-mail-fixture';assert mysql.revealed(account['cdk_code'])==record.code.upper()

def test_changed_non_setting_source_and_unique_collision_do_not_silently_overwrite(database,tmp_path):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source.sqlite3');assert not run_import(database,snapshot,source.sku,resolve='codex_invit').conflicts
    before=native_counts(database)
    with sqlite3.connect(snapshot) as con:con.execute("UPDATE outlook_accounts SET password='modified-private-password' WHERE email='native@example.test'")
    imp=run_import(database,snapshot,source.sku,resolve='codex_invit')
    assert any(r['table']=='outlook_accounts' and r['reason']=='SOURCE_CHANGED' for r in imp.conflicts);assert native_counts(database)==before
    assert not any('modified-private-password' in json.dumps(r) for r in imp.conflicts)
    imp=Importer(database,CODEC)
    try:
        with database.transaction():
            imp.put('codex_invit','proxy_pool',1,{'id':1,'url':'http://fixture-proxy.example:8080'})
            imp.put('codex_invit','proxy_pool',2,{'id':2,'url':'http://fixture-proxy.example:8080'})
            assert imp.conflicts[0]['reason']=='UNIQUE_CONSTRAINT';raise Conflict()
    except Conflict:pass
    assert database.scalar('SELECT COUNT(*) FROM invitation_v2_proxy_pool')==0

def test_changed_setting_needs_explicit_choice_even_after_successful_import(database,tmp_path):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source.sqlite3');assert not run_import(database,snapshot,source.sku,resolve='codex_invit').conflicts
    with sqlite3.connect(snapshot) as con:con.execute("UPDATE settings SET value='2' WHERE key='cdk_max_concurrency'")
    blocked=run_import(database,snapshot,source.sku)
    assert blocked.conflicts[0]['setting']=='cdk_max_concurrency' and blocked.conflicts[0]['reason']=='SOURCE_CHANGED'
    assert mysql.revealed(database.scalar("SELECT value FROM invitation_v2_settings WHERE `key`='cdk_max_concurrency'"))=='3'
    resolved=run_import(database,snapshot,source.sku,resolve='codex_invit')
    assert not resolved.conflicts and resolved.resolutions[0]['action']=='REPLACED'
    assert mysql.revealed(database.scalar("SELECT value FROM invitation_v2_settings WHERE `key`='cdk_max_concurrency'"))=='2'

def test_case_collision_and_legacy_session_mapping_collision_block_switch(database,tmp_path):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source.sqlite3')
    with sqlite3.connect(snapshot) as con:con.execute('INSERT INTO cdk_codes VALUES(?,?,?,?)',(source.records[0].code.lower(),'unused',None,1700000000.0))
    imp=run_import(database,snapshot,source.sku,resolve='codex_invit')
    assert any(r['reason']=='CASE_OR_INVENTORY_COLLISION' for r in imp.conflicts);assert not any(native_counts(database).values())
    insert(database,'invitation_v2_sessions',{'id':source.records[0].sid,'code_id':'another-code'})
    imp=run_import(database,None,source.sku)
    assert any(r['reason']=='SESSION_MAPPING_COLLISION' for r in imp.conflicts)
    assert database.scalar('SELECT code_id FROM invitation_v2_sessions WHERE id=?',source.records[0].sid)=='another-code'

def test_snapshot_is_read_only_and_manifest_is_checked(database,tmp_path):
    source=legacy_fixture(database);snapshot=sqlite_fixture(tmp_path/'source ?# file.sqlite3')
    stat=snapshot.stat();digest=hashlib.sha256(snapshot.read_bytes()).hexdigest()
    (tmp_path/'manifest.json').write_text(json.dumps({'snapshot':snapshot.name,'sha256':digest,'sourceUnchangedDuringCopy':True}))
    assert not run_import(database,snapshot,source.sku,resolve='codex_invit').conflicts
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest()==digest and snapshot.stat().st_mtime_ns==stat.st_mtime_ns
    assert not Path(str(snapshot)+'-wal').exists() and not Path(str(snapshot)+'-shm').exists()
    (tmp_path/'manifest.json').write_text(json.dumps({'snapshot':snapshot.name,'sha256':'incorrect','sourceUnchangedDuringCopy':True}))
    with pytest.raises(MaintenanceError):Importer(database,CODEC).sqlite(snapshot,source.sku)
    with pytest.raises(MaintenanceError):Importer(database,CODEC).sqlite('/srv/workspace/codex_invit/data/accounts.sqlite3',source.sku)

def clone_restored_source(target,source_database):
    """Cross-schema SELECT source / INSERT target, on the dedicated test server."""
    target_name=target.scalar('SELECT DATABASE()')
    assert re.fullmatch('smsinvit_inv_migration_[0-9a-f]{10}',target_name)
    assert re.fullmatch('[A-Za-z0-9_$-]{1,64}',source_database) and source_database!=target_name
    with target.engine.begin() as connection:
        connection.exec_driver_sql('SET FOREIGN_KEY_CHECKS=0')
        try:
            tables=[r[0] for r in connection.exec_driver_sql('SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA=%s AND TABLE_TYPE=%s',(source_database,'BASE TABLE'))]
            target_tables={r[0] for r in connection.exec_driver_sql('SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA=%s',(target_name,))}
            for table in tables:
                if table not in target_tables:continue
                assert re.fullmatch('[A-Za-z0-9_]+',table)
                columns=[r[0] for r in connection.exec_driver_sql('SELECT COLUMN_NAME FROM information_schema.columns WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND EXTRA NOT LIKE %s AND EXTRA NOT LIKE %s ORDER BY ORDINAL_POSITION',(source_database,table,'%STORED GENERATED%','%VIRTUAL GENERATED%'))]
                names=','.join('`'+c+'`' for c in columns)
                connection.exec_driver_sql('DELETE FROM `'+target_name+'`.`'+table+'`')
                connection.exec_driver_sql('INSERT INTO `'+target_name+'`.`'+table+'`('+names+') SELECT '+names+' FROM `'+source_database+'`.`'+table+'`')
        finally:connection.exec_driver_sql('SET FOREIGN_KEY_CHECKS=1')

@pytest.mark.parametrize('preferred',['codex_invit','smsinvit_legacy'])
def test_read_only_real_copies_conflict_rollback_explicit_apply_repeat_and_unknown_holds(database,preferred):
    directory=os.environ.get('SMSINVIT_INVITATION_REAL_COPY_DIR')
    if not directory:pytest.skip('Read-only real-source copy verification not enabled')
    directory=Path(directory);connection=json.loads((directory/'current-target-credentials.json').read_text())
    assert connection['host']=='127.0.0.1' and connection['port']==13308
    keys=json.loads((directory/'current-keys.json').read_text());codec=SecretCodec(keys['inventory_encryption_key'],keys['inventory_fingerprint_key'])
    sku=json.loads((directory/'import-sku.json').read_text())['id'];snapshot=directory/'codex-source-snapshot/snapshot.sqlite3'
    digest=hashlib.sha256(snapshot.read_bytes()).hexdigest();mtime=snapshot.stat().st_mtime_ns
    clone_restored_source(database,connection['database'])
    before=native_counts(database);legacy_runs=database.all('SELECT id,status,is_current,retry_safe,attempt FROM invitation_runs ORDER BY id')
    blocked=run_import(database,snapshot,sku,codec=codec)
    assert len(blocked.conflicts)==9 and all(r['table']=='settings' and r.get('setting') for r in blocked.conflicts)
    assert not blocked.resolutions and native_counts(database)==before
    applied=run_import(database,snapshot,sku,codec=codec,resolve=preferred)
    assert not applied.conflicts and len(applied.resolutions)==9
    assert all(r['selectedSource']==preferred for r in applied.resolutions)
    assert database.all('SELECT id,status,is_current,retry_safe,attempt FROM invitation_runs ORDER BY id')==legacy_runs
    counts=native_counts(database);repeat=run_import(database,snapshot,sku,codec=codec,resolve=preferred)
    assert not repeat.conflicts and not repeat.resolutions and native_counts(database)==counts
    assert repeat.counts['skipped']>300
    unresolved=database.all('SELECT j.status,h.original_status,h.reason,h.resolved_at FROM invitation_v2_task_holds h JOIN invitation_v2_cdk_jobs j ON j.job_id=h.job_id')
    assert all(r['resolved_at'] is None and r['status']==r['original_status'].lower() for r in unresolved)
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest()==digest and snapshot.stat().st_mtime_ns==mtime
    evidence_path=os.environ.get('SMSINVIT_INVITATION_MIGRATION_EVIDENCE')
    if evidence_path:
        path=Path(evidence_path);existing=json.loads(path.read_text()) if path.exists() else {'format':'smsinvit-invitation-migration-v1','syntheticScenarios':10,'originalDesktopUntouched':True,'realSourceReadOnly':True,'runs':[]}
        existing['runs'].append({'choice':preferred,'settingConflicts':9,'resolvedSettings':9,'allOrNothingRollback':True,'idempotentRepeat':True,'legacyRunsUnchanged':len(legacy_runs),'heldUncertainJobs':len(unresolved),'nativeRows':counts,'sourceSnapshotUnchanged':True,'status':'PASS'})
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(existing,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)

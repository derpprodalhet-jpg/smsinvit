#!/usr/bin/env python3
"""Idempotent two-source invitation import. Run on a restored target, never a live source."""
from __future__ import annotations
import argparse,hashlib,hmac,json,os,sqlite3,sys,time
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL,uuid5
from urllib.parse import quote
from sqlalchemy.exc import IntegrityError
from database_tools import ROOT,MaintenanceError,arguments,credentials,maintenance_lock,private_json
from smsinvit.security import SecretCodec
from smsinvit.invitation import mysql

class Conflict(Exception):pass
class DryRun(Exception):pass

def canonical(value):
    if isinstance(value,datetime):return value.isoformat(timespec='milliseconds')
    if isinstance(value,bytes):return value.hex()
    return str(value)
def checksum(row):return hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False,default=canonical,separators=(',',':')).encode()).hexdigest()
def timestamp(value):
    if isinstance(value,datetime):return value.replace(tzinfo=timezone.utc).timestamp()
    return value or time.time()

class Importer:
    def __init__(self,db,codec,resolve_settings=None):
        if resolve_settings not in (None,'codex_invit','smsinvit_legacy'):raise MaintenanceError('Unknown settings source selection.')
        self.db=db;self.codec=codec;self.resolve_settings=resolve_settings;self.conflicts=[];self.resolutions=[];self.counts={};mysql.configure(SimpleNamespace(db=db,codec=codec))
    def identity(self,value):return hmac.new(self.codec.fingerprint_key,str(value).encode(),hashlib.sha256).hexdigest()
    def archive(self,table,key,row):
        self.db.execute('INSERT IGNORE INTO invitation_v2_legacy_documents(source_table,source_id,ciphertext) VALUES (?,?,?)',table,self.identity(key),self.codec.encrypt(json.dumps(row,ensure_ascii=False,default=canonical)))
    def conflict(self,source,table,source_id,reason,row,**extra):
        item={'source':source,'table':table,'identity':source_id,'reason':reason,**extra}
        if table=='settings':item['setting']=row.get('key')
        self.conflicts.append(item)
    def resolve_setting(self,source,source_id,row,existing,reason):
        if not self.resolve_settings:return False
        setting=row['key'];preferred=self.resolve_settings;replace=source==preferred
        resolution={'source':source,'table':'settings','identity':source_id,'setting':setting,'reason':reason,'selectedSource':preferred,'action':'REPLACED' if replace else 'KEPT'}
        # Preserve both choices and the exact decision in encrypted storage.
        # Reports contain key names and actions only, never settings values.
        before={k:mysql.revealed(v) for k,v in (existing or {}).items()}
        self.archive('resolution.settings',[source,setting,reason,checksum(row),checksum(existing or {})],{**resolution,'previous':before,'incoming':row})
        if replace:self.db.execute('UPDATE invitation_v2_settings SET value=? WHERE `key`=?',mysql.protected(row.get('value')),setting)
        self.resolutions.append(resolution);self.counts['settings_resolved']=self.counts.get('settings_resolved',0)+1
        return True
    def put(self,source,table,source_id,row):
        if source not in ('codex_invit','smsinvit_legacy') or table not in mysql.TABLES:raise MaintenanceError('Unsupported invitation import source/table.')
        source_id=self.identity(source_id);digest=checksum(row);target='invitation_v2_'+table
        prior=self.db.one('SELECT * FROM invitation_v2_import_map WHERE source_name=? AND source_table=? AND source_id=?',source,table,source_id)
        if prior:
            if prior['source_checksum']!=digest:
                existing=self.db.one('SELECT * FROM invitation_v2_settings WHERE `key`=?',row['key']) if table=='settings' else None
                if table=='settings' and existing and self.resolve_setting(source,source_id,row,existing,'SOURCE_CHANGED'):
                    self.db.execute('UPDATE invitation_v2_import_map SET source_checksum=? WHERE source_name=? AND source_table=? AND source_id=?',digest,source,table,source_id)
                else:self.conflict(source,table,source_id,'SOURCE_CHANGED',row)
            else:self.counts['skipped']=self.counts.get('skipped',0)+1
            return
        info=self.db.all('SELECT COLUMN_NAME,COLUMN_KEY,EXTRA FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=? ORDER BY ORDINAL_POSITION',target)
        allowed={r['COLUMN_NAME'] for r in info if not any(kind in r['EXTRA'] for kind in ('STORED GENERATED','VIRTUAL GENERATED'))}
        unknown=set(row)-allowed
        if unknown:
            self.conflict(source,table,source_id,'UNSUPPORTED_COLUMNS',row,columns=sorted(unknown));return
        pk=[r['COLUMN_NAME'] for r in info if r['COLUMN_KEY']=='PRI']
        # Upstream job/run identifiers can embed the raw card code. Protect all
        # primary identifiers and references consistently, including task holds.
        private_fields=mysql.SECRET_FIELDS|{'job_id','run_id'}
        transformed={k:mysql.protected(v) if k in private_fields else v for k,v in row.items()}
        condition=' AND '.join('`'+k+'`=?' for k in pk)
        existing=self.db.one('SELECT * FROM '+target+' WHERE '+condition,*[transformed[k] for k in pk])
        if existing and any(existing.get(k)!=v for k,v in transformed.items()):
            if table!='settings' or not self.resolve_setting(source,source_id,row,existing,'TARGET_DIFFERS'):
                self.conflict(source,table,source_id,'TARGET_DIFFERS',row);return
        if not existing:
            columns=list(transformed)
            try:self.db.execute('INSERT INTO '+target+'('+','.join('`'+k+'`' for k in columns)+') VALUES ('+','.join('?' for _ in columns)+')',*[transformed[k] for k in columns])
            except IntegrityError:self.conflict(source,table,source_id,'UNIQUE_CONSTRAINT',row);return
        self.db.execute('INSERT INTO invitation_v2_import_map(source_name,source_table,source_id,target_id,source_checksum) VALUES (?,?,?,?,?)',source,table,source_id,self.identity([transformed[k] for k in pk]),digest)
        self.counts[table]=self.counts.get(table,0)+1
    def ledger(self,code,sku,item_id=None,expires=None,attrs=None,inventory_status='ISSUED'):
        normalized=code.strip().upper();marker=mysql.marker(normalized);fingerprint=self.codec.fingerprint(code)
        prior=self.db.one('SELECT * FROM invitation_v2_platform_keys WHERE code_id=? OR code_fingerprint=?',marker,fingerprint)
        if prior:
            if prior['code_id']!=marker or prior['code_fingerprint']!=fingerprint or prior['inventory_item_id']!=item_id and item_id:self.conflicts.append({'table':'platform_keys','identity':self.identity(code),'reason':'CASE_OR_INVENTORY_COLLISION'})
            return
        planned_item=item_id or str(uuid5(NAMESPACE_URL,'smsinvit:codex-card:'+normalized))
        self.db.execute("INSERT INTO redeem_cdk_registry(business_kind,code_fingerprint,owner_inventory_id) VALUES('CODEX_INVITE_REDEEM',?,?) ON DUPLICATE KEY UPDATE code_fingerprint=VALUES(code_fingerprint)",fingerprint,planned_item)
        reservation=self.db.one("SELECT owner_inventory_id,inventory_count FROM redeem_cdk_registry WHERE business_kind='CODEX_INVITE_REDEEM' AND code_fingerprint=? FOR UPDATE",fingerprint)
        if reservation['owner_inventory_id']!=planned_item or reservation['inventory_count']!=1:
            self.conflicts.append({'table':'platform_keys','identity':self.identity(code),'reason':'CDK_REGISTRY_COLLISION'});return
        if not item_id:
            item_id=planned_item
            try:self.db.execute("INSERT INTO inventory_items(id,sku_id,status,secret_ciphertext,secret_fingerprint,issued_at,updated_at) VALUES (?,?,?,?,?,UTC_TIMESTAMP(3),UTC_TIMESTAMP(3))",item_id,sku,inventory_status,self.codec.encrypt(code),fingerprint)
            except IntegrityError:
                self.conflicts.append({'table':'platform_keys','identity':self.identity(code),'reason':'INVENTORY_COLLISION'});return
            self.db.execute("INSERT INTO inventory_events(id,inventory_item_id,event_type,actor_type,metadata_json) VALUES (?,?,?,'SYSTEM',?)",str(uuid5(NAMESPACE_URL,'smsinvit:codex-import-event:'+normalized)),item_id,'ISSUED',json.dumps({'source':'codex_invit'}))
        self.db.execute('INSERT INTO invitation_v2_platform_keys(inventory_item_id,sku_id,code_id,code_fingerprint,expires_at,snapshot_json) VALUES (?,?,?,?,?,?)',item_id,sku,marker,fingerprint,expires,json.dumps(attrs or {'productKind':'CODEX_INVITE_REDEEM'}))
    def sqlite(self,snapshot,sku):
        # Snapshot must be a separate consolidated file, never the original DB.
        path=Path(snapshot).resolve()
        original=Path(os.environ.get('SMSINVIT_ORIGINAL_SOURCE_ROOT') or '/srv/workspace/codex_invit')
        if path.is_relative_to(original):raise MaintenanceError('Use sqlite_snapshot.py first; importing the original source is forbidden.')
        manifest=path.parent/'manifest.json'
        if manifest.exists():
            metadata=json.loads(manifest.read_text())
            if metadata.get('snapshot')!=path.name or metadata.get('sha256')!=hashlib.sha256(path.read_bytes()).hexdigest() or not metadata.get('sourceUnchangedDuringCopy'):raise MaintenanceError('The consolidated SQLite snapshot manifest does not match.')
        with sqlite3.connect('file:'+quote(str(path),safe='/')+'?mode=ro&immutable=1',uri=True) as con:
            con.row_factory=sqlite3.Row
            tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in mysql.TABLES:
                if table not in tables:continue
                pks=[r[1] for r in con.execute('PRAGMA table_info("'+table+'")') if r[5]]
                for raw in con.execute('SELECT * FROM "'+table+'"'):
                    row=dict(raw);key=[row[k] for k in pks];self.archive('codex.'+table,key,row)
                    if table=='settings' and row['key'] in ('codex_home_root','codex_binary'):
                        # Host executable paths are not portable authority. Original
                        # values remain encrypted in the archive for audit/recovery.
                        row['value']='' if row['key']=='codex_home_root' else 'codex'
                    if table=='runs':row['log_path']='' # source logs are copied separately under the managed log root
                    if table=='cdk_codes':
                        row['code']=str(row['code']).strip().upper()
                        self.ledger(row['code'],sku,inventory_status='INVALID' if row.get('status')=='void' else 'ISSUED')
                    self.put('codex_invit',table,key,row)
                    if table=='cdk_jobs' and row.get('status') in ('queued','running','review'):
                        self.db.execute("INSERT IGNORE INTO invitation_v2_task_holds(job_id,original_status,reason) VALUES (?,?,'NATIVE_UNCERTAIN')",mysql.protected(row['job_id']),row['status'])
                        self.counts['native_task_holds']=self.counts.get('native_task_holds',0)+1
    def legacy(self):
        settings=self.db.one('SELECT settings_ciphertext FROM invitation_settings WHERE id=1')
        if settings:
            raw=json.loads(self.codec.decrypt(settings['settings_ciphertext']));self.archive('smsinvit.settings','1',raw)
            rename={'concurrency':'cdk_max_concurrency','invite_timeout':'invite_wait_timeout','otp_timeout':'invite_otp_timeout','codex_oauth_attempts':'invite_max_attempts','want_2fa':'invite_want_2fa','want_sms':'sms_enabled','sms_api_key':'sms_api_key_smsbower','say_hi_prompt':'sayhi_prompt','say_hi_model':'sayhi_model','cf_admin_password':'cf_admin_token'}
            for key,value in raw.items():
                target=rename.get(key,key)
                if isinstance(value,(dict,list)):value=json.dumps(value,ensure_ascii=False)
                elif isinstance(value,bool):value='1' if value else '0'
                self.put('smsinvit_legacy','settings',key,{'key':target,'value':str(value)})
        accounts={r['id']:r for r in self.db.all('SELECT * FROM invitation_mail_accounts')}
        for key,raw in accounts.items():
            self.archive('smsinvit.mail_accounts',key,raw);secret=json.loads(self.codec.decrypt(raw['credentials_ciphertext']))
            kind=raw['kind'].lower();client=secret.get('client_id','')
            if kind=='imap':client=json.dumps({'host':secret.get('host',''),'port':secret.get('port',993)})
            row={'email':raw['email'].lower(),'password':secret.get('password',''),'client_id':client,'refresh_token':secret.get('refresh_token',''),'relay_url':secret.get('relay_url',''),'kind':kind,'status':{'AVAILABLE':'available','CLAIMED':'in_use','DONE':'done','DISABLED':'failed'}[raw['status']],'imported_at':timestamp(raw['created_at'])}
            self.put('smsinvit_legacy','outlook_accounts',key,row)
        for raw in self.db.all('SELECT * FROM invitation_proxies'):
            self.archive('smsinvit.proxies',raw['id'],raw)
            url=self.codec.decrypt(raw['url_ciphertext'])
            # Use deterministic disjoint integer identifiers for legacy proxies.
            id=int(self.identity('proxy:'+raw['id'])[:13],16)
            self.put('smsinvit_legacy','proxy_pool',raw['id'],{'id':id,'url':url,'label':raw['note'],'enabled':int(raw['enabled']),'added_at':timestamp(raw['created_at'])})
        for raw in self.db.all('SELECT k.*,i.secret_ciphertext FROM invitation_redeem_keys k JOIN inventory_items i ON i.id=k.inventory_item_id'):
            self.archive('smsinvit.keys',raw['id'],raw);code=self.codec.decrypt(raw['secret_ciphertext']);canonical_code=code.strip().upper()
            account=accounts.get(raw['claimed_mail_account_id']);status={'UNUSED':'unused','CLAIMED':'bound','CONSUMED':'done','DISABLED':'void','EXPIRED':'void'}[raw['status']]
            attrs=raw['attributes_snapshot_json'];attrs=json.loads(attrs) if isinstance(attrs,str) else attrs
            self.ledger(code,raw['sku_id'],raw['inventory_item_id'],raw['expires_at'],attrs)
            row={'code':canonical_code,'status':status,'email':account['email'].lower() if account else None,'mail_kind':account['kind'].lower() if account else None,'created_at':timestamp(raw['created_at'])}
            current=self.db.one('SELECT * FROM invitation_runs WHERE redeem_key_id=? AND is_current=1',raw['id'])
            if current:
                oldstate=current['status'];row['status']={'PREVIEWED':'bound','QUEUED':'queued','RUNNING':'running','REVIEW':'review','SUCCEEDED':'done','FAILED':'failed'}[oldstate]
                marker=mysql.marker(canonical_code)
                existing_session=self.db.one('SELECT id,code_id FROM invitation_v2_sessions WHERE id=? OR code_id=?',current['id'],marker)
                if existing_session and (existing_session['id']!=current['id'] or existing_session['code_id']!=marker):
                    self.conflict('smsinvit_legacy','sessions',self.identity(current['id']),'SESSION_MAPPING_COLLISION',{})
                elif not existing_session:self.db.execute("INSERT INTO invitation_v2_sessions(id,code_id,origin) VALUES (?,?,'legacy')",current['id'],marker)
                self.counts['legacy_current_sessions']=self.counts.get('legacy_current_sessions',0)+1
                if oldstate in ('QUEUED','RUNNING','REVIEW') or oldstate=='FAILED' and not current['retry_safe']:
                    self.put('smsinvit_legacy','cdk_jobs',current['id'],{'job_id':current['id'],'code':canonical_code,'email':current['email'],'status':oldstate.lower(),'created_at':timestamp(current['created_at']),'attempt':current['attempt']})
                    self.db.execute("INSERT IGNORE INTO invitation_v2_task_holds(job_id,original_status,reason) VALUES (?,?,'LEGACY_UNCERTAIN')",mysql.protected(current['id']),oldstate)
                credentials=current.get('account_credentials_ciphertext')
                if credentials:
                    secret=json.loads(self.codec.decrypt(credentials));secret['email']=current['email'];registered={k:v for k,v in secret.items() if k in ('email','password','access_token','refresh_token','session_token','id_token','cookie_header','csrf_token','totp_secret','device_id')}
                    self.put('smsinvit_legacy','registered',current['id'],registered)
            self.put('smsinvit_legacy','cdk_codes',raw['id'],row)
            if account:
                self.db.execute('UPDATE invitation_v2_outlook_accounts SET cdk_code=? WHERE email=?',mysql.marker(canonical_code),account['email'].lower())
        for table in ('invitation_runs','invitation_run_events','invitation_verifications'):
            for row in self.db.all('SELECT * FROM '+table):
                self.archive('smsinvit.'+table,row['id'],row)
                if table=='invitation_runs' and not row['is_current']:self.counts['legacy_superseded_history']=self.counts.get('legacy_superseded_history',0)+1

def main():
    parser=argparse.ArgumentParser(description=__doc__);arguments(parser)
    parser.add_argument('--keys-file',type=Path,required=True);parser.add_argument('--sqlite-snapshot',type=Path);parser.add_argument('--native-sku-id');parser.add_argument('--legacy',action='store_true');parser.add_argument('--apply',action='store_true');parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--resolve-settings',choices=['codex_invit','smsinvit_legacy'],help='Explicitly select the winning source for conflicting invitation settings; both values and the decision are archived encrypted.')
    args=parser.parse_args()
    if not args.legacy and not args.sqlite_snapshot:parser.error('Select an invitation source.')
    try:
        if args.keys_file.stat().st_mode&0o077:raise MaintenanceError('Key file requires 0600 permissions.')
        keys=json.loads(args.keys_file.read_text());codec=SecretCodec(keys['inventory_encryption_key'],keys['inventory_fingerprint_key']);connection=credentials(args);db=connection.connect();importer=Importer(db,codec,args.resolve_settings)
        with maintenance_lock(db,connection.database):
            try:
                with db.transaction():
                    if args.legacy:importer.legacy()
                    if args.sqlite_snapshot:
                        if not args.native_sku_id or not db.one('SELECT id FROM skus WHERE id=?',args.native_sku_id):raise MaintenanceError('An existing invitation SKU is required for native source cards.')
                        importer.sqlite(args.sqlite_snapshot,args.native_sku_id)
                    if importer.conflicts:raise Conflict()
                    if not args.apply:raise DryRun()
            except DryRun:pass
            except Conflict:pass
        report={'applied':bool(args.apply and not importer.conflicts),'switchAllowed':not importer.conflicts,'counts':importer.counts,'conflicts':importer.conflicts,'resolutions':importer.resolutions,'uncertainTasksNotReplayed':True}
        private_json(args.report,report);print(json.dumps({k:v for k,v in report.items() if k!='conflicts'}))
        if importer.conflicts:return 2
    except (MaintenanceError,KeyError,ValueError) as exc:
        print('Invitation migration refused: '+(str(exc) if isinstance(exc,MaintenanceError) else type(exc).__name__),file=sys.stderr);return 1
    except Exception as exc:
        print('Invitation migration refused: '+type(exc).__name__+'; no sensitive driver output',file=sys.stderr);return 1
    return 0
if __name__=='__main__':sys.exit(main())

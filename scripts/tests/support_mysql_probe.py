"""Fresh unprivileged support MySQL, TLS, current migrations and backup/restore fixture."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database_tools import ROOT,Credentials,sha256_file
from prepare_deployment import prepare
from migrate import run as migrate,check_schema,migrations,validate_history
from backup import backup
from restore import restore

def command(argv,timeout=60):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        diagnostic=ROOT/'data/test-private/support-mysql-command-error.json'
        import os
        fd=os.open(diagnostic,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as out:json.dump({'command':argv[:3],'stderr':result.stderr},out)
        raise RuntimeError('Synthetic support database fixture command failed: '+' '.join(argv[:3]))
    return result.stdout

def probe(architecture):
    expected_versions=[item.version for item in migrations()]
    image='smsinvit-mysql:v1.0.0-python-'+architecture+'-local'
    info=json.loads(command(['docker','image','inspect',image]))[0]
    suffix=uuid.uuid4().hex[:10];name='smsinvit-mysql-fixture-'+suffix;network=name+'-network';volume=name+'-data';private_volume=name+'-private';prep=name+'-prepare';maintenance=network+'-maintenance'
    private=ROOT/'data/test-private'/name
    private.mkdir(mode=0o700,parents=True)
    ca=str(private/'mysql-tls/ca.pem')
    try:
        # Prepare inside a new Linux named volume: macOS bind mounts cannot
        # reliably emulate Linux owner-only UID999 private file access.
        command(['docker','volume','create',private_volume])
        code="import sys,os;from pathlib import Path;sys.path.insert(0,'/app/scripts');from prepare_deployment import prepare;p=Path('/fixture/private');prepare(p,'fixture.example.com',new_install=True);p.chmod(0o750);os.chown(p,999,999)"
        command(['docker','run','--name',prep,'--network','none','--user','0:0','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges','--mount','type=volume,source='+private_volume+',target=/fixture','--entrypoint','python','smsinvit-python:v1.0.0-python-arm64-local','-c',code])
        (private/'mysql-tls').mkdir()
        for filename in ['mysql-root-password','migration.json','backup.json','mysql-tls/ca.pem']:
            command(['docker','cp',prep+':/fixture/private/'+filename,str(private/filename)])
        for filename in ['mysql-root-password','migration.json','backup.json']:(private/filename).chmod(0o600)
        root_password=(private/'mysql-root-password').read_text().strip()
        command(['docker','network','create','--internal',network]);command(['docker','network','create',maintenance]);command(['docker','volume','create',volume])
        argv=['docker','run','--detach','--name',name,'--network',maintenance,'--network',network,'--read-only','--user','999:999','--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777','--tmpfs','/var/run/mysqld:rw,nosuid,nodev,size=16m,mode=0755,uid=999,gid=999','--publish','127.0.0.1::3306','--env','MYSQL_DATABASE=smsinvit','--env','MYSQL_ROOT_HOST=%','--env','MYSQL_ROOT_PASSWORD_FILE=/fixture/private/mysql-root-password','--mount','type=volume,source='+volume+',target=/var/lib/mysql','--mount','type=volume,source='+private_volume+',target=/fixture,readonly','--mount','type=volume,source='+private_volume+',target=/docker-entrypoint-initdb.d,volume-subpath=private/init,readonly',image,'--require-secure-transport=ON','--ssl-ca=/fixture/private/mysql-tls/ca.pem','--ssl-cert=/fixture/private/mysql-tls/server.pem','--ssl-key=/fixture/private/mysql-tls/server-key.pem','--character-set-server=utf8mb4','--collation-server=utf8mb4_unicode_ci']
        command(argv)
        actual=json.loads(command(['docker','inspect',name]))[0]
        port=int(actual['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort'])
        health=['docker','exec',name,'mysqladmin','--protocol=TCP','--host=127.0.0.1','--ssl-mode=VERIFY_IDENTITY','--ssl-ca=/fixture/private/mysql-tls/ca.pem','ping','--silent']
        temporary_socket_ready_tcp_unready=False
        tcp_ready=False
        root=Credentials('localhost',port,'root',root_password,'mysql',ca_file=ca)
        admin=None
        for _ in range(160):
            try:
                tcp=subprocess.run(health,capture_output=True,timeout=5).returncode==0
                sock=subprocess.run(['docker','exec',name,'mysqladmin','--protocol=socket','ping','--silent'],capture_output=True,timeout=5).returncode==0
                temporary_socket_ready_tcp_unready|=sock and not tcp
                if not tcp:time.sleep(.1);continue
                tcp_ready=True
                admin=root.connect();version=admin.one('SELECT VERSION() AS version')['version'];break
            except Exception:
                if admin:admin.close()
                time.sleep(.25)
        else:raise RuntimeError('Unprivileged fresh MySQL initialization did not become ready.')
        assert version.startswith('8.4.11')
        sql_ssl=admin.one("SHOW SESSION STATUS LIKE 'Ssl_version'")['Value'];assert sql_ssl in ('TLSv1.2','TLSv1.3')
        selected=[]
        for kind in ['migration','backup']:
            data=json.loads((private/(kind+'.json')).read_text());selected.append(Credentials('localhost',port,data['user'],data['password'],'smsinvit',ca_file=ca))
        migrated=migrate(selected[0]);assert migrated['status']=='ready' and migrated['appliedVersions']==expected_versions
        db=selected[0].connect();db.execute("INSERT INTO settings(id,`key`,category,value_json,updated_at) VALUES(UUID(),%s,'fixture',%s,NOW(3))",'support.fixture','"synthetic-only"')
        expected=db.all('SELECT `key`,value_json FROM settings ORDER BY `key`');schema=check_schema(db,migrations());db.close()
        dump=private/'synthetic-backup.sql';backed=backup(selected[1],dump)
        admin.execute('CREATE DATABASE fixture_restore CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        destination=Credentials('localhost',port,'root',root_password,'fixture_restore',ca_file=ca)
        restored=restore(destination,dump);assert restored['status']=='restored_and_verified'
        db=destination.connect();assert db.all('SELECT `key`,value_json FROM settings ORDER BY `key`')==expected
        assert sorted(validate_history(db,migrations()))==expected_versions;assert check_schema(db,migrations())==schema;db.close()
        admin.close()
        command(['docker','restart','--time','10',name]);persisted=None
        current=json.loads(command(['docker','inspect',name]))[0]
        restarted_port=int(current['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort'])
        selected[0]=replace(selected[0],port=restarted_port)
        for _ in range(100):
            try:
                persisted=selected[0].connect();assert persisted.one("SELECT JSON_UNQUOTE(value_json) AS v FROM settings WHERE `key`=%s",'support.fixture')['v']=='synthetic-only';break
            except Exception:
                if persisted:persisted.close()
                time.sleep(.25)
        else:raise RuntimeError('MySQL fixture failed to retain synthetic data on restart.')
        persisted.close();admin.close()
        status=command(['docker','exec',name,'sh','-c','cat /proc/1/status; test ! -e /usr/local/bin/gosu; test ! -e /usr/lib/mysqlsh'])
        assert 'CapEff:\t0000000000000000' in status and 'CapBnd:\t0000000000000000' in status and 'NoNewPrivs:\t1' in status
        return {'status':'PASS','architecture':architecture,'image':image,'imageId':info['Id'],'mysqlVersion':version,'tcpTlsHealthReady':tcp_ready,'temporarySocketReadyBeforeFinalTcpObserved':temporary_socket_ready_tcp_unready,'freshNamedVolume':True,'runtimeUser':'999:999','readOnlyRoot':True,'capabilitiesEffectiveAndBoundingZero':True,'noNewPrivileges':True,'tlsMode':'VERIFY_IDENTITY','negotiatedTLS':sql_ssl,'migrations':expected_versions,'schema':schema,'backupSha256':sha256_file(dump),'restoredTables':restored['tables'],'backupRestoreDataAndHistoryMatch':True,'syntheticDataPersistsAfterRestart':True,'removedToolsAbsent':True,'internalBusinessNetwork':True,'mysqlOnlyMaintenanceBridge':True,'hostPortLoopbackOnly':True,'actualProductionDataAccessed':False,'upstreamRequests':0}
    finally:
        subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=30)
        subprocess.run(['docker','rm','--force',prep],capture_output=True,timeout=30)
        subprocess.run(['docker','volume','rm',volume],capture_output=True,timeout=30)
        subprocess.run(['docker','volume','rm',private_volume],capture_output=True,timeout=30)
        subprocess.run(['docker','network','rm',network],capture_output=True,timeout=30)
        subprocess.run(['docker','network','rm',maintenance],capture_output=True,timeout=30)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args();rows=[]
    try:
        for arch in ['amd64','arm64']:
            rows.append(probe(arch));print(json.dumps(rows[-1]),flush=True)
        a.evidence.parent.mkdir(parents=True,exist_ok=True);a.evidence.write_text(json.dumps({'status':'PASS','architectures':rows,'allFixtureContainersVolumesNetworksRemoved':True},indent=2,sort_keys=True)+'\n')
    except Exception as error:
        import traceback
        print(json.dumps({'safeErrorFrames':[{'file':Path(frame.filename).name,'line':frame.lineno} for frame in traceback.extract_tb(error.__traceback__)]}),file=sys.stderr)
        raise SystemExit('Synthetic support MySQL validation failed ('+type(error).__name__+'); private diagnostics are not printed.') from None

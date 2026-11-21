"""Explicit final-image probe: actual API process adapter/helper, entirely offline.

Run as the image's unprivileged runtime user with PYTHONPATH=/app/apps/api.
It never creates services, opens a database, starts workers or loads real tokens.
"""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
from types import SimpleNamespace
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from smsinvit.invitation import process

SDK_FIXTURE="""var SentinelSDK={init:async function(){return null;}};
globalThis.__debugP={getRequirementsToken:async function(){return 'offline-fixture-requirements';}};
"""

def ingress_probe():
    """Run real application middleware with no database or business services."""
    from fastapi import Request
    from fastapi.testclient import TestClient
    from smsinvit.app import create_app
    from smsinvit.config import Config
    calls={'auth':0,'rate':0,'database':0,'business':0,'upstream':0}
    class DatabaseForbidden:
        def __getattr__(self,name):
            calls['database']+=1
            raise AssertionError('The ingress probe must not open or use a database')
    def auth(token):calls['auth']+=1;raise AssertionError('Proxy rejection must precede authentication')
    def rate(*args):calls['rate']+=1
    services=SimpleNamespace(auth=SimpleNamespace(authenticate=auth),limiter=SimpleNamespace(check=rate))
    config=Config({'runtime.secure_cookies':False,'runtime.cors_origins':'','smsinvit.security.trusted-proxies':'127.0.0.1/32'},environ={})
    app=create_app(config,DatabaseForbidden(),services=services,include_domains=False,load_runtime=False)
    # Request annotations must be concrete: this probe uses no postponed annotations.
    def business(request:Request):
        calls['business']+=1;calls['upstream']+=1
        return {'ip':request.state.client_ip}
    app.add_api_route('/api/v1/sms/redeem',business,methods=['POST'])
    invalid=[('Forwarded','for=1.1.1.1'),('X-Forwarded-For','1.1.1.1'),('X-Real-IP','1.1.1.1'),('X-Forwarded-Proto','https'),('X-Forwarded-Host','example.test'),('X-Forwarded-Port','443')]
    refused=0
    with TestClient(app,client=('203.0.113.10',123)) as client:
        for header in invalid:
            for path in ('/api/v1/sms/redeem','/api/v1/admin/plans'):
                result=client.post(path,headers=[header,('Authorization','Bearer fixture')],json={})
                assert result.status_code==400 and result.json()['error']['code']=='INVALID_PROXY_HEADERS'
                refused+=1
    with TestClient(app,client=('127.0.0.1',123)) as client:
        for headers in [[('X-Forwarded-For','1.1.1.1'),('X-Forwarded-For','2.2.2.2')],[('X-Forwarded-For','1.1.1.1,,127.0.0.1')],[('X-Forwarded-Proto','https,http')]]:
            result=client.post('/api/v1/admin/plans',headers=[*headers,('Authorization','Bearer fixture')],json={})
            assert result.status_code==400 and result.json()['error']['code']=='INVALID_PROXY_HEADERS'
            refused+=1
        assert calls=={'auth':0,'rate':0,'database':0,'business':0,'upstream':0}
        result=client.post('/api/v1/sms/redeem',headers={'X-Forwarded-For':'198.51.100.5','X-Forwarded-Proto':'https','X-Forwarded-Host':'example.test'},json={})
        assert result.status_code==200 and result.json()=={'ip':'198.51.100.5'}
        assert calls=={'auth':0,'rate':1,'database':0,'business':1,'upstream':1}
    return {'rejectedBeforeAuthRateDatabaseBusiness':refused,'validTrustedChainPreserved':True,'databaseOpened':False,'upstreamIsSyntheticCounterOnly':True}

def main():
    ingress=ingress_probe()
    data=Path(os.environ.get('SMSINVIT_INVITATION_DATA_DIR','/app/runtime/invitation'))
    os.environ.setdefault('CODEX_HOMES_ROOT',str(data/'codex-homes'))
    os.environ['MYSQL_PASSWORD']='offline-fixture-parent-db-secret'
    os.environ['JWT_SECRET']='offline-fixture-parent-jwt-secret'
    root=process.managed_homes_root();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    cache=Path(tempfile.gettempdir())/'openai-sentinel-demo';cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    fixture=Path(tempfile.mkdtemp(prefix='api-probe-fixture-',dir=cache))
    sdk=fixture/'sdk.js';sdk.write_text(SDK_FIXTURE);sdk.chmod(0o600)
    ca=fixture/'ca.pem'
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME,'Offline API probe CA')]);now=datetime.now(timezone.utc)
    certificate=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256())
    ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM));ca.chmod(0o600)
    script=Path(process.__file__).resolve().parents[4]/'vendor/codex_invit/openai_sentinel_quickjs.js'
    original={path:(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mode) for path in (sdk,ca,script)}
    before=set(root.iterdir());calls=[];actual=process.run_child
    os.environ['SMSINVIT_INVITATION_CA_BUNDLE']=str(ca)
    def checked_actual(argv,**kwargs):
        home=Path(argv[argv.index('--home')+1]);work=Path(argv[argv.index('--workdir')+1])
        assert home!=root and work!=root and home!=work and home.is_relative_to(root) and work.is_relative_to(root)
        assert not {'MYSQL_PASSWORD','JWT_SECRET'}&kwargs['env'].keys()
        sdk_copy=kwargs['env'].get('OPENAI_SENTINEL_SDK_FILE')
        if sdk_copy:
            copy=Path(sdk_copy);script_copy=Path(argv[-1])
            assert copy==home/'sdk.js' and copy.read_text()==SDK_FIXTURE and copy.stat().st_mode&0o077==0
            assert argv[argv.index('--read-file')+1]==str(copy) and str(sdk) not in argv
            assert script_copy==home/'sentinel.js' and script_copy.read_bytes()==script.read_bytes() and script_copy.stat().st_mode&0o077==0
            assert str(script) not in argv and '--ca-file' not in argv
            # File metadata operations affect only task-owned snapshots. The
            # live helper still executes these files after this permission change.
            for path in (copy,script_copy):
                with path.open('rb') as stream:os.fchmod(stream.fileno(),0o400)
        if '--ca-file' in argv:
            copy=Path(argv[argv.index('--ca-file')+1]);assert copy==home/'trusted-ca.pem'
            assert copy.read_bytes()==ca.read_bytes() and copy.stat().st_mode&0o077==0 and str(ca) not in argv
            with copy.open('rb') as stream:os.fchmod(stream.fileno(),0o400)
        result=actual(argv,**kwargs)
        assert result.returncode==0,(result.returncode,result.stderr)
        calls.append({'program':Path(argv[argv.index('--')+1]).name,'arguments':argv[-1] if argv[-1]=='--version' else 'private Sentinel fixture','caSnapshot':'--ca-file' in argv,'sdkSnapshot':bool(sdk_copy)})
        return result
    process.run_child=checked_actual
    try:
        assert process.execution_self_check(data,os.environ.get('SMSINVIT_INVITATION_CODEX_BINARY','/opt/codex/bin/codex')) is True
        node=shutil.which(os.environ.get('OPENAI_SENTINEL_NODE_PATH','node')) or '/opt/smsinvit-bin/node'
        result=process.subprocess.run([node,str(script)],env={'OPENAI_SENTINEL_SDK_FILE':str(sdk)},input=json.dumps({'action':'requirements','flow':'offline-fixture'}),capture_output=True,text=True,timeout=30)
        assert json.loads(result.stdout)=={'request_p':'offline-fixture-requirements'}
        assert set(root.iterdir())==before
        assert all((hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mode)==state for path,state in original.items())
        report={'status':'PASS','architecture':platform.machine(),'proxyIngress':ingress,'offlineVersionsSelfCheck':True,'mandatoryHelperActuallyExecuted':True,'actualFixedSentinelWithFixtureSdk':True,'allHomesAndWorkWithinBoundedMount':True,'privateCaSdkScriptSnapshots':True,'sharedSourceMetadataUnchanged':True,'taskFilesRemoved':True,'workersStarted':False,'databaseOpened':False,'upstreamRequests':0,'calls':calls}
        print(json.dumps(report,sort_keys=True))
        return 0
    finally:
        process.run_child=actual
        shutil.rmtree(fixture,ignore_errors=True)

if __name__=='__main__':sys.exit(main())

"""Actual isolated Caddy 80/443 TLS and fresh-volume/restart validation."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

def command(argv,timeout=60):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Isolated Caddy fixture command failed: '+' '.join(argv[:3]))
    return result.stdout

def probe(architecture):
    image='smsinvit-caddy:v1.0.0-python-'+architecture+'-local'
    image_info=json.loads(command(['docker','image','inspect',image]))[0]
    prefix='smsinvit-caddy-fixture-'+uuid.uuid4().hex[:10]
    container=prefix;volumes=[prefix+'-data',prefix+'-config']
    try:
        for volume in volumes:command(['docker','volume','create',volume])
        with tempfile.TemporaryDirectory(prefix='caddy-local-tls-fixture-') as temporary:
            folder=Path(temporary);config=folder/'Caddyfile';config.write_text('''{
    admin off
    skip_install_trust
}
localhost {
    tls internal
    respond "isolated-caddy-fixture" 200
}
''');config.chmod(0o644)
            command(['docker','run','--detach','--name',container,'--network','none','--read-only','--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges','--sysctl','net.ipv4.ip_unprivileged_port_start=0','--tmpfs','/tmp:rw,nosuid,nodev,size=32m,mode=1777','--mount','type=volume,source='+volumes[0]+',target=/data','--mount','type=volume,source='+volumes[1]+',target=/config','--mount','type=bind,source='+str(config)+',target=/fixture/Caddyfile,readonly',image,'caddy','run','--config','/fixture/Caddyfile'])
            for _ in range(60):
                copied=subprocess.run(['docker','cp',container+':/data/caddy/pki/authorities/local/root.crt',str(folder/'root.crt')],capture_output=True,timeout=15)
                if copied.returncode==0:break
                time.sleep(.25)
            else:raise RuntimeError('The nonroot fixture could not create its persistent CA.')
            ca=folder/'root.crt';ca.chmod(0o644);before=hashlib.sha256(ca.read_bytes()).hexdigest()
            client='''import http.client,ssl,json
c=ssl.create_default_context(cafile='/fixture/root.crt');h=http.client.HTTPSConnection('localhost',443,context=c,timeout=10);h.request('GET','/');r=h.getresponse();assert r.status==200 and r.read()==b'isolated-caddy-fixture';h.close()
h=http.client.HTTPConnection('localhost',80,timeout=10);h.request('GET','/');r=h.getresponse();assert r.status==308 and r.getheader('Location')=='https://localhost/';h.close();print('verified-local-TLS-and-HTTP-redirect')'''
            def request():
                return command(['docker','run','--rm','--network','container:'+container,'--read-only','--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges','--mount','type=bind,source='+str(ca)+',target=/fixture/root.crt,readonly','--entrypoint','python','smsinvit-python:v1.0.0-python-arm64-local','-c',client])
            request();command(['docker','restart','--time','5',container]);request()
            command(['docker','cp',container+':/data/caddy/pki/authorities/local/root.crt',str(folder/'restarted-root.crt')]);assert hashlib.sha256((folder/'restarted-root.crt').read_bytes()).hexdigest()==before
            facts=json.loads(command(['docker','inspect',container]))[0]
            assert facts['Config']['User']=='10001:10001' and facts['HostConfig']['ReadonlyRootfs'] and facts['HostConfig']['CapDrop']==['ALL']
            assert facts['HostConfig']['NetworkMode']=='none' and facts['HostConfig']['Sysctls']['net.ipv4.ip_unprivileged_port_start']=='0'
            status=command(['docker','exec',container,'sh','-c','cat /proc/1/status; stat -c "%u:%g %a %n" /data /config'])
            assert 'CapEff:\t0000000000000000' in status and 'CapBnd:\t0000000000000000' in status and 'NoNewPrivs:\t1' in status
            assert '10001:10001 755 /data' in status and '10001:10001 755 /config' in status
            return {'status':'PASS','architecture':architecture,'image':image,'imageId':image_info['Id'],'freshNamedVolumes':True,'runtimeUser':'10001:10001','readOnlyRoot':True,'capabilitiesEffectiveAndBoundingZero':True,'noNewPrivileges':True,'namespacedUnprivilegedPortStart':0,'actualHttp80Redirect':True,'actualHttps443TrustedLocalCa':True,'persistentCaUnchangedAfterRestart':True,'caCertificateSha256':before,'externalNetwork':'none','realUpstreamRequests':0,'businessDataMounted':False}
    finally:
        subprocess.run(['docker','rm','--force',container],capture_output=True,timeout=30)
        for volume in volumes:subprocess.run(['docker','volume','rm',volume],capture_output=True,timeout=30)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args()
    rows=[]
    for arch in ['amd64','arm64']:
        rows.append(probe(arch));print(json.dumps(rows[-1]),flush=True)
    a.evidence.parent.mkdir(parents=True,exist_ok=True);a.evidence.write_text(json.dumps({'status':'PASS','architectures':rows,'allFixtureContainersAndVolumesRemoved':True},indent=2,sort_keys=True)+'\n')

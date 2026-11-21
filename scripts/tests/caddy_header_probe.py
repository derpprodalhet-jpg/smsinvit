"""Real Caddy/public-template forwarding probe with a loopback-only fake backend."""
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time

class Backend(BaseHTTPRequestHandler):
    def do_GET(self):
        data=json.dumps(dict(self.headers)).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass


def main():
    binary=Path('/fixtures/caddy');backend=ThreadingHTTPServer(('127.0.0.1',0),Backend);thread=threading.Thread(target=backend.serve_forever,daemon=True);thread.start();records=[];error_records=[]
    try:
        for project in ('smsinvit','dujiao'):
            original=Path('/fixtures/'+project+'.Caddyfile').read_text()
            with socket.socket() as descriptor:descriptor.bind(('127.0.0.1',0));port=descriptor.getsockname()[1]
            site='127.0.0.1:'+str(port)
            target='api:3005' if project=='smsinvit' else 'shop:8080'
            config=original.replace('{$ACME_EMAIL}','fixture@example.com').replace('{$DOMAIN}','http://'+site).replace(target,'127.0.0.1:'+str(backend.server_port))
            with tempfile.TemporaryDirectory(prefix='caddy-header-probe-') as temporary:
                path=Path(temporary)/'Caddyfile';path.write_text(config)
                validated=subprocess.run([str(binary),'validate','--config',str(path),'--adapter','caddyfile'],capture_output=True,text=True,timeout=15);assert validated.returncode==0,validated.stderr
                process=subprocess.Popen([str(binary),'run','--config',str(path),'--adapter','caddyfile'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                try:
                    for _ in range(100):
                        try:
                            with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                        except OSError:time.sleep(.02)
                    else:raise AssertionError('Caddy did not start')
                    for forged in (False,True):
                        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                        connection.putrequest('GET','/fixture')
                        if forged:
                            for key,value in {'Forwarded':'for=203.0.113.99;proto=https;host=attacker.invalid','X-Real-IP':'203.0.113.99','X-Forwarded-Port':'9443','X-Forwarded-For':'203.0.113.99','X-Forwarded-Proto':'https','X-Forwarded-Host':'attacker.invalid'}.items():connection.putheader(key,value)
                        connection.endheaders();response=connection.getresponse();assert response.status==200;headers={k.lower():v for k,v in json.loads(response.read()).items()};connection.close()
                        assert not {'forwarded','x-real-ip','x-forwarded-port'}&headers.keys()
                        assert headers['x-forwarded-for']=='127.0.0.1' and headers['x-forwarded-proto']=='http' and headers['x-forwarded-host']==site and headers['host']==site
                        assert not any('attacker.invalid' in value or '203.0.113.99' in value for value in headers.values())
                        records.append({'project':project,'forgedClientHeaders':forged,'clientFakeFieldsReachedBackend':False,'canonicalHostProtoAndPeerGenerated':True,'explicitForwardedRealIpPortRemoved':True,'templateSha256':hashlib.sha256(original.encode()).hexdigest()})
                finally:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                with socket.socket() as descriptor:descriptor.bind(('127.0.0.1',0));failed_port=descriptor.getsockname()[1]
                path.write_text(config.replace('127.0.0.1:'+str(backend.server_port),'127.0.0.1:'+str(failed_port)))
                error_log=Path(temporary)/'runtime-errors.jsonl'
                with error_log.open('w') as output:
                    process=subprocess.Popen([str(binary),'run','--config',str(path),'--adapter','caddyfile'],stdout=output,stderr=output)
                    try:
                        for _ in range(100):
                            try:
                                with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                            except OSError:time.sleep(.02)
                        else:raise AssertionError('Caddy error fixture did not start')
                        canaries=['synthetic-smsinvit-card-825471','synthetic-provider-card-519382','synthetic-invitation-token-417682','synthetic-cookie-727412','synthetic-authorization-192756']
                        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                        connection.request('GET','/fixture?smsinvitCdk='+canaries[0]+'&zcCdk='+canaries[1]+'&query_token='+canaries[2],headers={'X-Card-Key':canaries[0],'Cookie':'fixture='+canaries[3],'Authorization':'Bearer '+canaries[4]})
                        response=connection.getresponse();assert response.status==502;response.read();connection.close();time.sleep(.05)
                    finally:
                        process.terminate()
                        try:process.wait(timeout=5)
                        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                logged=error_log.read_text();assert all(canary not in logged for canary in canaries)
                rows=[json.loads(line) for line in logged.splitlines() if line.startswith('{')]
                failures=[row for row in rows if row.get('status')==502 and row.get('level')=='error']
                assert failures and all('uri' not in row.get('request',{}) and 'headers' not in row.get('request',{}) for row in failures)
                error_records.append({'project':project,'actualStatus':502,'actualErrorRecordPresent':True,'requestUriAndHeadersRemoved':True,'plaintextSyntheticSecretsLogged':0,'diagnosticStatusAndErrorIdRetained':True})
    finally:backend.shutdown();backend.server_close()
    print(json.dumps({'status':'PASS','caddyBinarySha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'productionTemplatesValidated':2,'cases':records,'backendErrorLogCases':error_records,'network':'none; loopback fake backend only','productionServicesStarted':False,'realUpstreamBusinessRequests':0},sort_keys=True))

if __name__=='__main__':main()

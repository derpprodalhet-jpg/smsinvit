"""Real socket/epoll/TLS/CLI probe; all requests terminate at a local mock.

The test patches ONLY the trusted policy constructor to authorize the isolated
fixture IP/port. Production helper has no test-policy flag or private target
configuration. Never uses production auth or performs an upstream registration.
"""
import datetime
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import socket
import socketserver
import base64
import http.server
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization,hashes
from cryptography.hazmat.primitives.asymmetric import rsa
import ipaddress

private=Path(tempfile.mkdtemp(prefix='mock-private-'));home=Path(tempfile.mkdtemp(prefix='mock-home-'));work=Path(tempfile.mkdtemp(prefix='mock-work-'))
ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'SmsInvit Fixture Root')])
now=datetime.datetime.now(datetime.timezone.utc)
ca=x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(ca_key,hashes.SHA256())
cert=x509.CertificateBuilder().subject_name(name).issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1')),x509.IPAddress(ipaddress.ip_address('8.8.8.8'))]),critical=False).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).sign(ca_key,hashes.SHA256())
(private/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()));(private/'leaf.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM));(home/'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
requests=[]
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def do_GET(self):
        requests.append(('GET',self.path));body=b'fixture-response'
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')));requests.append(('POST',self.path))
        if not self.path.endswith('/responses'):
            self.send_response(404);self.send_header('Content-Length','0');self.end_headers();return
        json.loads(body)
        message={'id':'msg_fixture','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'fixture-success','annotations':[]}]}
        response={'id':'resp_fixture','object':'response','created_at':1,'status':'completed','error':None,'incomplete_details':None,'model':'mock','output':[message],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}
        events=[('response.created',{'type':'response.created','response':{**response,'status':'in_progress','output':[]}}),('response.output_item.added',{'type':'response.output_item.added','output_index':0,'item':{**message,'status':'in_progress','content':[]}}),('response.content_part.added',{'type':'response.content_part.added','item_id':'msg_fixture','output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}}),('response.output_text.delta',{'type':'response.output_text.delta','item_id':'msg_fixture','output_index':0,'content_index':0,'delta':'fixture-success'}),('response.output_text.done',{'type':'response.output_text.done','item_id':'msg_fixture','output_index':0,'content_index':0,'text':'fixture-success'}),('response.output_item.done',{'type':'response.output_item.done','output_index':0,'item':message}),('response.completed',{'type':'response.completed','response':response})]
        payload=''.join('event: '+kind+'\ndata: '+json.dumps(data)+'\n\n' for kind,data in events).encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(payload)));self.send_header('Connection','close');self.end_headers();self.wfile.write(payload)
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(str(private/'leaf.pem'),str(private/'key.pem'));server.socket=context.wrap_socket(server.socket,server_side=True)
threading.Thread(target=server.serve_forever,daemon=True).start();port=server.server_port
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
# Local proxy fixtures exercise the same production proxy negotiation code.
from urllib.parse import urlsplit
class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        from sandbox_network import read_exact
        if self.server.scheme=='socks5':
            assert read_exact(self.request,3)==b'\x05\x01\x02';self.request.sendall(b'\x05\x02')
            assert read_exact(self.request,1)==b'\x01'
            username=read_exact(self.request,read_exact(self.request,1)[0]);password=read_exact(self.request,read_exact(self.request,1)[0])
            assert username==b'fixture-user' and password==b'fixture-password';self.request.sendall(b'\x01\x00')
            assert read_exact(self.request,10)==b'\x05\x01\x00\x01'+socket.inet_aton('8.8.8.8')+b'\x01\xbb'
            self.request.sendall(b'\x05\x00\x00\x01'+b'\x00'*6)
        else:
            header=bytearray()
            while not header.endswith(b'\r\n\r\n'):
                assert len(header)<8192;header.extend(read_exact(self.request,1))
            assert header.startswith(b'CONNECT 8.8.8.8:443 HTTP/1.1\r\n')
            assert base64.b64encode(b'fixture-user:fixture-password') in header
            self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
        remote=socket.create_connection(('127.0.0.1',port))
        def copy(source,target):
            try:
                while True:
                    data=source.recv(65536)
                    if not data:break
                    target.sendall(data)
            except OSError:pass
            finally:
                try:target.shutdown(socket.SHUT_WR)
                except OSError:pass
        thread=threading.Thread(target=copy,args=(self.request,remote),daemon=True);thread.start();copy(remote,self.request);thread.join(5);remote.close()
proxies=[]
for scheme in ('http','https','socks5'):
    proxy=socketserver.ThreadingTCPServer(('127.0.0.1',0),ProxyHandler);proxy.daemon_threads=True;proxy.scheme=scheme
    if scheme=='https':proxy.socket=context.wrap_socket(proxy.socket,server_side=True)
    threading.Thread(target=proxy.serve_forever,daemon=True).start();proxies.append(proxy)
scripts=Path(__file__).resolve().parents[1]
wrapper='''import sys
sys.path.insert(0,%r)
import sandbox_exec
from sandbox_network import Policy
class FixturePolicy(Policy):
 def __init__(self,*args,**kwargs):
  super().__init__({},enabled=False);self.targets=set();self.resolvers=set()
 def approved(self,address,port):return address=="127.0.0.1" and port==%d
sandbox_exec.Policy=FixturePolicy
sys.exit(sandbox_exec.main())
''' % (str(scripts),port)
sys.path.insert(0,str(scripts))
base=[sys.executable,'-c',wrapper,'--home',str(home),'--workdir',str(work),'--ca-file',str(home/'ca.pem'),'--']
node='''const https=require('https');let count=0;for(let i=0;i<12;i++){https.get('https://127.0.0.1:%d/fixture-'+i,r=>{let b='';r.on('data',v=>b+=v);r.on('end',()=>{if(b!=='fixture-response')process.exit(3);if(++count===12)console.log('node-tls-epoll-12-pass')})}).on('error',e=>{console.error(e.message);process.exit(2)})}''' % port
closed='import socket;[(lambda s:s.close())(socket.socket()) for _ in range(250)];print("closed-sockets-reclaimed")'
result=subprocess.run(base+[sys.executable,'-c',closed],capture_output=True,text=True,timeout=20)
assert result.returncode==0 and 'closed-sockets-reclaimed' in result.stdout,result.stderr
result=subprocess.run(base+['/opt/smsinvit-bin/node','-e',node],capture_output=True,text=True,timeout=30)
assert result.returncode==0 and 'node-tls-epoll-12-pass' in result.stdout,result.stderr
for proxy in proxies:
    proxy_wrapper=wrapper.replace('self.targets=set();self.resolvers=set()', 'self.targets={"8.8.8.8"};self.resolvers=set();self.proxy_tls_context=__import__("ssl").create_default_context(cafile='+repr(str(home/'ca.pem'))+');self.proxy=(__import__("urllib.parse",fromlist=["urlsplit"]).urlsplit("'+proxy.scheme+'://fixture-user:fixture-password@127.0.0.1:'+str(proxy.server_address[1])+'"),("127.0.0.1",))').replace('return address=="127.0.0.1" and port=='+str(port), 'return address=="8.8.8.8" and port==443')
    proxy_base=base.copy();proxy_base[2]=proxy_wrapper
    proxy_node="const https=require('https');https.get('https://8.8.8.8/approved-proxy',r=>{let b='';r.on('data',v=>b+=v);r.on('end',()=>{if(b==='fixture-response')console.log('proxy-pass');else process.exit(3)})}).on('error',e=>{console.error(e.message);process.exit(2)});"
    result=subprocess.run(proxy_base+['/opt/smsinvit-bin/node','-e',proxy_node],capture_output=True,text=True,timeout=20)
    assert result.returncode==0 and 'proxy-pass' in result.stdout,(proxy.scheme,result.stderr)
# Auth rotation uses actual CLI, an entirely fake local API key and fresh home.
result=subprocess.run(base+['/opt/codex/bin/codex','login','--with-api-key'],input='sk-fixture-no-real-upstream\n',capture_output=True,text=True,timeout=20)
assert result.returncode==0,result.stderr
assert 'sk-fixture-no-real-upstream' in (home/'auth.json').read_text()
provider='model_providers.fixture={name="Fixture",base_url="https://127.0.0.1:'+str(port)+'/v1",wire_api="responses",requires_openai_auth=true}'
command=['/opt/codex/bin/codex','exec','--ignore-user-config','--skip-git-repo-check','--sandbox','read-only','--json','--ephemeral','-m','gpt-5','-c','model_provider="fixture"','-c',provider,'--','Return fixture text']
result=subprocess.run(base+command,capture_output=True,text=True,timeout=40)
assert result.returncode==0 and 'fixture-success' in result.stdout,result.stderr[-4000:]+'\n'+result.stdout[-2000:]
assert sum(kind=='POST' for kind,_ in requests)==1,requests
server.shutdown()
for proxy in proxies:proxy.shutdown()
print(json.dumps({'status':'PASS','architecture':__import__('platform').machine(),'closedUnconnectedSocketsReclaimed':250,'verifiedTransparentProxySchemes':['http','https','socks5'],'nodeParallelHttpsRequests':12,'realCodexMockResponseRequests':1,'realCodexLocalKeyPersistence':True,'hostnameAndCaVerified':True,'socketEpollAndTlsCompatible':True,'realUpstreamBusinessRequests':0},sort_keys=True))

"""Local-only HTTPS fixtures verify TLS, pinning, redirect checks and response caps."""
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import socket
import ssl
import threading
from pathlib import Path
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from smsinvit.invitation.safety import GuardedCffiSession,GuardedRequestsSession,require_url,require_proxy,_PinnedAdapter

@pytest.fixture
def server(tmp_path,monkeypatch):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);now=datetime.now(timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=2)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),critical=False).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256())
    certfile=tmp_path/'ca.pem';keyfile=tmp_path/'key.pem';certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM));keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
    class Records(list):cross_url=None
    requests=Records()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            requests.append((self.path,self.headers.get('Host'),self.headers.get('Authorization'),self.headers.get('Cookie')))
            if self.path=='/cross':
                self.send_response(302);self.send_header('Location',requests.cross_url);self.send_header('Content-Length','0');self.end_headers();return
            if self.path=='/fail':
                self.send_response(401);self.send_header('Content-Length','6');self.end_headers();self.wfile.write(b'denied');return
            if self.path=='/redirect':
                self.send_response(302);self.send_header('Location','http://127.0.0.1:1/blocked');self.send_header('Content-Length','0');self.end_headers();return
            body=b'x'*(4*1024*1024+1000) if self.path=='/large' else b'{"ok":true}'
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
            try:self.wfile.write(body)
            except (OSError,ssl.SSLError):pass
    httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler);tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(certfile,keyfile);httpd.socket=tls.wrap_socket(httpd.socket,server_side=True)
    thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start();base='https://localhost:'+str(httpd.server_port)
    monkeypatch.setenv('SMSINVIT_ENV','test');monkeypatch.setenv('SMSINVIT_INVITATION_CA_BUNDLE',str(certfile));monkeypatch.setenv('SMSINVIT_INVITATION_TEST_URLS',','.join(base+p for p in ('/ok','/redirect','/large','/cross','/fail')))
    yield base,requests,certfile
    httpd.shutdown();httpd.server_close();thread.join(timeout=5)

@pytest.mark.parametrize('session_type',[GuardedRequestsSession,GuardedCffiSession])
def test_tls_pin_preserves_host_and_ignores_verify_false(server,session_type):
    base,requests,_=server
    with session_type() as session:
        response=session.get(base+'/ok',verify=False)
        assert response.status_code==200 and response.json()=={'ok':True}
        assert requests[-1][1].startswith('localhost:')

def test_adapter_uses_pinned_socket_and_correct_certificate_name(server,monkeypatch):
    base,_,_=server
    from smsinvit.invitation import safety
    checked=[];original=safety._socket_connection.create_connection
    def capture(address,*args,**kwargs):checked.append(address);return original(address,*args,**kwargs)
    monkeypatch.setattr(safety._socket_connection,'create_connection',capture)
    with GuardedRequestsSession() as session:assert session.get(base+'/ok').status_code==200
    assert checked and checked[0][0]=='127.0.0.1'

@pytest.mark.parametrize('session_type',[GuardedRequestsSession,GuardedCffiSession])
def test_redirect_private_target_blocked_before_second_request(server,session_type):
    base,requests,_=server
    with session_type() as session:
        with pytest.raises(ValueError):session.get(base+'/redirect')
    assert len(requests)==1

@pytest.mark.parametrize('session_type',[GuardedRequestsSession,GuardedCffiSession])
def test_response_cap_aborts_before_large_payload_returns(server,session_type):
    base,_,_=server
    with session_type() as session:
        with pytest.raises(Exception):session.get(base+'/large')

@pytest.mark.parametrize('session_type',[GuardedRequestsSession,GuardedCffiSession])
def test_untrusted_ca_rejected_even_caller_disables_verify(server,monkeypatch,session_type):
    base,_,_=server;monkeypatch.delenv('SMSINVIT_INVITATION_CA_BUNDLE')
    with session_type() as session:
        with pytest.raises(Exception):session.get(base+'/ok',verify=False)

def test_private_proxy_requires_deployment_allowlist(monkeypatch):
    monkeypatch.delenv('SMSINVIT_INVITATION_PROXY_HOSTS',raising=False)
    with pytest.raises(ValueError):require_proxy('http://127.0.0.1:8080')
    monkeypatch.setenv('SMSINVIT_INVITATION_PROXY_HOSTS','127.0.0.1:8080')
    assert require_proxy('http://127.0.0.1:8080')[2]==('127.0.0.1',)

@pytest.fixture
def proxy(server,monkeypatch,request):
    import select
    connections=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_CONNECT(self):
            connections.append(self.path)
            # Test fixture only tunnels to its own TLS server; capture the exact
            # CONNECT destination to prove the proxy receives the checked IP.
            host,port=self.path.rsplit(':',1)
            assert host=='127.0.0.1'
            upstream=socket.create_connection((host,int(port)))
            self.send_response(200);self.end_headers()
            peers=[self.connection,upstream]
            try:
                while True:
                    ready,_,_=select.select(peers,[],[],3)
                    if not ready:break
                    for peer in ready:
                        data=peer.recv(65536)
                        if not data:return
                        other=upstream if peer is self.connection else self.connection
                        other.sendall(data)
            except OSError:pass
            finally:upstream.close()
    p=ThreadingHTTPServer(('127.0.0.1',0),Handler);scheme=getattr(request,'param','http')
    if scheme=='https':
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(server[2],server[2].parent/'key.pem');p.socket=tls.wrap_socket(p.socket,server_side=True)
    t=threading.Thread(target=p.serve_forever,daemon=True);t.start()
    monkeypatch.setenv('SMSINVIT_INVITATION_PROXY_HOSTS','localhost:'+str(p.server_port))
    yield scheme+'://localhost:'+str(p.server_port),connections
    p.shutdown();p.server_close();t.join(timeout=5)

@pytest.mark.parametrize('session_type',[GuardedRequestsSession,GuardedCffiSession])
@pytest.mark.parametrize('proxy',['http','https'],indirect=True)
def test_proxy_connect_target_is_pinned_and_tls_name_stays_original(server,proxy,session_type):
    base,_,_=server;proxy_url,connections=proxy
    with session_type() as session:
        response=session.get(base+'/ok',proxies={'https':proxy_url})
        assert response.status_code==200 and response.json()=={'ok':True}
    assert connections and connections[0].startswith('127.0.0.1:')

def test_urllib_fallback_shares_tls_address_limit_and_http_error_contract(server):
    from smsinvit.invitation.urlclient import urlopen
    from urllib.error import HTTPError
    base,requests,_=server
    with urlopen(base+'/ok') as response:assert response.status==200 and json_load(response.read())=={'ok':True}
    with pytest.raises(HTTPError) as failure:urlopen(base+'/fail')
    assert failure.value.code==401 and failure.value.read()==b'denied'
    with pytest.raises(ValueError):urlopen(base+'/redirect')

def json_load(data):
    import json
    return json.loads(data)

@pytest.mark.parametrize('session_type',[GuardedRequestsSession,GuardedCffiSession])
def test_cross_origin_redirect_never_forwards_authorization_or_cookies(server,monkeypatch,session_type):
    import os
    base,records,certfile=server;received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            received.append((self.headers.get('Authorization'),self.headers.get('Cookie')))
            self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
    second=ThreadingHTTPServer(('127.0.0.1',0),Handler);context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certfile,certfile.with_name('key.pem'));second.socket=context.wrap_socket(second.socket,server_side=True)
    thread=threading.Thread(target=second.serve_forever,daemon=True);thread.start()
    records.cross_url='https://localhost:'+str(second.server_port)+'/destination'
    monkeypatch.setenv('SMSINVIT_INVITATION_TEST_URLS',os.environ['SMSINVIT_INVITATION_TEST_URLS']+','+records.cross_url)
    try:
        with session_type() as session:
            session.headers.update({'Authorization':'Bearer private-token','Cookie':'private_cookie=private-value'})
            session.cookies.set('session_cookie','private-jar-value')
            assert session.get(base+'/cross',cookies={'per_request_cookie':'private-request-value'}).status_code==200
        assert received==[(None,None)] or received==[('',None)] or received==[('','')]
    finally:second.shutdown();second.server_close();thread.join(timeout=5)

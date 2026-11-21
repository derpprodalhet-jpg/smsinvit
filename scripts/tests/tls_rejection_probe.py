"""Verify CA, hostname and expiration rejection through the real Node boundary.

Only fresh local test certificates and local fixture servers are used. Production
helper has no test authorization switch; this patches only its trusted policy.
"""
import datetime
import http.server
import ipaddress
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from cryptography import x509
from cryptography.hazmat.primitives import serialization,hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

root=Path(tempfile.mkdtemp(prefix='tls-fixture-private-'));home=Path(tempfile.mkdtemp(prefix='tls-task-home-'));work=Path(tempfile.mkdtemp(prefix='tls-task-work-'))
now=datetime.datetime.now(datetime.timezone.utc);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Fixture CA')]);key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
ca=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256())
(home/'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM));scripts=Path(__file__).resolve().parents[1]
results={}
for case in ('valid','hostname','expired','untrusted_ca'):
    signing_key=key;issuer=name
    if case=='untrusted_ca':signing_key=rsa.generate_private_key(public_exponent=65537,key_size=2048);issuer=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Untrusted CA')])
    leafkey=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    start=now-datetime.timedelta(days=2);end=now-datetime.timedelta(days=1) if case=='expired' else now+datetime.timedelta(days=1)
    san=x509.DNSName('unapproved.example') if case=='hostname' else x509.IPAddress(ipaddress.ip_address('127.0.0.1'))
    cert=x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Fixture Server')])).issuer_name(issuer).public_key(leafkey.public_key()).serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end).add_extension(x509.SubjectAlternativeName([san]),critical=False).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).sign(signing_key,hashes.SHA256())
    (root/'leaf.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM));(root/'key.pem').write_bytes(leafkey.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
    requests=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):requests.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(b'fixture')
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(str(root/'leaf.pem'),str(root/'key.pem'));server.socket=context.wrap_socket(server.socket,server_side=True);threading.Thread(target=server.serve_forever,daemon=True).start()
    wrapper='''import sys
sys.path.insert(0,%r)
import sandbox_exec
from sandbox_network import Policy
class FixturePolicy(Policy):
 def __init__(self,*a,**kw):super().__init__({},enabled=False)
 def approved(self,address,port):return address=="127.0.0.1" and port==%d
sandbox_exec.Policy=FixturePolicy
sys.exit(sandbox_exec.main())
'''%(str(scripts),server.server_port)
    node="const https=require('https');https.get('https://127.0.0.1:"+str(server.server_port)+"/fixture',r=>{r.resume();r.on('end',()=>console.log('TLS_ACCEPTED'))}).on('error',e=>console.log(e.code));"
    result=subprocess.run([sys.executable,'-c',wrapper,'--home',str(home),'--workdir',str(work),'--ca-file',str(home/'ca.pem'),'--','/opt/smsinvit-bin/node','-e',node],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    if case=='valid':assert result.stdout.strip()=='TLS_ACCEPTED' and len(requests)==1
    else:assert result.stdout.strip() in ('ERR_TLS_CERT_ALTNAME_INVALID','CERT_HAS_EXPIRED','UNABLE_TO_VERIFY_LEAF_SIGNATURE','UNABLE_TO_VERIFY_FIRST_CERT','UNABLE_TO_GET_ISSUER_CERT_LOCALLY','CERT_SIGNATURE_FAILURE') and not requests,result.stdout
    results[case]={'httpRequests':len(requests),'result':result.stdout.strip()};server.shutdown()
print(json.dumps({'status':'PASS','architecture':__import__('platform').machine(),'cases':results,'realUpstreamBusinessRequests':0},sort_keys=True))

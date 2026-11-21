"""Linux-only standalone broker-policy assertions for offline image/VM evidence."""
import ctypes
import json
import os
from pathlib import Path
import socket
import struct
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox_network import Policy,dns_question,endpoint_snapshot,Stream
lookups=[]
def resolver(host,port,**kwargs):
    lookups.append(host);address='127.0.0.1' if host=='proxy.example' else '8.8.8.8'
    return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,port))]
with patch('socket.getaddrinfo',resolver):
    policy=Policy({'HTTPS_PROXY':'https://proxy.example:8443'},proxy_pins=['192.0.2.1'])
assert 'proxy.example' not in lookups and policy.proxy[1]==('192.0.2.1',)
assert policy.approved('8.8.8.8',443) and not policy.approved('127.0.0.1',443)
with patch('socket.getaddrinfo',resolver):
    try:Policy({'ALL_PROXY':'http://proxy.example:8443'});raise AssertionError('unapproved private proxy')
    except ValueError:pass

def query(host,kind=1,extra=b''):
    labels=b''.join(bytes([len(v)])+v.encode() for v in host.split('.'))+b'\0'
    return struct.pack('!6H',123,0x100,1,0,0,int(bool(extra)))+labels+struct.pack('!HH',kind,1)+extra
normal=query('chatgpt.com');assert dns_question(normal)==normal
assert dns_question(query('api.openai.com',28,b'extra-not-forwarded'))==query('api.openai.com',28)
for bad in (query('secret.attacker.example'),query('localhost'),query('chatgpt.com',16),query('auth.openai.com')+b'x'*5000,b'\0'*12):
    try:dns_question(bad);raise AssertionError('unauthorized DNS question')
    except ValueError:pass
value=struct.pack('=H',socket.AF_INET)+struct.pack('!H',443)+socket.inet_aton('8.8.8.8')+b'\0'*8
buffer=ctypes.create_string_buffer(value)
assert endpoint_snapshot(os.getpid(),ctypes.addressof(buffer),16)==(socket.AF_INET,'8.8.8.8',443)
for address,scope in (('::ffff:8.8.8.8',0),('fe80::1',1)):
    value=struct.pack('=H',socket.AF_INET6)+struct.pack('!H',443)+b'\0'*4+socket.inet_pton(socket.AF_INET6,address)+struct.pack('=I',scope)
    buffer=ctypes.create_string_buffer(value)
    try:endpoint_snapshot(os.getpid(),ctypes.addressof(buffer),28);raise AssertionError('mapped or scoped IPv6')
    except ValueError:pass
for _ in range(200):
    stream=Stream(socket.AF_INET,socket.SOCK_CLOEXEC);stream.client.close();stream.client=None
    assert stream.abandoned() and stream.closed
print(json.dumps({'status':'PASS','architecture':__import__('platform').machine(),'proxyDnsSnapshotPinned':True,'unapprovedPrivateProxyDenied':True,'dnsExfiltrationDenied':True,'dnsExtraPayloadStripped':True,'boundedSockaddrSnapshot':True,'mappedAndScopedIpv6Denied':True,'closedStreamReclaimed':200,'realUpstreamBusinessRequests':0},sort_keys=True))

import ctypes
import ipaddress
import os
from pathlib import Path
import socket
import struct
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox_network import Policy,dns_question,endpoint_snapshot,Stream,read_exact,Broker,_architecture
from types import SimpleNamespace

def records(address,port):
    family=socket.AF_INET6 if ':' in address else socket.AF_INET
    return [(family,socket.SOCK_STREAM,6,'',(address,port))]

def test_proxy_snapshot_does_not_resolve_again(monkeypatch):
    lookups=[]
    def resolve(host,port,**kwargs):
        lookups.append(host)
        return records('127.0.0.1' if host=='proxy.example' else '8.8.8.8',port)
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    policy=Policy({'HTTPS_PROXY':'https://proxy.example:8443'},proxy_pins=['192.0.2.1'])
    assert 'proxy.example' not in lookups
    assert policy.proxy[1]==('192.0.2.1',)
    assert policy.approved('8.8.8.8',443) and not policy.approved('127.0.0.1',443)

def test_proxy_without_snapshot_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda host,port,**kwargs:records('127.0.0.1',port))
    with pytest.raises(ValueError,match='Private proxy'):Policy({'ALL_PROXY':'http://proxy.example:8443'})

def query(host,kind=1,extra=b''):
    labels=b''.join(bytes([len(v)])+v.encode() for v in host.split('.'))+b'\0'
    return struct.pack('!6H',123,0x100,1,0,0,int(bool(extra)))+labels+struct.pack('!HH',kind,1)+extra

def test_dns_only_authorized_names_and_no_extra_payload():
    normal=query('chatgpt.com')
    assert dns_question(normal)==normal
    assert dns_question(query('api.openai.com',28,b'not-forwarded'))==query('api.openai.com',28)
    for value in (query('secret.attacker.example'),query('localhost'),query('chatgpt.com',16),query('auth.openai.com')+b'x'*5000,b'\0'*12):
        with pytest.raises(ValueError):dns_question(value)

@pytest.mark.skipif(sys.platform!='linux',reason='Real Linux proc address memory required')
def test_address_snapshot_and_mapped_scope_rejected():
    address=struct.pack('=H',socket.AF_INET)+struct.pack('!H',443)+socket.inet_pton(socket.AF_INET,'8.8.8.8')+b'\0'*8
    buffer=ctypes.create_string_buffer(address)
    assert endpoint_snapshot(os.getpid(),ctypes.addressof(buffer),16)==(socket.AF_INET,'8.8.8.8',443)
    for literal,scope in (('::ffff:8.8.8.8',0),('fe80::1',1)):
        value=struct.pack('=H',socket.AF_INET6)+struct.pack('!H',443)+b'\0'*4+ipaddress.ip_address(literal).packed+struct.pack('=I',scope)
        buffer=ctypes.create_string_buffer(value)
        with pytest.raises(ValueError):endpoint_snapshot(os.getpid(),ctypes.addressof(buffer),28)

@pytest.mark.skipif(sys.platform!='linux',reason='Real Linux socket flags required')
def test_closed_unconnected_stream_is_reclaimed():
    for _ in range(200):
        stream=Stream(socket.AF_INET,socket.SOCK_CLOEXEC)
        stream.client.close();stream.client=None
        assert stream.abandoned() and stream.closed


def answers(reply):
    transaction,flags,questions,count,authority,additional=struct.unpack_from('!6H',reply)
    offset=12
    while reply[offset]:offset+=1+reply[offset]
    offset+=5
    addresses=[]
    for _ in range(count):
        name,kind,klass,ttl,size=struct.unpack_from('!HHHIH',reply,offset)
        assert name==0xc00c and klass==1 and ttl==60
        offset+=12
        addresses.append(str(ipaddress.ip_address(reply[offset:offset+size])));offset+=size
    assert offset==len(reply)
    return transaction,flags,questions,authority,additional,addresses


@pytest.fixture
def frozen_policy(monkeypatch):
    lookups=[]
    def resolve(host,port,**kwargs):
        lookups.append(host)
        return records('8.8.8.8',port)+records('2001:4860:4860::8888',port)
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    policy=Policy({})
    assert lookups==list(__import__('sandbox_network').OFFICIAL_HOSTS)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:pytest.fail('DNS must not be resolved again'))
    return policy


@pytest.mark.parametrize('kind,expected',[(1,['8.8.8.8']),(28,['2001:4860:4860::8888'])])
def test_child_dns_uses_same_public_snapshot_as_connect(frozen_policy,kind,expected):
    packet=query('chatgpt.com',kind,b'EDNS-is-not-forwarded')
    reply=frozen_policy.dns_response(packet)
    transaction,flags,questions,authority,additional,addresses=answers(reply)
    assert (transaction,flags,questions,authority,additional)==(123,0x8180,1,0,0)
    assert addresses==expected
    assert all(frozen_policy.approved(address,443) for address in addresses)
    assert not frozen_policy.approved('1.1.1.1',443)


@pytest.mark.parametrize('kind',[1,28])
def test_empty_dns_family_returns_no_addresses_without_lookup(frozen_policy,kind):
    frozen_policy.host_addresses['chatgpt.com']=()
    assert answers(frozen_policy.dns_response(query('chatgpt.com',kind)))[-1]==[]


def test_private_dns_answer_is_never_granted(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda host,port,**kw:records('127.0.0.1',port))
    policy=Policy({})
    assert not policy.targets and not policy.approved('127.0.0.1',443)
    assert answers(policy.dns_response(query('chatgpt.com')))[-1]==[]
    policy.host_addresses['chatgpt.com']=('127.0.0.1',)
    with pytest.raises(ValueError,match='approved public'):policy.dns_response(query('chatgpt.com'))


def test_dns_snapshot_cannot_include_unapproved_or_unknown_targets(frozen_policy):
    with pytest.raises(ValueError,match='not authorized'):frozen_policy.dns_response(query('unapproved.example'))
    frozen_policy.host_addresses['chatgpt.com']=('1.1.1.1',)
    with pytest.raises(ValueError,match='approved public'):frozen_policy.dns_response(query('chatgpt.com'))


@pytest.mark.skipif(sys.platform!='linux',reason='Real Linux proc address memory required')
def test_mock_kernel_tcp_dns_channel_returns_snapshot_without_resolver_connection(frozen_policy,monkeypatch):
    stream=Stream(socket.AF_INET,0)
    stream.client.settimeout(3)
    monkeypatch.setattr(frozen_policy,'connect',lambda *a:pytest.fail('DNS must not connect to the resolver'))
    frozen_policy.resolvers={'127.0.0.11'}
    broker=Broker(-1,frozen_policy);broker.streams[stream.inode]=stream
    responses=[]
    monkeypatch.setattr(broker,'valid',lambda ident:None)
    monkeypatch.setattr(broker,'respond',lambda ident,**kw:responses.append((ident,kw)))
    address=struct.pack('=H',socket.AF_INET)+struct.pack('!H',53)+socket.inet_aton('127.0.0.11')+b'\0'*8
    buffer=ctypes.create_string_buffer(address)
    connect_nr=42 if _architecture()[0]=='x86_64' else 203
    notification=SimpleNamespace(id=123,pid=os.getpid(),
        data=SimpleNamespace(nr=connect_nr,args=(stream.client.fileno(),ctypes.addressof(buffer),16)))
    broker.handle(notification)
    assert responses==[(123,{})]
    try:
        for kind in (1,28):
            packet=query('api.openai.com',kind)
            stream.client.sendall(struct.pack('!H',len(packet))+packet)
            size=struct.unpack('!H',read_exact(stream.client,2))[0]
            reply=read_exact(stream.client,size)
            assert reply==frozen_policy.dns_response(packet)
        stream.client.shutdown(socket.SHUT_WR)
    finally:
        stream.close()

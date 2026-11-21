"""Fail-closed whole-process TCP broker for invitation CLI/SDK children.

Children receive local stream endpoints, never Internet sockets. socket/connect
are emulated through seccomp notification; a copied sockaddr is checked and the
trusted broker connects the pinned destination. It never asks the kernel to
CONTINUE a child syscall. The same child socket OFD remains stable for epoll,
nonblocking I/O and dup aliases. Proxy credentials and TLS proxy negotiation
remain exclusively in the trusted broker. Target TLS passes through unchanged.
"""
from __future__ import annotations
import array
import base64
import ctypes
import errno
import ipaddress
import os
from pathlib import Path
import select
import socket
import ssl
import struct
import threading
from urllib.parse import unquote, urlsplit
from sandbox_seccomp import SockFilter, SockFprog, _architecture

OFFICIAL_HOSTS=('chatgpt.com','api.openai.com','auth.openai.com','sentinel.openai.com')
NOTIFY=0x7fc00000;ALLOW=0x7fff0000;KILL=0x80000000
RECV=0xc0502100;SEND=0xc0182101;VALID=0x40082102;ADDFD=0x40182103
MAX_SOCKETS=48
class Data(ctypes.Structure):
    _fields_=[('nr',ctypes.c_int32),('arch',ctypes.c_uint32),('ip',ctypes.c_uint64),('args',ctypes.c_uint64*6)]
class Notification(ctypes.Structure):
    _fields_=[('id',ctypes.c_uint64),('pid',ctypes.c_uint32),('flags',ctypes.c_uint32),('data',Data)]
class Response(ctypes.Structure):
    _fields_=[('id',ctypes.c_uint64),('val',ctypes.c_int64),('error',ctypes.c_int32),('flags',ctypes.c_uint32)]
class AddFd(ctypes.Structure):
    _fields_=[('id',ctypes.c_uint64),('flags',ctypes.c_uint32),('srcfd',ctypes.c_uint32),('newfd',ctypes.c_uint32),('newfd_flags',ctypes.c_uint32)]
LIBC=ctypes.CDLL(None,use_errno=True);LIBC.syscall.restype=ctypes.c_long
LIBC.ioctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_void_p];LIBC.ioctl.restype=ctypes.c_int

def ioctl(fd,number,item):
    result=LIBC.ioctl(fd,number,ctypes.byref(item))
    if result<0:raise OSError(ctypes.get_errno(),'Invitation network broker kernel request rejected')
    return result

def install_notifications():
    machine,(audit,socket_nr,_,seccomp_nr)=_architecture()
    connect_nr=42 if machine=='x86_64' else 203
    # Audit ABI is always checked, including rejection of x32 compatibility.
    items=[SockFilter(0x20,0,0,4),SockFilter(0x15,1,0,audit),SockFilter(6,0,0,KILL),SockFilter(0x20,0,0,0)]
    if machine=='x86_64':items += [SockFilter(0x35,0,1,0x40000000),SockFilter(6,0,0,KILL)]
    items += [SockFilter(0x15,1,0,socket_nr),SockFilter(0x15,0,1,connect_nr),SockFilter(6,0,0,NOTIFY),SockFilter(6,0,0,ALLOW)]
    filters=(SockFilter*len(items))(*items);program=SockFprog(len(items),filters)
    sizes=(ctypes.c_ushort*3)()
    if LIBC.syscall(seccomp_nr,3,0,ctypes.byref(sizes)) or tuple(sizes)!=(80,24,64):
        raise RuntimeError('Verified seccomp notification layout is required')
    result=LIBC.syscall(seccomp_nr,1,8,ctypes.byref(program))
    if result<0:raise OSError(ctypes.get_errno(),'Mandatory network notifications unavailable')
    return result

def send_listener(channel,fd):
    channel.sendmsg([b'N'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])

def receive_listener(channel):
    channel.settimeout(10)
    msg,controls,flags,_=channel.recvmsg(1,socket.CMSG_SPACE(array.array('i').itemsize))
    if msg!=b'N' or flags&socket.MSG_CTRUNC:raise RuntimeError('Missing mandatory network listener')
    descriptors=[]
    for level,kind,data in controls:
        if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
            values=array.array('i');values.frombytes(data[:len(data)//values.itemsize*values.itemsize]);descriptors.extend(values)
    if len(descriptors)!=1:
        for fd in descriptors:os.close(fd)
        raise RuntimeError('Invalid mandatory network listener')
    return descriptors[0]

def endpoint_snapshot(pid,pointer,length):
    if length not in (16,28) or not pointer:raise ValueError('Invalid network address length')
    # Only numeric kernel-supplied TIDs are used. Bytes are copied once; the child
    # cannot race validation into an actual connect of its mutable memory.
    with open('/proc/'+str(pid)+'/mem','rb',buffering=0) as source:
        data=os.pread(source.fileno(),length,pointer)
    if len(data)!=length:raise ValueError('Incomplete network address')
    family=struct.unpack_from('=H',data)[0];port=struct.unpack_from('!H',data,2)[0]
    if family==socket.AF_INET and length==16:address=socket.inet_ntop(family,data[4:8])
    elif family==socket.AF_INET6 and length==28:
        if struct.unpack_from('=I',data,24)[0]!=0:raise ValueError('Scoped IPv6 addresses are forbidden')
        address=socket.inet_ntop(family,data[8:24])
        if ipaddress.ip_address(address).ipv4_mapped:raise ValueError('Mapped IPv6 address is forbidden')
    else:raise ValueError('Unsupported network address')
    return family,address,port

class Policy:
    def __init__(self,env,enabled=True,proxy_pins=(),cafile=None):
        self.targets=set();self.resolvers=set();self.proxy=None;self.proxy_tls_context=None
        self.host_addresses={host:() for host in OFFICIAL_HOSTS}
        if not enabled:return
        for line in Path('/etc/resolv.conf').read_text().splitlines():
            fields=line.split()
            if len(fields)>=2 and fields[0]=='nameserver':
                try:self.resolvers.add(str(ipaddress.ip_address(fields[1])))
                except ValueError:raise ValueError('Invalid deployment DNS resolver')
        for host in OFFICIAL_HOSTS:
            try:addresses={row[4][0] for row in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)}
            except OSError:continue  # Offline CLI self-check remains possible.
            if addresses and all(ipaddress.ip_address(a).is_global for a in addresses):
                self.host_addresses[host]=tuple(sorted(addresses))
                self.targets.update(addresses)
        value=next((env[name] for name in ('HTTPS_PROXY','https_proxy','ALL_PROXY','all_proxy','HTTP_PROXY','http_proxy') if env.get(name)),None)
        if value:
            proxy=urlsplit(value)
            if proxy.scheme not in ('http','https','socks5','socks5h') or not proxy.hostname or not proxy.port or proxy.path not in ('','/') or proxy.query or proxy.fragment:
                raise ValueError('Invalid approved proxy')
            if proxy.port in (3306,5432,6379,11211,27017):raise ValueError('Database proxy ports are prohibited')
            # Use the exact API-validated snapshot, never a second DNS lookup.
            # Without API pins only a freshly verified wholly public answer is
            # accepted. Private proxy approval cannot be inferred from a name.
            addresses=tuple(sorted({str(ipaddress.ip_address(value)) for value in proxy_pins}))
            if not addresses:
                addresses=tuple(sorted({r[4][0] for r in socket.getaddrinfo(proxy.hostname,proxy.port,type=socket.SOCK_STREAM)}))
                if any(not ipaddress.ip_address(a).is_global for a in addresses):raise ValueError('Private proxy requires the verified deployment snapshot')
            if not addresses:raise ValueError('Cannot resolve approved proxy')
            if proxy.scheme=='https':
                # This constructor runs in the trusted parent before the child
                # exists. Snapshot trust into memory: the child may rotate its
                # own CA file, but must never change the broker's trust later.
                self.proxy_tls_context=ssl.create_default_context(cafile=cafile)
                self.proxy_tls_context.minimum_version=ssl.TLSVersion.TLSv1_2
            self.proxy=(proxy,addresses)
    def approved(self,address,port):
        return port==443 and address in self.targets or port==53 and address in self.resolvers
    def dns_response(self,packet):
        # DNS and connect use exactly the same verified startup snapshot. A
        # second CDN answer must neither become a new authority nor strand the
        # child with an address which its connect policy cannot permit.
        question=dns_question(packet)
        offset=12;labels=[]
        while question[offset]:
            length=question[offset];offset+=1
            labels.append(question[offset:offset+length].decode('ascii').lower());offset+=length
        kind,klass=struct.unpack_from('!2H',question,offset+1)
        name='.'.join(labels);version=4 if kind==1 else 6
        answers=[]
        for address in self.host_addresses[name]:
            ip=ipaddress.ip_address(address)
            if not ip.is_global or not self.approved(str(ip),443):
                raise ValueError('DNS snapshot does not match approved public destinations')
            if ip.version==version:
                answers.append(b'\xc0\x0c'+struct.pack('!HHIH',kind,klass,60,len(ip.packed))+ip.packed)
        transaction=struct.unpack_from('!H',question)[0]
        # Preserve the transaction and exact authorized question. NODATA for a
        # missing family allows normal A/AAAA lookup without an outside query.
        reply=struct.pack('!6H',transaction,0x8180,1,len(answers),0,0)+question[12:]+b''.join(answers)
        if len(reply)>65535:raise ValueError('DNS snapshot response exceeded limit')
        return reply
    def connect(self,address,port):
        if not self.approved(address,port):raise PermissionError(errno.EACCES,'Network destination denied')
        if port==53 or not self.proxy:return socket.create_connection((address,port),timeout=10)
        proxy,addresses=self.proxy
        if proxy.scheme=='https' and self.proxy_tls_context is None:
            raise RuntimeError('HTTPS proxy trust was not initialized before the task')
        conn=socket.create_connection((addresses[0],proxy.port),timeout=10)
        try:
            if proxy.scheme=='https':
                conn=self.proxy_tls_context.wrap_socket(conn,server_hostname=proxy.hostname)
            if proxy.scheme in ('http','https'):
                target=('['+address+']' if ':' in address else address)+':'+str(port)
                header='CONNECT '+target+' HTTP/1.1\r\nHost: '+target+'\r\n'
                if proxy.username is not None:
                    token=base64.b64encode((unquote(proxy.username)+':'+unquote(proxy.password or '')).encode()).decode()
                    header+='Proxy-Authorization: Basic '+token+'\r\n'
                conn.sendall((header+'\r\n').encode('ascii'));reply=bytearray()
                while not reply.endswith(b'\r\n\r\n'):
                    if len(reply)>=8192:raise ValueError('Proxy response exceeded limit')
                    byte=conn.recv(1)
                    if not byte:raise OSError('Proxy closed the tunnel')
                    reply.extend(byte)
                if not bytes(reply).split(b'\r\n',1)[0].split(b' ')[1:2]==[b'200']:raise OSError('Proxy refused the approved tunnel')
            else:
                authenticated=proxy.username is not None
                conn.sendall(b'\x05\x01'+(b'\x02' if authenticated else b'\x00'))
                if read_exact(conn,2)!=b'\x05'+(b'\x02' if authenticated else b'\x00'):raise OSError('Proxy authentication method rejected')
                if authenticated:
                    username=unquote(proxy.username).encode();password=unquote(proxy.password or '').encode()
                    if len(username)>255 or len(password)>255:raise ValueError('Proxy credential exceeded limit')
                    conn.sendall(b'\x01'+bytes([len(username)])+username+bytes([len(password)])+password)
                    if read_exact(conn,2)!=b'\x01\x00':raise OSError('Proxy authentication rejected')
                ip=ipaddress.ip_address(address);conn.sendall(b'\x05\x01\x00'+(b'\x01' if ip.version==4 else b'\x04')+ip.packed+struct.pack('!H',port))
                head=read_exact(conn,4)
                if head[:3]!=b'\x05\x00\x00':raise OSError('Proxy refused the approved destination')
                length=4 if head[3]==1 else 16 if head[3]==4 else read_exact(conn,1)[0] if head[3]==3 else -1
                if length<0:raise OSError('Invalid proxy response')
                read_exact(conn,length+2)
            return conn
        except BaseException:conn.close();raise

def read_exact(conn,length):
    value=bytearray()
    while len(value)<length:
        part=conn.recv(length-len(value))
        if not part:raise OSError('Incomplete proxy response')
        value.extend(part)
    return bytes(value)

def dns_question(packet):
    if len(packet)<12 or len(packet)>4096:raise ValueError('Invalid DNS packet size')
    transaction,flags,questions,answers,authority,additional=struct.unpack_from('!6H',packet)
    if flags&0xf800 or (questions,answers,authority)!=(1,0,0) or additional>1:raise ValueError('Invalid DNS request')
    offset=12;labels=[]
    while True:
        if offset>=len(packet):raise ValueError('Incomplete DNS question')
        length=packet[offset];offset+=1
        if length==0:break
        if length>63 or offset+length>len(packet):raise ValueError('Invalid DNS label')
        labels.append(packet[offset:offset+length].decode('ascii').lower());offset+=length
    name='.'.join(labels)
    if name not in OFFICIAL_HOSTS or len(packet)<offset+4:raise ValueError('DNS hostname is not authorized')
    kind,klass=struct.unpack_from('!2H',packet,offset)
    if kind not in (1,28) or klass!=1:raise ValueError('DNS question type is not authorized')
    # Drop EDNS/extra sections; only the fixed authorized question is forwarded.
    return struct.pack('!6H',transaction,0x0100,1,0,0,0)+packet[12:offset+4]

class Stream:
    def __init__(self,family,flags):
        listener=socket.socket(family,socket.SOCK_STREAM);listener.bind(('::1' if family==socket.AF_INET6 else '127.0.0.1',0));listener.listen(1)
        self.client=socket.socket(family,socket.SOCK_STREAM);self.client.connect(listener.getsockname())
        self.peer,_=listener.accept();listener.close()
        self.client.setblocking(not bool(flags&socket.SOCK_NONBLOCK))
        self.family=family;self.inode=os.fstat(self.client.fileno()).st_ino;self.connected=False;self.remote=None;self.closed=False
        self.lock=threading.Lock();self.finished=0
        self.client.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,65536)
    def relay(self,remote):
        self.remote=remote;self.connected=True
        def transfer(source,target):
            try:
                source.settimeout(None)
                while True:
                    data=source.recv(65536)
                    if not data:break
                    target.sendall(data)
            except OSError:pass
            finally:
                try:target.shutdown(socket.SHUT_WR)
                except OSError:pass
                with self.lock:
                    self.finished+=1
                    if self.finished==2:self.close()
        for source,target in ((self.peer,remote),(remote,self.peer)):
            threading.Thread(target=transfer,args=(source,target),daemon=True).start()
    def relay_dns(self,policy):
        try:
            self.peer.settimeout(None)
            while True:
                size=struct.unpack('!H',read_exact(self.peer,2))[0]
                if not 12<=size<=4096:raise ValueError('DNS question exceeded limit')
                query=read_exact(self.peer,size)
                reply=policy.dns_response(query)
                self.peer.sendall(struct.pack('!H',len(reply))+reply)
        except (OSError,ValueError,UnicodeError):pass
        finally:self.close()
    def abandoned(self):
        if self.closed:return True
        if self.connected:return False
        poll=select.poll();poll.register(self.peer,select.POLLHUP|getattr(select,'POLLRDHUP',0x2000))
        if poll.poll(0):self.close();return True
        return False
    def close(self):
        if self.closed:return
        self.closed=True
        for conn in (self.client,self.peer,self.remote):
            if conn:
                try:conn.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                conn.close()

class Broker:
    def __init__(self,listener,policy):self.listener=listener;self.policy=policy;self.streams={}
    def valid(self,id):ioctl(self.listener,VALID,ctypes.c_uint64(id))
    def respond(self,id,value=0,error=0):ioctl(self.listener,SEND,Response(id,value,-error,0))
    def handle(self,request):
        self.valid(request.id)
        machine,(_,socket_nr,_,_)=_architecture();connect_nr=42 if machine=='x86_64' else 203
        if request.data.nr==socket_nr:
            family,flags,protocol=(int(value)&0xffffffff for value in request.data.args[:3])
            if family not in (socket.AF_INET,socket.AF_INET6) or flags&~(socket.SOCK_NONBLOCK|socket.SOCK_CLOEXEC)!=socket.SOCK_STREAM or protocol not in (0,socket.IPPROTO_TCP):
                self.respond(request.id,error=errno.EPERM);return
            self.streams={inode:stream for inode,stream in self.streams.items() if not stream.abandoned()}
            if len(self.streams)>=MAX_SOCKETS:self.respond(request.id,error=errno.EMFILE);return
            stream=Stream(family,flags)
            try:
                ioctl(self.listener,ADDFD,AddFd(request.id,2,stream.client.fileno(),0,os.O_CLOEXEC if flags&socket.SOCK_CLOEXEC else 0))
                self.streams[stream.inode]=stream
                # Do not retain a client duplicate: the last child close must
                # produce EOF at the relay even when the task itself continues.
                stream.client.close();stream.client=None
            except BaseException:stream.close();raise
        elif request.data.nr==connect_nr:
            fd,pointer,length=(int(value) for value in request.data.args[:3])
            family,address,port=endpoint_snapshot(request.pid,pointer,length)
            self.valid(request.id)
            inode=os.stat('/proc/'+str(request.pid)+'/fd/'+str(fd)).st_ino
            stream=self.streams.get(inode)
            if stream is None or stream.family!=family:self.respond(request.id,error=errno.EACCES);return
            if not self.policy.approved(address,port):stream.close();self.respond(request.id,error=errno.EACCES);return
            if stream.connected:self.respond(request.id,error=errno.EISCONN);return
            self.valid(request.id)
            stream.connected=True
            # Never block the notification loop on DNS/TCP/proxy negotiation.
            # A single-threaded libuv client can issue many connects before it
            # sends the first TLS handshake; synchronously filling a server's
            # backlog here would deadlock that legitimate event loop.
            self.respond(request.id)
            def establish():
                try:
                    if port==53:
                        stream.relay_dns(self.policy)
                        return
                    conn=self.policy.connect(address,port)
                    if stream.closed:conn.close();return
                    stream.relay(conn)
                except (OSError,ValueError):stream.close()
            threading.Thread(target=establish,daemon=True).start()
        else:self.respond(request.id,error=errno.EPERM)
    def serve(self,child):
        poll=select.poll();poll.register(self.listener,select.POLLIN)
        try:
            while True:
                pid,status=os.waitpid(child,os.WNOHANG)
                if pid:return os.waitstatus_to_exitcode(status)
                for _,events in poll.poll(100):
                    if not events&select.POLLIN:continue
                    request=Notification()
                    try:ioctl(self.listener,RECV,request)
                    except OSError as error:
                        if error.errno in (errno.ENOENT,errno.EINTR):continue
                        raise
                    try:self.handle(request)
                    except (OSError,ValueError,IndexError):
                        try:self.respond(request.id,error=errno.EACCES)
                        except OSError:pass
        finally:
            for stream in self.streams.values():stream.close()
            os.close(self.listener)

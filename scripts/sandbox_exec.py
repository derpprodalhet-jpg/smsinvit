#!/usr/bin/env python3
"""Fail-closed Linux Landlock boundary for an entire invitation child process.

The trusted application chooses exactly one task home/work directory and fixed
read-only Sentinel files. No platform configuration, source tree, parent task,
database socket or parent-process environment is granted. This is an additional
kernel boundary, independent of the CLI's model-tool sandbox.
Reference: https://docs.kernel.org/userspace-api/landlock.html
"""
import argparse
import ctypes
import os
from pathlib import Path
import platform
import socket
import resource
import stat
import sys
from sandbox_seccomp import install_socket_policy
from sandbox_network import Policy,Broker,install_notifications,send_listener,receive_listener

EXECUTE=1;WRITE_FILE=2;READ_FILE=4;READ_DIR=8
ALL_FS=(1<<16)-1
READ=EXECUTE|READ_FILE|READ_DIR
WRITE=ALL_FS & ~(1<<6|1<<11|1<<15)  # No device creation or arbitrary device ioctl.
CREATE_RULESET=444;ADD_RULE=445;RESTRICT_SELF=446
SYSTEM_READ=('/usr','/bin','/lib','/lib64','/opt/codex','/opt/smsinvit-bin','/etc/ssl/certs','/etc/pki')
SYSTEM_FILES=('/etc/resolv.conf','/etc/hosts','/etc/nsswitch.conf','/etc/ld.so.cache','/etc/localtime','/etc/ssl/openssl.cnf','/proc/cpuinfo','/proc/meminfo','/proc/sys/kernel/random/boot_id')

class Ruleset(ctypes.Structure):
    _fields_=[('handled_access_fs',ctypes.c_uint64),('handled_access_net',ctypes.c_uint64),('scoped',ctypes.c_uint64)]
class PathRule(ctypes.Structure):
    _pack_=1
    _fields_=[('allowed_access',ctypes.c_uint64),('parent_fd',ctypes.c_int32)]
class PortRule(ctypes.Structure):
    _fields_=[('allowed_access',ctypes.c_uint64),('port',ctypes.c_uint64)]

def private_directory(value):
    path=Path(value)
    if path.is_symlink():raise ValueError('Task directory cannot be a symlink')
    path=path.resolve(strict=True)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode)&0o077 or path.stat().st_uid!=os.getuid():
        raise ValueError('Task directories must be private and owned by the runtime user')
    # An application bug may not grant a whole system/runtime directory.
    if len(path.parts)<3 or path.name in ('runtime','invitation','codex-homes','secrets','private','data'):
        raise ValueError('A unique task directory is required')
    return path

def environment(task_home,readonly_files):
    allowed={'PATH','LANG','LC_ALL','TZ','CODEX_INTERNAL_ORIGINATOR_OVERRIDE','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'}
    result={key:value for key,value in os.environ.items() if key in allowed}
    result.update(HOME=str(task_home),CODEX_HOME=str(task_home),SMSINVIT_TASK_HOME=str(task_home),TMPDIR=str(task_home/'tmp'),XDG_CACHE_HOME=str(task_home/'cache'),XDG_CONFIG_HOME=str(task_home/'config'),RES_OPTIONS='use-vc')
    sdk=os.environ.get('OPENAI_SENTINEL_SDK_FILE')
    if sdk:
        if Path(sdk).resolve() not in readonly_files:raise ValueError('Sentinel SDK requires an explicit read-only leaf')
        result['OPENAI_SENTINEL_SDK_FILE']=sdk
    return result

def apply_policy(task_home,workdir,readonly_files,child_env):
    if sys.platform!='linux' or platform.machine() not in ('x86_64','aarch64','amd64','arm64'):
        raise RuntimeError('Production invitation execution requires verified Linux Landlock')
    libc=ctypes.CDLL(None,use_errno=True);libc.syscall.restype=ctypes.c_long
    def call(number,*args):
        value=libc.syscall(number,*args)
        if value<0:raise OSError(ctypes.get_errno(),'Kernel rejected invitation sandbox policy')
        return value
    abi=call(CREATE_RULESET,0,0,1)
    if abi<6:raise RuntimeError('Landlock ABI >=6 is required; execution is refused')
    # ABI 5 guards filesystem truncation, device ioctl and TCP. ABI 6 also
    # prevents signalling unsandboxed processes and abstract Unix IPC.
    mask=ALL_FS
    attributes=Ruleset(mask,3,3)
    ruleset=call(CREATE_RULESET,ctypes.byref(attributes),ctypes.sizeof(attributes),0)
    def add_path(value,access,required=False):
        path=Path(value)
        if not path.exists():
            if required:raise ValueError('Required sandbox path is unavailable')
            return
        is_dir=path.is_dir()
        if not is_dir:access&=EXECUTE|READ_FILE|WRITE_FILE|(1<<14)|(1<<15)
        fd=os.open(path,os.O_PATH|os.O_CLOEXEC)
        try:call(ADD_RULE,ruleset,1,ctypes.byref(PathRule(access&mask,fd)),0)
        finally:os.close(fd)
    try:
        for path in SYSTEM_READ:add_path(path,READ)
        for path in SYSTEM_FILES:add_path(path,READ_FILE)
        # Only this process' proc data is readable, not its API parent/siblings.
        add_path('/proc/'+str(os.getpid()),READ)
        for path in ('/dev/null','/dev/urandom','/dev/random'):add_path(path,READ_FILE|WRITE_FILE)
        add_path(task_home,WRITE,True);add_path(workdir,WRITE,True)
        for path in readonly_files:add_path(path,READ_FILE,True)
        # No child TCP connect/bind is granted. The trusted notification broker
        # owns remote sockets and only hands the child private local streams.
        if libc.prctl(38,1,0,0,0)!=0:raise OSError(ctypes.get_errno(),'Cannot restrict child privilege escalation')
        call(RESTRICT_SELF,ruleset,0)
    finally:os.close(ruleset)
    return abi

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',required=True);parser.add_argument('--workdir',required=True);parser.add_argument('--read-file',action='append',default=[])
    parser.add_argument('--proxy-pin',action='append',default=[]);parser.add_argument('--ca-file')
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    child=None
    try:
        if os.getuid()==0 or os.geteuid()!=os.getuid():raise RuntimeError('Invitation execution requires a dedicated unprivileged user')
        if sys.platform!='linux':raise RuntimeError('Invitation execution requires verified Linux')
        capabilities={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if line.startswith(('CapEff:','CapPrm:','CapAmb:'))}
        if any(int(value,16) for value in capabilities.values()):raise RuntimeError('Invitation execution refuses elevated or ambient capabilities')
        os.umask(0o077)
        task_home=private_directory(args.home);workdir=private_directory(args.workdir)
        files=[]
        for value in args.read_file:
            path=Path(value)
            if path.is_symlink() or not path.is_file():raise ValueError('Only explicit regular read-only files are allowed')
            files.append(path.resolve())
        cafile=None
        if args.ca_file:
            path=Path(args.ca_file)
            if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024:raise ValueError('An explicit regular trusted CA leaf is required')
            cafile=str(path.resolve());files.append(path.resolve())
        command=args.command[1:] if args.command[:1]==['--'] else args.command
        if not command or not Path(command[0]).is_absolute():raise ValueError('Absolute fixed executable required')
        for path in (task_home/'tmp',task_home/'cache',task_home/'config'):path.mkdir(mode=0o700,exist_ok=True)
        child_env=environment(task_home,files)
        if cafile:child_env.update(SSL_CERT_FILE=cafile,CODEX_CA_CERTIFICATE=cafile,NODE_EXTRA_CA_CERTS=cafile)
        network=Policy(child_env,enabled=Path(command[0]).name=='codex' and command[1:2]==['exec'] and '--help' not in command[2:],proxy_pins=args.proxy_pin,cafile=cafile)
        # Preserve configured routing in the broker. Model tools cannot use a
        # proxy to tunnel to destinations outside the frozen OpenAI allowlist.
        child_env={key:value for key,value in child_env.items() if key.lower() not in ('http_proxy','https_proxy','all_proxy')}
        # Nothing inherited except standard streams may survive to a child.
        libc=ctypes.CDLL(None,use_errno=True);libc.syscall.restype=ctypes.c_long
        if libc.syscall(436,ctypes.c_uint(3),ctypes.c_uint(0xffffffff),ctypes.c_uint(0)):
            raise OSError(ctypes.get_errno(),'Cannot close all inherited descriptors')
        channel,peer=socket.socketpair()
        child=os.fork()
        if child==0:
            channel.close()
            for limit,value in ((resource.RLIMIT_NOFILE,256),(resource.RLIMIT_NPROC,256),(resource.RLIMIT_FSIZE,16*1024*1024),(resource.RLIMIT_CPU,600),(resource.RLIMIT_CORE,0)):
                resource.setrlimit(limit,(value,value))
            os.chdir(workdir)
            apply_policy(task_home,workdir,files,child_env)
            listener=install_notifications()
            send_listener(peer,listener);peer.close();os.close(listener)
            install_socket_policy()
            os.execve(command[0],command,child_env)
        peer.close()
        listener=receive_listener(channel);channel.close()
        return Broker(listener,network).serve(child)
    except Exception as error:
        if child:
            try:os.kill(child,9);os.waitpid(child,0)
            except OSError:pass
        print('Invitation sandbox rejected execution ('+type(error).__name__+').',file=sys.stderr)
        return 126

if __name__=='__main__':sys.exit(main())

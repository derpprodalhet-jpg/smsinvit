"""Verify CVE applicability facts in a disposable hardened runtime container."""
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

status={}
for line in Path('/proc/self/status').read_text().splitlines():
    if ':' in line:
        key,value=line.split(':',1);status[key]=value.strip()
assert os.getuid()==10001
assert status['NoNewPrivs']=='1' and int(status['CapEff'],16)==0 and int(status['CapBnd'],16)==0
mounts=Path('/proc/mounts').read_text().splitlines()
assert any(line.split()[1]=='/' and 'ro' in line.split()[3].split(',') for line in mounts)
absent=('/usr/bin/infocmp','/usr/bin/perl','/usr/bin/nsenter','/usr/bin/mount','/usr/bin/umount','/usr/lib/systemd/systemd-homed','/usr/bin/su','/usr/bin/passwd','/usr/bin/apt')
assert all(not Path(path).exists() for path in absent)
for directory in ('/usr/bin','/usr/sbin','/usr/lib'):
    for file in Path(directory).rglob('*'):
        if file.is_file():assert file.stat().st_mode&0o6000==0,str(file)
assert subprocess.run([sys.executable,'-c','import fastapi,sqlalchemy,greenlet,cryptography,curl_cffi,requests,PIL,pyotp,uuid,curses,readline; print(uuid.uuid4())'],capture_output=True,text=True,timeout=10).returncode==0
home=Path(tempfile.mkdtemp(prefix='cve-task-home-'));work=Path(tempfile.mkdtemp(prefix='cve-task-work-'))
protected=Path(tempfile.mkdtemp(prefix='cve-api-private-'))/'config';protected.write_text('synthetic-api-fixture')
(home/'outside-link').symlink_to(protected)
code='''import ctypes,errno,json,os
from pathlib import Path
lib=ctypes.CDLL('libacl.so.1',use_errno=True)
lib.acl_from_text.restype=ctypes.c_void_p
lib.acl_set_file.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_void_p]
lib.acl_free.argtypes=[ctypes.c_void_p]
acl=lib.acl_from_text(b'user::rw-,group::---,other::---')
assert acl
file=Path(os.environ['CODEX_HOME'])/'own-auth';file.write_text('own-token-fixture')
for path in (str(file),str(Path(os.environ['CODEX_HOME'])/'outside-link')):
 ctypes.set_errno(0)
 assert lib.acl_set_file(path.encode(),0x8000,acl)==-1
 assert ctypes.get_errno() in (errno.EPERM,errno.EACCES)
lib.acl_free(acl)
try:Path(%s).read_bytes();raise AssertionError('API fixture readable')
except PermissionError:pass
print(json.dumps({'pathAclWritesDenied':True,'protectedSymlinkTargetDenied':True,'ownTokenReadWritePreserved':file.read_text()=='own-token-fixture'}))
''' % repr(str(protected))
helper=Path(__file__).resolve().parents[1]/'sandbox_exec.py'
result=subprocess.run([sys.executable,str(helper),'--home',str(home),'--workdir',str(work),'--',sys.executable,'-c',code],capture_output=True,text=True,timeout=20)
assert result.returncode==0,result.stderr
report=json.loads(result.stdout)
for args in (['--version'],['exec','--help']):
    result=subprocess.run([sys.executable,str(helper),'--home',str(home),'--workdir',str(work),'--','/opt/codex/bin/codex',*args],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    if args==['--version']:report['cliVersion']=result.stdout.strip()
print(json.dumps({**report,'architecture':__import__('platform').machine(),'uid':os.getuid(),'noNewPrivileges':True,'effectiveAndBoundingCapabilitiesZero':True,'rootFilesystemReadOnly':True,'removedProgramsAbsent':list(absent),'setuidSetgidPrivilegesRemoved':True,'pythonNativeImportsPassed':True,'cliExecHelpPassed':True,'upstreamBusinessRequests':0,'status':'PASS'},sort_keys=True))

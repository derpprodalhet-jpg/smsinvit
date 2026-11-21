"""Run inside disposable dependency/runtime images; no real credentials/network."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

home=Path(tempfile.mkdtemp(prefix='task-home-'));work=Path(tempfile.mkdtemp(prefix='task-work-'))
other=Path(tempfile.mkdtemp(prefix='other-task-'));(other/'auth.json').write_text('other-task-fixture')
fake_config=Path(tempfile.mkdtemp(prefix='api-config-'))/'runtime.yaml';fake_config.write_text('fake-db-password')
(home/'auth.json').write_text('own-task-fixture')
(home/'outside-link').symlink_to(fake_config)
secret_fd=os.open(fake_config,os.O_RDONLY);os.set_inheritable(secret_fd,True)
os.environ['JWT_SECRET']='fake-parent-jwt';os.environ['MYSQL_PASSWORD']='fake-parent-db'
script=Path(__file__).resolve().parents[1]/'sandbox_exec.py'
code='''import errno,json,os,socket
from pathlib import Path
denied=[]
for label,path in %s:
 try:Path(path).read_bytes();raise AssertionError(label+" was readable")
 except PermissionError:denied.append(label)
assert not os.getenv("JWT_SECRET") and not os.getenv("MYSQL_PASSWORD")
assert Path(os.environ["CODEX_HOME"],"auth.json").read_text()=="own-task-fixture"
Path(os.environ["CODEX_HOME"],"auth.json").write_text("rotated-task-fixture")
Path("own-work-file").write_text("allowed")
try:os.read(%s,1);raise AssertionError("inherited secret fd was readable")
except OSError as error:assert error.errno==errno.EBADF
for port in (3306,6379,3005,13308):
 s=socket.socket();s.settimeout(1)
 try:s.connect(("127.0.0.1",port));raise AssertionError("forbidden TCP port")
 except PermissionError:pass
 finally:s.close()
for address,port in (("127.0.0.1",443),("169.254.169.254",443),("10.0.0.1",443),("127.0.0.1",53),("1.1.1.1",53)):
 s=socket.socket()
 try:s.connect((address,port));raise AssertionError("unapproved network endpoint")
 except PermissionError:pass
 finally:s.close()
for family,kind in ((socket.AF_UNIX,socket.SOCK_STREAM),(socket.AF_INET,socket.SOCK_DGRAM),(socket.AF_INET,socket.SOCK_RAW)):
 try:socket.socket(family,kind);raise AssertionError("forbidden transport")
 except PermissionError:pass
for action in (lambda:os.chmod(%s,0o777),lambda:os.utime(%s,None),lambda:os.setxattr(%s,"user.probe",b"denied"),lambda:os.setsid(),lambda:os.setpgid(0,0)):
 try:action();raise AssertionError("metadata or process group escape")
 except PermissionError:pass
print(json.dumps({"denied":denied,"privateEnvironment":True,"inheritedDescriptorsClosed":True,"ownAuthRotation":True,"ownWorkWritable":True,"tcpDatabaseAndApiDenied":True,"privateAndUnapprovedDnsDenied":True,"udpUnixRawDenied":True,"outsideMetadataDenied":True,"processGroupEscapeDenied":True}))
''' % (repr([('apiConfig',str(fake_config)),('runSecrets','/run/secrets/runtime.yaml'),('otherTaskAuth',str(other/'auth.json')),('apiProcEnvironment','/proc/'+str(os.getpid())+'/environ'),('symlinkEscape',str(home/'outside-link'))]),secret_fd,repr(str(fake_config)),repr(str(fake_config)),repr(str(fake_config)))
base=[sys.executable,str(script),'--home',str(home),'--workdir',str(work),'--']
result=subprocess.run(base+[sys.executable,'-c',code],capture_output=True,text=True,close_fds=False)
if result.returncode:print(result.stderr,file=sys.stderr);sys.exit(result.returncode)
report=json.loads(result.stdout)
os.close(secret_fd)
assert (home/'auth.json').read_text()=='rotated-task-fixture'
for args in (['--version'],['exec','--help']):
    result=subprocess.run(base+['/opt/codex/bin/codex',*args],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    if args==['--version']:report['cliVersion']=result.stdout.strip()
report['cliExecHelp']=True
nodecode='''const fs=require("fs");for (const path of %s){try{fs.readFileSync(path);throw new Error("escaped")}catch(e){if(e.code!=="EACCES")throw e}}console.log("node-isolated");''' % json.dumps([str(fake_config),str(other/'auth.json'),'/proc/'+str(os.getpid())+'/environ'])
result=subprocess.run(base+['/opt/smsinvit-bin/node','-e',nodecode],capture_output=True,text=True)
assert result.returncode==0 and result.stdout.strip()=='node-isolated',result.stderr
report['nodeWholeProcessIsolated']=True
import platform
report.update(architecture=platform.machine(),status='PASS',upstreamBusinessRequests=0)
print(json.dumps(report,sort_keys=True))

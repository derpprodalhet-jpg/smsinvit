"""Adversarial high-FD, O_PATH and signal checks with synthetic local secrets."""
import errno
import fcntl
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

home = Path(tempfile.mkdtemp(prefix="edge-home-"))
work = Path(tempfile.mkdtemp(prefix="edge-work-"))
outside = Path(tempfile.mkdtemp(prefix="edge-api-")) / "runtime.yaml"
outside.write_text("synthetic-api-secret")
original_mode = outside.stat().st_mode
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
assert hard >= 70001, "The isolated high-FD fixture requires an adequate fd limit"
resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 70001), hard))
descriptor = os.open(outside, os.O_RDONLY)
high = fcntl.fcntl(descriptor, fcntl.F_DUPFD, 70000)
os.set_inheritable(high, True)
script = Path(__file__).resolve().parents[1] / "sandbox_exec.py"
code = """import errno,json,os
from pathlib import Path
try:os.read(%r,1);raise AssertionError('high fd survived')
except OSError as e:assert e.errno==errno.EBADF
pathfd=os.open(%r,os.O_PATH)
try:
 try:Path('/proc/self/fd/'+str(pathfd)).read_bytes();raise AssertionError('O_PATH proc escaped')
 except PermissionError:pass
 try:os.fchmod(pathfd,0o777);raise AssertionError('O_PATH chmod escaped')
 except OSError as e:assert e.errno==errno.EBADF
finally:os.close(pathfd)
try:os.kill(%r,0);raise AssertionError('parent signalling escaped')
except PermissionError:pass
os.kill(os.getpid(),0)
print(json.dumps({'status':'PASS','architecture':__import__('platform').machine(),'inheritedFd70000Closed':True,'oPathProcReadDenied':True,'oPathFchmodRejected':True,'parentSignalDenied':True,'selfSignalAllowed':True,'realUpstreamBusinessRequests':0},sort_keys=True))
""" % (high, str(outside), os.getpid())
try:
    result = subprocess.run(
        [sys.executable, str(script), "--home", str(home), "--workdir", str(work),
         "--", sys.executable, "-c", code],
        close_fds=False, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert outside.read_text() == "synthetic-api-secret" and outside.stat().st_mode == original_mode
    print(result.stdout.strip())
finally:
    os.close(high); os.close(descriptor)

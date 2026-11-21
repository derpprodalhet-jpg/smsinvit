"""Real glibc DNS through the kernel sandbox; no external sockets or tokens.

Only the trusted Policy constructor's getaddrinfo is mocked. The child performs
ordinary getaddrinfo calls under the production Landlock/seccomp/broker boundary
and receives A/AAAA answers from the verified snapshot over local TCP streams.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


SCRIPTS = Path(__file__).resolve().parents[1]
EXPECTED = ['8.8.8.8', '2001:4860:4860::8888']
HOSTS = ['chatgpt.com', 'api.openai.com', 'auth.openai.com', 'sentinel.openai.com']

wrapper = '''import errno,socket,sys
from unittest.mock import patch
sys.path.insert(0,%r)
import sandbox_exec
from sandbox_network import Policy,OFFICIAL_HOSTS
class FrozenFixturePolicy(Policy):
 def __init__(self,*args,**kwargs):
  def snapshot(host,port,**options):
   assert host in OFFICIAL_HOSTS and port==443
   return [(socket.AF_INET,socket.SOCK_STREAM,6,"",("8.8.8.8",port)),
           (socket.AF_INET6,socket.SOCK_STREAM,6,"",("2001:4860:4860::8888",port,0,0))]
  with patch("socket.getaddrinfo",snapshot):super().__init__({},enabled=True)
  assert self.targets=={"8.8.8.8","2001:4860:4860::8888"}
 def connect(self,*args):
  raise OSError(errno.EACCES,"The DNS fixture forbids every remote connection")
sandbox_exec.Policy=FrozenFixturePolicy
sys.exit(sandbox_exec.main())
''' % str(SCRIPTS)

child = '''import os,socket,json
assert os.environ.get("RES_OPTIONS")=="use-vc"
assert not any(name.lower() in ("http_proxy","https_proxy","all_proxy") for name in os.environ)
expected={socket.AF_INET:{"8.8.8.8"},socket.AF_INET6:{"2001:4860:4860::8888"}}
lookups=[]
for host in %r:
 for family,addresses in expected.items():
  records=socket.getaddrinfo(host,443,family=family,type=socket.SOCK_STREAM)
  assert {record[4][0] for record in records}==addresses
  lookups.append((host,"A" if family==socket.AF_INET else "AAAA"))
print(json.dumps({"status":"PASS","glibcLookups":lookups,"useVc":True,"childProxyEnvironmentRemoved":True}))
''' % HOSTS

with tempfile.TemporaryDirectory(prefix='dns-probe-home-') as home, \
        tempfile.TemporaryDirectory(prefix='dns-probe-work-') as work:
    # Parent env deliberately has a proxy; the fixture Policy ignores it, and
    # sandbox_exec must still remove it from the final child environment.
    env = {**os.environ, 'HTTPS_PROXY': 'http://fixture-unused.example:8080'}
    result = subprocess.run(
        [sys.executable, '-c', wrapper, '--home', home, '--workdir', work,
         '--', sys.executable, '-c', child], env=env, capture_output=True,
        text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:] + result.stdout[-1000:]
    evidence = json.loads(result.stdout)
    assert evidence['status'] == 'PASS' and len(evidence['glibcLookups']) == 8
    print(json.dumps({
        **evidence, 'architecture': __import__('platform').machine(),
        'realKernelSandbox': True, 'verifiedSnapshotAddresses': EXPECTED,
        'externalNetworkConnections': 0, 'realUpstreamBusinessRequests': 0,
    }, sort_keys=True))

"""Minimal child environment; copied engine cannot inherit application credentials."""
import os
from pathlib import Path
import shutil
import subprocess as _subprocess
import sys
import tempfile
import signal
import threading
import re
import ssl
from contextvars import ContextVar
from .safety import child_environment, require_binary

EXTRA={'OPENAI_SENTINEL_SDK_FILE','CODEX_HOME','SMSINVIT_TASK_HOME','CODEX_INTERNAL_ORIGINATOR_OVERRIDE','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'}
PROXY_PRIORITY=('HTTPS_PROXY','https_proxy','ALL_PROXY','all_proxy','HTTP_PROXY','http_proxy')
MAX_CHILD_OUTPUT=4*1024*1024
MAX_CA_BUNDLE=1024*1024
UNCERTAIN_PROCESS=ContextVar('invitation_uncertain_child_process',default=False)

def managed_homes_root():
    """Deployment authority for all credential-bearing CLI temporary files."""
    value=os.environ.get('CODEX_HOMES_ROOT','').strip()
    if not value:raise ValueError('The managed Codex homes root is not configured')
    return Path(value).expanduser().resolve()

def managed_home_directory(value):
    """A setting/task may select a child, never the shared root or its parent."""
    path=Path(value).expanduser()
    resolved=path.resolve();root=managed_homes_root()
    if path.is_symlink() or resolved==root or not resolved.is_relative_to(root):
        raise ValueError('CLI task directory must be a private child of the managed Codex homes root')
    return resolved

def private_snapshot(home,name,data):
    """Grant children task-owned copies, never writable shared source files."""
    target=home/name
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as file:file.write(data)
    return target

def certificate_bundle(path):
    if path.is_symlink() or not path.is_file():raise ValueError('The invitation CA must be one regular file')
    raw=path.read_bytes()
    if len(raw)>MAX_CA_BUNDLE:raise ValueError('The invitation CA bundle exceeds the file limit')
    try:
        text=raw.decode('ascii')
        if not re.fullmatch(r'\s*(?:-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*)+',text):raise ValueError('Only PEM certificates may be supplied as the invitation CA')
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=text)
    except (UnicodeError,ssl.SSLError) as exc:raise ValueError('The invitation CA must be a valid PEM certificate bundle') from exc
    return raw

def run_child(command,**kwargs):
    """Bound output and terminate the entire task group before releasing a permit."""
    if os.name!='posix':raise RuntimeError('Invitation execution requires the verified Linux process boundary')
    payload=kwargs.pop('input',None);timeout=kwargs.pop('timeout',None);check=kwargs.pop('check',False)
    capture=kwargs.pop('capture_output',False)
    if capture:
        if 'stdout' in kwargs or 'stderr' in kwargs:raise ValueError('Duplicate output capture options')
        kwargs.update(stdout=_subprocess.PIPE,stderr=_subprocess.PIPE)
    if kwargs.pop('stdout',None)!=_subprocess.PIPE or kwargs.pop('stderr',None)!=_subprocess.PIPE:raise ValueError('Invitation output must be privately captured')
    if timeout is None or not 0<float(timeout)<=3600:raise ValueError('Invitation execution requires a bounded timeout')
    if payload is not None:
        if 'stdin' in kwargs:raise ValueError('Duplicate stdin options')
        kwargs['stdin']=_subprocess.PIPE
    else:kwargs.setdefault('stdin',_subprocess.DEVNULL)
    if kwargs.pop('start_new_session',True) is not True or 'process_group' in kwargs:raise ValueError('A dedicated task process group is required')
    text=bool(kwargs.get('text') or kwargs.get('universal_newlines') or kwargs.get('encoding') or kwargs.get('errors'))
    encoding=kwargs.get('encoding') or 'utf-8';errors=kwargs.get('errors') or 'strict'
    with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
        proc=_subprocess.Popen(command,stdout=stdout,stderr=stderr,start_new_session=True,**kwargs)
        stop=threading.Event();oversized=threading.Event();unsafe=threading.Event();kill_lock=threading.Lock();signalled=False
        def kill_group():
            nonlocal signalled
            with kill_lock:
                if signalled:return
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:signalled=True
                except OSError:unsafe.set()
                else:signalled=True
        def watch_output():
            while not stop.wait(.02):
                if any(os.fstat(file.fileno()).st_size>MAX_CHILD_OUTPUT for file in (stdout,stderr)):
                    oversized.set();kill_group();return
        monitor=threading.Thread(target=watch_output,daemon=True);monitor.start()
        try:
            try:proc.communicate(payload,timeout=float(timeout))
            except BaseException:
                kill_group();proc.wait(timeout=5);raise
        finally:
            # Normal exit can also leave spawned children behind. They retain the
            # same group and inherited Landlock policy, and must finish before the
            # database permit is made available to another task.
            kill_group();stop.set();monitor.join(timeout=1)
            if unsafe.is_set():UNCERTAIN_PROCESS.set(True)
        output=[]
        for file in (stdout,stderr):
            file.seek(0);data=file.read(MAX_CHILD_OUTPUT+1)
            if oversized.is_set() or len(data)>MAX_CHILD_OUTPUT:raise RuntimeError('Invitation child output exceeded the response limit')
            output.append(data.decode(encoding,errors) if text else data)
        if unsafe.is_set():raise RuntimeError('Invitation child termination could not be verified')
        result=_subprocess.CompletedProcess(command,proc.returncode,*output)
        if check and proc.returncode:raise _subprocess.CalledProcessError(proc.returncode,command,output=output[0],stderr=output[1])
        return result

class SafeSubprocess:
    def __getattr__(self,name):
        value=getattr(_subprocess,name)
        if name.isupper() or isinstance(value,type) and issubclass(value,BaseException):return value
        raise AttributeError('Only guarded process entry points are available')
    def _run(self,method,args,**kwargs):
        if kwargs.get('shell'):raise ValueError('Shell execution is not allowed in invitation workers')
        if not isinstance(args,(list,tuple)) or not args:raise ValueError('Explicit process argv required')
        binary=shutil.which(str(args[0])) or str(args[0]);require_binary(binary)
        env=child_environment();supplied=kwargs.pop('env',None) or {}
        env.update({k:str(v) for k,v in supplied.items() if k in EXTRA})
        task_home=env.get('SMSINVIT_TASK_HOME')
        if task_home:
            home=managed_home_directory(task_home)
            if not home.is_dir():raise ValueError('Child task home must be managed')
            env['HOME']=str(home)
        from .safety import require_proxy
        proxy_snapshots={}
        for name in PROXY_PRIORITY:
            value=env.get(name)
            if value and value not in proxy_snapshots:proxy_snapshots[value]=require_proxy(value)
        selected_proxy=next((env[name] for name in PROXY_PRIORITY if env.get(name)),None)
        proxy_pins=proxy_snapshots[selected_proxy][2] if selected_proxy else ()
        helper=Path(__file__).resolve().parents[4]/'scripts/sandbox_exec.py'
        if not helper.is_file() or helper.is_symlink():raise RuntimeError('The mandatory invitation sandbox helper is unavailable')
        if kwargs.get('pass_fds') or kwargs.get('preexec_fn') or kwargs.get('close_fds') is False:raise ValueError('Child descriptor or privilege overrides are forbidden')
        if method!='run':raise ValueError('Only bounded invitation child execution is supported')
        vendor=Path(__file__).resolve().parents[4]/'vendor/codex_invit'
        created=[];readonly=[];snapshots=[]
        try:
            node=Path(binary).name in ('node','node.exe');version=list(args[1:])==['--version']
            if node:
                script=vendor/'openai_sentinel_quickjs.js'
                if not version:
                    if len(args)!=2 or Path(args[1]).resolve()!=script.resolve() or script.is_symlink():raise ValueError('Only the fixed Sentinel Node program is supported')
                    sdk=Path(env.get('OPENAI_SENTINEL_SDK_FILE',''))
                    cache=Path(tempfile.gettempdir())/'openai-sentinel-demo'
                    if sdk.is_symlink() or not sdk.is_file() or not sdk.resolve().is_relative_to(cache.resolve()) or sdk.stat().st_size>4*1024*1024:raise ValueError('Invalid fixed Sentinel SDK file')
                    sdk_data=sdk.read_bytes()
                    if len(sdk_data)>4*1024*1024:raise ValueError('Invalid fixed Sentinel SDK file')
                    script_data=script.read_bytes()
                    if len(script_data)>4*1024*1024:raise ValueError('Invalid fixed Sentinel script')
                root=managed_homes_root();root.mkdir(parents=True,exist_ok=True,mode=0o700)
                home=Path(tempfile.mkdtemp(prefix='sentinel-home-',dir=root));created.append(home)
                work=Path(tempfile.mkdtemp(prefix='sentinel-work-',dir=root));created.append(work)
                if not version:
                    snapshot=private_snapshot(home,'sdk.js',sdk_data);readonly.append(snapshot)
                    env['OPENAI_SENTINEL_SDK_FILE']=str(snapshot)
                    args=[args[0],str(private_snapshot(home,'sentinel.js',script_data))]
                # Sentinel is offline and receives no shared CA/file authority.
                for name in ('SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE'):env.pop(name,None)
            else:
                if not task_home or len(args)<2 or args[1]!='exec' and not version:raise ValueError('A managed Codex task is required')
                work=managed_home_directory(kwargs.get('cwd',''))
                if not work.is_dir() or work.is_symlink() or work==home or work.name in ('data','invitation','runtime','codex-homes'):raise ValueError('An independent private task work directory is required')
            command=[sys.executable,str(helper),'--home',str(home),'--workdir',str(work)]
            for file in readonly:command.extend(('--read-file',str(file.resolve())))
            if not node:
                for address in proxy_pins:command.extend(('--proxy-pin',address))
                ca=os.environ.get('SMSINVIT_INVITATION_CA_BUNDLE') or env.get('SSL_CERT_FILE')
                if ca:
                    path=private_snapshot(home,'trusted-ca.pem',certificate_bundle(Path(ca)));snapshots.append(path)
                    env['SSL_CERT_FILE']=str(path)
                    env.pop('SSL_CERT_DIR',None)
                    for name in ('REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE'):
                        if name in env:env[name]=str(path)
                    command.extend(('--ca-file',str(path)))
            command.extend(('--',binary,*args[1:]))
            kwargs.update(env=env,cwd=str(work),close_fds=True)
            # The kernel policy is applied before exec; unsupported systems fail
            # closed with exit126. No direct CLI/Node fallback is available.
            return run_child(command,**kwargs)
        finally:
            for file in snapshots:file.unlink(missing_ok=True)
            for directory in created:shutil.rmtree(directory,ignore_errors=True)
    def run(self,args,**kwargs):return self._run('run',args,**kwargs)
    def check_output(self,args,**kwargs):return self._run('check_output',args,**kwargs)
    def check_call(self,args,**kwargs):return self._run('check_call',args,**kwargs)
    def Popen(self,args,**kwargs):return self._run('Popen',args,**kwargs)
subprocess=SafeSubprocess()

def execution_self_check(data_dir,codex_binary='codex'):
    """Refuse workers before any billable action if the kernel/runtime is unusable."""
    root=managed_homes_root();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    home=Path(tempfile.mkdtemp(prefix='preflight-home-',dir=root))
    work=Path(tempfile.mkdtemp(prefix='preflight-work-',dir=root))
    try:
        env={'SMSINVIT_TASK_HOME':str(home),'CODEX_HOME':str(home)}
        binary=shutil.which(str(codex_binary)) or str(codex_binary)
        node=shutil.which(os.environ.get('OPENAI_SENTINEL_NODE_PATH') or 'node') or os.environ.get('OPENAI_SENTINEL_NODE_PATH') or 'node'
        for program,pattern in ((binary,r'codex-cli [0-9]+\.[0-9]+\.[0-9]+'),(node,r'v[0-9]+\.[0-9]+\.[0-9]+')):
            result=subprocess.run([program,'--version'],env=env,cwd=work,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            if result.returncode or not re.fullmatch(pattern,result.stdout.strip()):raise RuntimeError('Invitation sandbox or required runtime self-check failed')
        return True
    finally:
        shutil.rmtree(home,ignore_errors=True);shutil.rmtree(work,ignore_errors=True)

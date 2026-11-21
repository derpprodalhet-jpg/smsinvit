"""Mocked child execution verifies mandatory kernel-helper invocation without CLI/network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest
from smsinvit.invitation import process

ROOT=Path(__file__).resolve().parents[3]
@pytest.fixture
def child(tmp_path,monkeypatch):
    binary=tmp_path/'codex';binary.write_text('# mock executable');binary.chmod(0o700)
    homes=tmp_path/'private/codex-homes';homes.mkdir(parents=True,mode=0o700)
    monkeypatch.setenv('CODEX_HOMES_ROOT',str(homes));monkeypatch.setenv('SMSINVIT_INVITATION_BINARY_ROOTS',str(tmp_path))
    monkeypatch.setenv('MYSQL_PASSWORD','database-private-value');monkeypatch.setenv('JWT_SECRET','jwt-private-value');monkeypatch.setenv('SMSINVIT_CONFIG_FILE','/run/secrets/runtime.yaml')
    calls=[]
    def fake(argv,**kwargs):calls.append((argv,kwargs));return subprocess.CompletedProcess(argv,126,'','Kernel unsupported')
    monkeypatch.setattr(process,'run_child',fake)
    return binary,homes,calls

def test_codex_always_uses_fixed_helper_minimal_env_and_no_fallback(child,tmp_path):
    binary,homes,calls=child;home=homes/'task-example';home.mkdir(mode=0o700);work=homes/'work-task';work.mkdir(mode=0o700)
    result=process.subprocess.run([str(binary),'exec','--sandbox','read-only','--','hello'],env={'SMSINVIT_TASK_HOME':str(home),'CODEX_HOME':str(home),'MYSQL_PASSWORD':'bad','JWT_SECRET':'bad'},cwd=work,timeout=20,capture_output=True,text=True)
    assert result.returncode==126 and len(calls)==1
    argv,kwargs=calls[0]
    assert argv[:2]==[sys.executable,str(ROOT/'scripts/sandbox_exec.py')]
    assert argv[2:6]==['--home',str(home),'--workdir',str(work)]
    assert argv[6:9]==['--',str(binary),'exec'] and kwargs['close_fds'] is True
    assert kwargs['env']['HOME']==str(home)
    assert not {'MYSQL_PASSWORD','JWT_SECRET','SMSINVIT_CONFIG_FILE'}&kwargs['env'].keys()
    assert kwargs['timeout']==20

def test_process_override_and_async_bypass_rejected_before_spawn(child,tmp_path):
    binary,homes,calls=child
    for kwargs in ({'shell':True},{'pass_fds':(9,)},{'preexec_fn':lambda:None},{'close_fds':False}):
        with pytest.raises(ValueError):process.subprocess.run([str(binary),'exec'],**kwargs)
    with pytest.raises(ValueError):process.subprocess.Popen([str(binary),'exec'])
    with pytest.raises(AttributeError):process.subprocess.call
    assert not calls

@pytest.mark.parametrize('location',['root','parent','sibling','symlink'])
def test_cli_rejects_shared_root_and_outside_home_before_spawn(child,tmp_path,location):
    binary,homes,calls=child;work=homes/'work';work.mkdir()
    home={'root':homes,'parent':homes.parent,'sibling':homes.parent/'other-task','symlink':homes/'linked'}[location]
    if location=='sibling':home.mkdir()
    if location=='symlink':home.symlink_to(homes.parent,target_is_directory=True)
    with pytest.raises(ValueError,match='private child'):
        process.subprocess.run([str(binary),'exec','--','hello'],env={'SMSINVIT_TASK_HOME':str(home)},cwd=work,capture_output=True,text=True,timeout=10)
    assert not calls

def test_cli_work_directory_must_share_the_bounded_homes_mount(child,tmp_path):
    binary,homes,calls=child;home=homes/'private-home';home.mkdir();outside=tmp_path/'outside-work';outside.mkdir()
    for work in (homes,homes.parent,outside):
        with pytest.raises(ValueError,match='private child'):
            process.subprocess.run([str(binary),'exec','--','hello'],env={'SMSINVIT_TASK_HOME':str(home)},cwd=work,capture_output=True,text=True,timeout=10)
    assert not calls

def test_copied_home_writer_preserves_default_and_rejects_unsafe_custom_paths(child,tmp_path):
    binary,homes,calls=child
    spec=importlib.util.spec_from_file_location('isolated_homes_codex_turn',ROOT/'vendor/codex_invit/webui/codex_turn.py');turn=importlib.util.module_from_spec(spec);spec.loader.exec_module(turn)
    for path in (homes,homes.parent,homes.parent/'outside-custom'):
        token=turn._OVERRIDES.set({'CODEX_HOMES_ROOT':str(path)})
        try:
            with pytest.raises(ValueError,match='private child'):turn._write_home('test@example.test','access','refresh','id')
        finally:turn._OVERRIDES.reset(token)
    assert not any(homes.iterdir()) and not (homes.parent/'outside-custom').exists()
    # The empty setting still uses the exact deployment root; custom roots may
    # partition its bounded mount without moving credential files outside it.
    for custom in ('',str(homes/'chosen')):
        token=turn._OVERRIDES.set({'CODEX_HOMES_ROOT':custom})
        try:home=turn._write_home('test@example.test','access','refresh','id')
        finally:turn._OVERRIDES.reset(token)
        assert home.is_relative_to(homes) and home!=homes
        assert home.parent==Path(custom) if custom else home.parent==homes
        turn._cleanup_home(home)
    assert not any(p.name=='auth.json' for p in homes.rglob('*'))

def test_node_only_reads_fixed_script_and_sdk_in_unique_private_directories(child,tmp_path,monkeypatch):
    binary,homes,calls=child;node=tmp_path/'node';node.write_text('# node fixture');node.chmod(0o700)
    monkeypatch.setattr(process.tempfile,'gettempdir',lambda:str(tmp_path))
    cache=tmp_path/'openai-sentinel-demo/version';cache.mkdir(parents=True);sdk=cache/'sdk.js';sdk.write_text('public SDK fixture')
    script=ROOT/'vendor/codex_invit/openai_sentinel_quickjs.js'
    shared_mode=sdk.stat().st_mode
    def fake(argv,**kwargs):
        calls.append((argv,kwargs));copied=Path(kwargs['env']['OPENAI_SENTINEL_SDK_FILE'])
        assert copied.parent==Path(argv[3]) and copied.read_text()=='public SDK fixture'
        assert copied.stat().st_mode&0o077==0 and str(sdk) not in argv
        script_copy=Path(argv[-1])
        assert script_copy.parent==Path(argv[3]) and script_copy.name=='sentinel.js'
        assert script_copy.read_bytes()==script.read_bytes() and script_copy.stat().st_mode&0o077==0
        assert str(script) not in argv
        assert argv[argv.index('--read-file')+1]==str(copied)
        with copied.open('rb') as stream:
            import os
            os.fchmod(stream.fileno(),0o000)
        return subprocess.CompletedProcess(argv,0,'{}','')
    monkeypatch.setattr(process,'run_child',fake)
    process.subprocess.run([str(node),str(script)],env={'OPENAI_SENTINEL_SDK_FILE':str(sdk)},input='{}',text=True,capture_output=True,timeout=10)
    argv,kwargs=calls[0];home=Path(argv[3]);work=Path(argv[5])
    assert home!=work and not home.exists() and not work.exists()
    assert home.is_relative_to(homes) and work.is_relative_to(homes) and home!=homes and work!=homes
    assert argv.count('--read-file')==1 and Path(argv[argv.index('--read-file')+1]).parent==home and Path(argv[-1]).name=='sentinel.js'
    assert '--' in argv and Path(kwargs['env']['OPENAI_SENTINEL_SDK_FILE']).parent==home
    assert sdk.read_text()=='public SDK fixture' and sdk.stat().st_mode==shared_mode
    with pytest.raises(ValueError):process.subprocess.run([str(node),'-e','require("fs").readFileSync("/run/secrets/runtime.yaml")'],env={'OPENAI_SENTINEL_SDK_FILE':str(sdk)})
    assert len(calls)==1

def test_copied_turn_keeps_token_rotation_and_prompt_is_data_after_separator(child,monkeypatch):
    binary,homes,calls=child
    spec=importlib.util.spec_from_file_location('isolated_codex_turn',ROOT/'vendor/codex_invit/webui/codex_turn.py');turn=importlib.util.module_from_spec(spec);spec.loader.exec_module(turn)
    monkeypatch.setattr(turn,'find_codex_binary',lambda:str(binary))
    def fake(cmd,*,env,cwd,timeout,**kwargs):
        calls.append((cmd,env));home=Path(env['CODEX_HOME']);auth=json.loads((home/'auth.json').read_text())
        assert auth['tokens']['access_token']=='original-'+cmd[-1]
        assert Path(cwd).is_relative_to(homes) and home.is_relative_to(homes)
        assert home!=homes and Path(cwd)!=homes
        config=(home/'config.toml').read_text();assert 'sandbox_mode = "read-only"' in config
        # Model newlines remain in a quoted TOML string; the trusted fixed flags
        # stay mandatory even for imported configuration.
        assert '\\n' in config and cmd[-2]=='--'
        auth['tokens']['access_token']='rotated-'+cmd[-1];(home/'auth.json').write_text(json.dumps(auth))
        return ('\n'.join(json.dumps(event) for event in [
            {'type':'turn.started'}, {'type':'item.completed','item':{'type':'agent_message','text':'hello'}}, {'type':'turn.completed'}
        ])+'\n','',0,False,{})
    monkeypatch.setattr(turn,'_run_codex_process',fake)
    def run(prompt):return turn.run_codex_turn(email=prompt.strip('-')+'@example.test',access_token='original-'+prompt,refresh_token='private-refresh',overrides={'CODEX_REDEEM_MODEL':'model\nsandbox_mode="danger-full-access"','CODEX_REDEEM_PROMPT':prompt})
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,['--dangerously-bypass-approvals-and-sandbox','another-prompt']))
    assert all(r['ok'] and r['reply']=='hello' for r in results)
    assert {r['access_token'] for r in results}=={'rotated---dangerously-bypass-approvals-and-sandbox','rotated-another-prompt'}
    assert len(calls)==2 and len({r['home'] for r in results})==2
    assert not any(homes.iterdir())

def test_rotated_opaque_credentials_are_redacted_before_reply_logging(child,monkeypatch):
    from smsinvit.invitation.safety import redact
    from smsinvit.invitation import mysql
    monkeypatch.setattr(mysql,'_redaction_values',set())
    binary,homes,calls=child;rotated='new-opaque-rotated-refresh-value'
    spec=importlib.util.spec_from_file_location('isolated_rotation_codex_turn',ROOT/'vendor/codex_invit/webui/codex_turn.py');turn=importlib.util.module_from_spec(spec);spec.loader.exec_module(turn)
    monkeypatch.setattr(turn,'find_codex_binary',lambda:str(binary));seen=[]
    def fake(cmd,*,env,cwd,timeout,**kwargs):
        home=Path(env['CODEX_HOME']);path=home/'auth.json';auth=json.loads(path.read_text());auth['tokens']['refresh_token']=rotated;path.write_text(json.dumps(auth))
        return ('\n'.join(json.dumps(event) for event in [
            {'type':'turn.started'}, {'type':'item.completed','item':{'type':'agent_message','text':rotated}}, {'type':'turn.completed'}
        ])+'\n','',0,False,{})
    def info(message,*args):seen.append(redact(message%args))
    monkeypatch.setattr(turn,'_run_codex_process',fake);monkeypatch.setattr(turn.logger,'info',info)
    result=turn.run_codex_turn(email='test@example.test',access_token='old-access-value',refresh_token='old-refresh-value')
    assert result['ok'] and result['refresh_token']==rotated and result['reply']==rotated
    assert any('[REDACTED]' in line for line in seen) and all(rotated not in line for line in seen)
    assert not any(homes.iterdir())

@pytest.mark.parametrize('timeout',[.1,5])
def test_complete_child_group_is_killed_on_timeout_and_normal_parent_exit(tmp_path,timeout):
    import time
    marker=tmp_path/'unwanted-external-action'
    grandchild='import time;from pathlib import Path;time.sleep(.6);Path('+repr(str(marker))+').write_text("unexpected")'
    code='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(grandchild)+'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'+('time.sleep(10)' if timeout<1 else 'sys.exit(0)')
    kwargs=dict(capture_output=True,text=True,timeout=timeout)
    if timeout<1:
        with pytest.raises(subprocess.TimeoutExpired):process.run_child([sys.executable,'-c',code],**kwargs)
    else:assert process.run_child([sys.executable,'-c',code],**kwargs).returncode==0
    time.sleep(.7)
    assert not marker.exists()

def test_child_output_limit_never_returns_large_response():
    code='import sys;sys.stdout.buffer.write(b"x"*(4*1024*1024+1000));sys.stdout.flush()'
    with pytest.raises(RuntimeError,match='output exceeded'):process.run_child([sys.executable,'-c',code],capture_output=True,text=True,timeout=5)

def test_proxy_snapshot_precedence_is_passed_once_without_new_dns_lookup(child,tmp_path,monkeypatch):
    from smsinvit.invitation import safety
    binary,homes,calls=child;home=homes/'task-proxy';home.mkdir(mode=0o700);work=homes/'task-work';work.mkdir(mode=0o700)
    checks=[]
    def check(url):
        checks.append(url)
        return ('proxy.example.test',8080,('8.8.8.8','2001:4860:4860::8888')) if url=='https://selected.example.test:8080' else ('other.example.test',8081,('1.1.1.1',))
    monkeypatch.setattr(safety,'require_proxy',check)
    env={'SMSINVIT_TASK_HOME':str(home),'HTTPS_PROXY':'https://selected.example.test:8080','https_proxy':'http://alternate.example.test:8081','ALL_PROXY':'https://selected.example.test:8080'}
    process.subprocess.run([str(binary),'exec','--','hello'],env=env,cwd=work,capture_output=True,text=True,timeout=30)
    argv=calls[0][0];pins=[argv[i+1] for i,value in enumerate(argv) if value=='--proxy-pin']
    assert pins==['8.8.8.8','2001:4860:4860::8888'] and checks==['https://selected.example.test:8080','http://alternate.example.test:8081']

@pytest.mark.parametrize('setting',['SMSINVIT_INVITATION_CA_BUNDLE','SSL_CERT_FILE'])
def test_private_ca_only_grants_one_non_symlink_leaf(child,tmp_path,monkeypatch,setting):
    binary,homes,calls=child;home=homes/'task-ca';home.mkdir(mode=0o700);work=homes/'task-work';work.mkdir(mode=0o700)
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME,'Isolated CA fixture')])
    now=datetime.now(timezone.utc)
    certificate=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
    ca=tmp_path/'private/ca.pem';ca.write_bytes(certificate);monkeypatch.setenv(setting,str(ca));shared_mode=ca.stat().st_mode
    def fake(argv,**kwargs):
        calls.append((argv,kwargs));copied=Path(argv[argv.index('--ca-file')+1])
        assert copied==home/'trusted-ca.pem' and copied.read_bytes()==certificate
        assert copied.stat().st_mode&0o077==0 and kwargs['env']['SSL_CERT_FILE']==str(copied)
        assert str(ca) not in argv and str(ca) not in kwargs['env'].values()
        with copied.open('rb') as stream:
            import os
            os.fchmod(stream.fileno(),0o000)
        return subprocess.CompletedProcess(argv,0,'','')
    monkeypatch.setattr(process,'run_child',fake)
    process.subprocess.run([str(binary),'exec','--','hello'],env={'SMSINVIT_TASK_HOME':str(home)},cwd=work,capture_output=True,text=True,timeout=30)
    argv=calls[0][0]
    assert argv[argv.index('--ca-file')+1]==str(home/'trusted-ca.pem')
    assert ca.read_bytes()==certificate and ca.stat().st_mode==shared_mode and not (home/'trusted-ca.pem').exists()
    assert '--read-file' not in argv
    link=tmp_path/'link.pem';link.symlink_to(ca);monkeypatch.setenv(setting,str(link))
    with pytest.raises(ValueError,match='regular file'):process.subprocess.run([str(binary),'exec','--','hello'],env={'SMSINVIT_TASK_HOME':str(home)},cwd=work,capture_output=True,text=True,timeout=30)
    monkeypatch.setenv(setting,str(ca));ca.write_text('-----BEGIN PRIVATE KEY-----\nAAA=\n-----END PRIVATE KEY-----\n')
    with pytest.raises(ValueError,match='Only PEM certificates'):process.subprocess.run([str(binary),'exec','--','hello'],env={'SMSINVIT_TASK_HOME':str(home)},cwd=work,capture_output=True,text=True,timeout=30)
    ca.write_bytes(certificate+b'\n'*(1024*1024+1))
    with pytest.raises(ValueError,match='file limit'):process.subprocess.run([str(binary),'exec','--','hello'],env={'SMSINVIT_TASK_HOME':str(home)},cwd=work,capture_output=True,text=True,timeout=30)
    assert len(calls)==1

def test_preflight_uses_only_offline_versions_and_private_directories(child,tmp_path,monkeypatch):
    binary,homes,calls=child;node=tmp_path/'node';node.write_text('# fixture');node.chmod(0o700)
    monkeypatch.setenv('OPENAI_SENTINEL_NODE_PATH',str(node))
    def fake(argv,**kwargs):
        calls.append((argv,kwargs));assert argv[-1]=='--version'
        output='v24.17.0' if argv[-2]==str(node) else 'codex-cli 0.160.0'
        assert Path(argv[3]).stat().st_mode&0o077==0
        assert '--proxy-pin' not in argv and '--read-file' not in argv and '--ca-file' not in argv
        return subprocess.CompletedProcess(argv,0,output+'\n','')
    monkeypatch.setattr(process,'run_child',fake)
    assert process.execution_self_check(homes.parent,str(binary)) is True
    assert len(calls)==2 and all(not Path(argv[3]).exists() and not Path(argv[5]).exists() for argv,kwargs in calls)
    assert not any(homes.iterdir())

def test_unsupported_kernel_preflight_fails_before_node_and_cleans_directories(child):
    binary,homes,calls=child
    with pytest.raises(RuntimeError,match='self-check failed'):process.execution_self_check(homes.parent,str(binary))
    assert len(calls)==1 and calls[0][0][-1]=='--version' and not any(homes.iterdir())


def _cli_stubs(monkeypatch, codex_auth, run_say_hi):
    """CLI 阶段的最小桩：只保留 run_say_hi 这个接缝是真的。"""
    monkeypatch.setattr(codex_auth.codex_cli, "run_say_hi", run_say_hi)
    monkeypatch.setattr(codex_auth.db, "save_codex_auth", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth.db, "set_registered_refresh_token", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_ensure_fresh_codex_creds", lambda email, creds, proxy: creds)
    monkeypatch.setattr(codex_auth, "_export_sub2api", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(codex_auth, "emit_stage", lambda *a, **k: None)
    monkeypatch.setattr(codex_auth, "_print_stage_log", lambda *a, **k: None)
    return {"access_token": "a", "refresh_token": "r", "id_token": "i", "account_id": "ac"}


def test_sayhi_direct_setting_bypasses_the_proxy_for_the_cli_step(monkeypatch):
    """实测直连整轮 10.6s，走慢代理 33.3s 甚至整轮跑不完：CLI 这一步默认直连。"""
    from webui import codex_auth

    calls = {}
    creds = _cli_stubs(monkeypatch, codex_auth,
                       lambda job_id, creds, proxy="", **kw: calls.update(proxy=proxy) or {
                           "ok": True, "tokens": {}, "elapsed": 1.0})
    for setting, expected in (("1", ""), ("0", "http://p:1")):
        monkeypatch.setattr(codex_auth, "_truthy_setting",
                            lambda key, default="", _s=setting: _s == "1")
        codex_auth._complete_cli("a@example.test", dict(creds), proxy="http://p:1",
                                 job_id="job", on_stage=None)
        assert calls["proxy"] == expected, (setting, calls)


def test_cli_is_direct_when_the_setting_row_is_missing(monkeypatch):
    """库里没有 `sayhi_direct` 这一行时必须直连。

    真实事故：后台 `_INVITE_DEFAULTS` 写的是 "1"（页面显示"直连已开"），而运行时读的
    是 `_truthy_setting("sayhi_direct","0")` + 库里没有这一行 → 生产每一轮都走代理，
    三段计时 16s/33s/36s（输入完全相同）。
    """
    from smsinvit.invitation.runtime import TASK_SETTINGS
    from webui import codex_auth

    calls = {}
    creds = _cli_stubs(monkeypatch, codex_auth,
                       lambda job_id, creds, proxy="", **kw: calls.update(proxy=proxy) or {
                           "ok": True, "tokens": {}, "elapsed": 1.0})
    token = TASK_SETTINGS.set({})          # 冻结设置里没有 sayhi_direct
    try:
        codex_auth._complete_cli("a@example.test", dict(creds), proxy="http://p:1",
                                 job_id="job", on_stage=None)
    finally:
        TASK_SETTINGS.reset(token)
    assert calls["proxy"] == ""


def test_cli_direct_transport_failure_falls_back_to_the_proxy_once(monkeypatch):
    """直连被拦/超时 → 用本次 run 的代理重跑一次，并带上第一次轮换过的凭据。"""
    from webui import codex_auth

    attempts = []

    def fake(job_id, creds, proxy="", settings=None, **kw):
        attempts.append({"proxy": proxy, "access": creds.get("access_token"), "settings": settings})
        if len(attempts) == 1:
            return {"ok": False, "reason": "codex 超时 timeout（60s，代理太慢或不可用）",
                    "tokens": {"access_token": "rotated-access"}, "elapsed": 61.0}
        return {"ok": True, "tokens": {}, "elapsed": 9.0}

    creds = _cli_stubs(monkeypatch, codex_auth, fake)
    monkeypatch.setattr(codex_auth, "_truthy_setting", lambda key, default="": True)
    monkeypatch.setattr(codex_auth, "_int_setting", lambda key, default=0: 180)
    saved = []
    monkeypatch.setattr(codex_auth.db, "save_codex_auth",
                        lambda email, **fields: saved.append(fields))
    codex_auth._complete_cli("a@example.test", dict(creds), proxy="http://p:1",
                             job_id="job", on_stage=None)
    assert [a["proxy"] for a in attempts] == ["", "http://p:1"]
    assert attempts[1]["access"] == "rotated-access"
    assert 30 <= int(attempts[1]["settings"]["cli_timeout"]) <= 180
    # 落库也必须是累计值：回退轮没轮换，直连轮刷出来的 access_token 不能被旧值盖掉。
    # （收尾还会写一条只带 cli_completed_at 的完成戳，所以取最后一条带凭据的。）
    credential_writes = [entry for entry in saved if "access_token" in entry]
    assert credential_writes and credential_writes[-1]["access_token"] == "rotated-access"


def test_cli_business_failure_does_not_fall_back(monkeypatch):
    """账号/业务类失败不回退：重试只会白烧一次启动与配额。"""
    from webui import codex_auth

    attempts = []

    def fake(job_id, creds, proxy="", settings=None, **kw):
        attempts.append(proxy)
        return {"ok": False, "reason": "CLI 回合未完成: codex 没有产出回复（退出码 1，20s）",
                "tokens": {}, "elapsed": 20.0}

    creds = _cli_stubs(monkeypatch, codex_auth, fake)
    monkeypatch.setattr(codex_auth, "_truthy_setting", lambda key, default="": True)
    monkeypatch.setattr(codex_auth, "_int_setting", lambda key, default=0: 180)
    try:
        codex_auth._complete_cli("a@example.test", dict(creds), proxy="http://p:1",
                                 job_id="job", on_stage=None)
    except Exception:  # JobFailure：这一轮确实失败了，本用例只关心"没有第二次"
        pass
    assert attempts == [""]


def test_cli_explicit_proxy_setting_never_falls_back(monkeypatch):
    """`sayhi_direct=0` 是操作员"锁死出口"的开关：失败也不切直连/不重试。"""
    from webui import codex_auth

    attempts = []

    def fake(job_id, creds, proxy="", settings=None, **kw):
        attempts.append(proxy)
        return {"ok": False, "reason": "codex 超时 timeout", "tokens": {}, "elapsed": 60.0}

    creds = _cli_stubs(monkeypatch, codex_auth, fake)
    monkeypatch.setattr(codex_auth, "_truthy_setting", lambda key, default="": False)
    monkeypatch.setattr(codex_auth, "_int_setting", lambda key, default=0: 180)
    try:
        codex_auth._complete_cli("a@example.test", dict(creds), proxy="http://p:1",
                                 job_id="job", on_stage=None)
    except Exception:  # JobFailure
        pass
    assert attempts == ["http://p:1"]

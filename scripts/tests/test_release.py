import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from release import DUJIAO_PUBLIC_EXCEPTIONS,ReleaseError,archive,selected_files,scan_bytes,stage
from prepare_deployment import prepare
from database_tools import MaintenanceError
from sqlite_snapshot import snapshot
from cryptography import x509
from configure_site import configure,origins
from prepare_dujiao import prepare as prepare_dujiao

def test_private_trees_are_never_read_and_source_auth_db_modules_retained(tmp_path,monkeypatch):
    source=tmp_path/'source';(source/'apps/api').mkdir(parents=True);(source/'apps/api/app.py').write_text('print("ok")')
    forbidden=source/'data/private/auth.json';forbidden.parent.mkdir(parents=True);forbidden.write_text('private')
    nested=source/'vendor/codex_invit/private/token.json';nested.parent.mkdir(parents=True);nested.write_text('private')
    read=Path.read_bytes
    def checked(path):
        assert path not in (forbidden,nested),'Sensitive input was read'
        return read(path)
    monkeypatch.setattr(Path,'read_bytes',checked)
    manifest=stage(source,tmp_path/'stage')
    assert [entry['path'] for entry in manifest['files']]==['apps/api/app.py']
    dujiao=tmp_path/'dujiao';(dujiao/'internal/auth').mkdir(parents=True);(dujiao/'internal/db').mkdir(parents=True)
    (dujiao/'internal/auth/login.go').write_text('package auth');(dujiao/'internal/db/db.go').write_text('package db')
    assert len(selected_files(dujiao,'dujiao'))==2

def test_deterministic_archive_and_hashed_manifest(tmp_path):
    source=tmp_path/'source';(source/'apps/api').mkdir(parents=True);(source/'apps/api/app.py').write_text('print("ok")')
    first=archive(source,tmp_path/'a.tar.gz');second=archive(source,tmp_path/'b.tar.gz')
    assert first['sha256']==second['sha256']
    with tarfile.open(tmp_path/'a.tar.gz') as tar:
        manifest=json.load(tar.extractfile('smsinvit-redeem/release-manifest.json'))
        entry=manifest['files'][0];data=tar.extractfile('smsinvit-redeem/'+entry['path']).read()
        assert hashlib.sha256(data).hexdigest()==entry['sha256']
    with pytest.raises(ReleaseError):archive(source,tmp_path/'a.tar.gz')

def test_runtime_trees_are_excluded_without_reading(tmp_path,monkeypatch):
    """`runtime` is a denied directory name and no path exception exists any more."""
    source=tmp_path/'source';page=source/'apps/admin/src/pages/settings/runtime/page.tsx';page.parent.mkdir(parents=True);page.write_text('export default function Page() {}')
    secret=page.parent/'secret.json';secret.write_text('private-fixture')
    other=source/'apps/api/runtime/session.json';other.parent.mkdir(parents=True);other.write_text('private-fixture')
    kept=source/'apps/admin/src/pages/invitation/page.tsx';kept.parent.mkdir(parents=True);kept.write_text('native admin')
    read=Path.read_bytes
    def checked(path):
        assert path not in (secret,page,other),'Runtime data was read'
        return read(path)
    monkeypatch.setattr(Path,'read_bytes',checked)
    manifest=stage(source,tmp_path/'stage')
    assert {row['path'] for row in manifest['files']}=={'apps/admin/src/pages/invitation/page.tsx'}

def test_non_allowlisted_suffixes_are_not_packaged(tmp_path):
    """Java fixtures left the tree with the retired parity harness; only source suffixes ship."""
    source=tmp_path/'source';known=source/'apps/api/tests/sms_gpt_parity/ParityLauncher.java';known.parent.mkdir(parents=True);known.write_text('class ParityLauncher {}')
    test_file=known.parent/'test_parity.py';test_file.write_text('def test_ok(): pass')
    assert [relative.as_posix() for relative,_ in selected_files(source)]==['apps/api/tests/sms_gpt_parity/test_parity.py']

def test_retired_invitation_ui_is_excluded_without_reading_but_business_modules_remain(tmp_path,monkeypatch):
    source=tmp_path/'source';business=source/'vendor/codex_invit/webui/codex_auth.py';business.parent.mkdir(parents=True);business.write_text('business logic')
    rejected=[]
    for directory in ('frontend','frontend_user','static','static_user'):
        file=business.parent/directory/'index.html';file.parent.mkdir();file.write_text('retired frontend');rejected.append(file)
    public=source/'apps/web/src/page.tsx';public.parent.mkdir(parents=True);public.write_text('public frontend')
    admin=source/'apps/admin/src/pages/invitation/page.tsx';admin.parent.mkdir(parents=True);admin.write_text('native admin')
    read=Path.read_bytes
    def checked(path):
        assert path not in rejected,'Retired UI entered release reads'
        return read(path)
    monkeypatch.setattr(Path,'read_bytes',checked)
    manifest=stage(source,tmp_path/'stage')
    assert {row['path'] for row in manifest['files']}=={'vendor/codex_invit/webui/codex_auth.py','apps/web/src/page.tsx','apps/admin/src/pages/invitation/page.tsx'}

def test_optional_telegram_public_inputs_are_exactly_allowlisted_without_private_reads(tmp_path,monkeypatch):
    source=tmp_path/'dujiao'
    for relative in DUJIAO_PUBLIC_EXCEPTIONS:
        file=source/relative;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('public fixture')
    rejected=['.env.telegram-bot','.env.telegram-stack','telegram-bot','docs/private.md',
              'deploy/telegram/config.yml','deploy/telegram/private/token.json',
              'scripts/telegram/credentials.json']
    for relative in rejected:
        file=source/relative;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('private fixture')
    read=Path.read_bytes
    def checked(path):
        assert path.relative_to(source).as_posix() not in rejected,'Unlisted input was read'
        return read(path)
    monkeypatch.setattr(Path,'read_bytes',checked)
    manifest=stage(source,tmp_path/'stage','dujiao')
    assert {row['path'] for row in manifest['files']}==DUJIAO_PUBLIC_EXCEPTIONS
    assert len(manifest['files'])==19

def test_source_symlink_and_actual_secret_are_rejected(tmp_path):
    (tmp_path/'apps/api').mkdir(parents=True);(tmp_path/'apps/api/leak.py').symlink_to('/etc/passwd')
    with pytest.raises(ReleaseError):stage(tmp_path,tmp_path.parent/'rejected-stage')
    with pytest.raises(ReleaseError):scan_bytes('test.py',('token="'+'sk-proj-'+'a'*60+'"').encode())
    # PEM parsing code often contains marker strings without an actual key.
    scan_bytes('pem_parser.go',b'const header="-----BEGIN PRIVATE KEY-----"')

def test_deployment_uses_explicit_retained_keys_tls_and_least_privilege(tmp_path):
    keys={'inventory_encryption_key':'f'*64,'inventory_fingerprint_key':'fingerprint-retained','jwt_secret':'jwt-retained'}
    retained=tmp_path/'retained.json';retained.write_text(json.dumps(keys));retained.chmod(0o600)
    private=tmp_path/'private';result=prepare(private,'redeem.example.com',retained)
    assert result['retainedEncryptionKeys'] and not result['workersEnabled']
    import yaml
    config=yaml.safe_load((private/'runtime.yaml').read_text())
    assert config['smsinvit']['inventory-encryption-key']==keys['inventory_encryption_key']
    assert config['MYSQL_TLS_MODE']=='VERIFY_IDENTITY'
    assert (private/'runtime.yaml').stat().st_mode&0o777==0o600
    account_sql=(private/'init/001-accounts.sql').read_text()
    runtime_grant=next(line for line in account_sql.splitlines() if "TO 'smsinvit_runtime'" in line)
    assert runtime_grant.startswith('GRANT SELECT,INSERT,UPDATE,DELETE ON') and account_sql.count('REQUIRE SSL')==3
    cert=x509.load_pem_x509_certificate((private/'mysql-tls/server.pem').read_bytes())
    assert 'mysql' in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    with pytest.raises(MaintenanceError):prepare(private,'redeem.example.com',retained)
    with pytest.raises(MaintenanceError):prepare(tmp_path/'codex_invit/private','redeem.example.com',retained)

def test_sqlite_source_is_only_copied_then_copy_is_opened(tmp_path,monkeypatch):
    source_dir=tmp_path/'codex_invit/webui';source_dir.mkdir(parents=True);source=source_dir/'db.sqlite3'
    with sqlite3.connect(source) as db:db.execute('CREATE TABLE records(id INTEGER)');db.execute('INSERT INTO records VALUES(7)')
    before=hashlib.sha256(source.read_bytes()).hexdigest();connect=sqlite3.connect;opened=[]
    def checked(path,*args,**kwargs):
        assert str(source) not in str(path),'Source database opened'
        opened.append(str(path));return connect(path,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',checked)
    result=snapshot(source,tmp_path/'snapshot')
    assert opened and hashlib.sha256(source.read_bytes()).hexdigest()==before
    with connect(result['snapshot']) as db:assert db.execute('SELECT id FROM records').fetchone()[0]==7
    with pytest.raises(MaintenanceError):snapshot(source,tmp_path/'codex_invit/forbidden')

def test_site_metadata_is_https_scoped_and_requires_explicit_apply():
    preview=configure(None,'https://redeem.example.com/')
    assert preview['status']=='preview' and preview['requiresExplicitApply']
    # runtime.web_origin 已从运行时目录删除（无读取方），站点元数据只剩 API 基址与 CORS。
    assert preview['values']=={'runtime.public_api_base':'https://redeem.example.com','runtime.cors_origins':'https://redeem.example.com'}
    for url in ['http://redeem.example.com','https://user:password@redeem.example.com','https://redeem.example.com/path','https://*.example.com','https://redeem.example.com?token=unsafe']:
        with pytest.raises(MaintenanceError):origins(url)

def test_storefront_deployment_retains_private_config_without_source_writes(tmp_path):
    config=tmp_path/'retained.yml';data=b'jwt:\n  secret: retained-fixture\n';config.write_bytes(data);config.chmod(0o600)
    prepared=tmp_path/'private';result=prepare_dujiao(prepared,config)
    assert result['runtimeUid']==10001 and result['retainedConfigUnchanged']
    assert config.read_bytes()==data and (prepared/'config.yml').read_bytes()==data
    assert (prepared/'config.yml').stat().st_mode&0o777==0o600
    assert not result['servicesStarted'] and not result['databasesOpened']
    with pytest.raises(ValueError):prepare_dujiao(prepared,config)
    config.chmod(0o644)
    with pytest.raises(ValueError):prepare_dujiao(tmp_path/'rejected',config)


@pytest.mark.parametrize('image,user,changed',[
    ('unexpected-caddy:v1','10001:10001',False),
    ('smsinvit-caddy:v1','0:0',False),
    ('smsinvit-caddy:v1','10001:10001',True),
    ('smsinvit-mysql:v1','10001:10001',False),
    ('dujiao-redis:v1','999:999',False),
])
def test_offline_exports_reject_unverified_tags_ids_and_runtime_users(tmp_path,monkeypatch,image,user,changed):
    import export_images
    proof=tmp_path/'build.json';identifier='sha256:'+'a'*64
    proof.write_text(json.dumps({'builds':[{'image':image,'imageId':identifier,'architecture':'amd64','status':'passed','target':'runtime'}]}))
    monkeypatch.setattr(export_images,'inspect',lambda value:{'image':value,'imageId':'sha256:'+'b'*64 if changed else identifier,'architecture':'amd64','runtimeUser':user})
    # A refusal must occur before any archive directory or Docker save is made.
    monkeypatch.setattr(export_images.subprocess,'run',lambda *args,**kwargs:pytest.fail('An unverified runtime image reached Docker save'))
    output=tmp_path/'rejected'
    with pytest.raises(ValueError):export_images.export([image],output,proof)
    assert not output.exists()

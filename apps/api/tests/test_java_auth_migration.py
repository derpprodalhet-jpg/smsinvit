"""Real old-Java-issued administrator credentials survive backup/restore and forward migrations.

Opt in with SMSINVIT_TEST_PRIVATE_CONFIG and SMSINVIT_PARITY_LEGACY_JAR. Every
account/key/secret is a fixture in two random disposable localhost:13308 schemas.
The old application and SecretCodec are read-only copies, never reimplemented or
patched. No existing schema, original project, supplier or invitation worker is
used. The Python requests exercise the real HTTP middleware and auth routes.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4
import zipfile

from fastapi.testclient import TestClient
import httpx
import pymysql
import pytest

from smsinvit.app import Services, create_app
from smsinvit.config import Config
from smsinvit.security import SecretCodec, totp_code

WORK = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    not os.environ.get('SMSINVIT_TEST_PRIVATE_CONFIG') or not os.environ.get('SMSINVIT_PARITY_LEGACY_JAR'),
    reason='Explicit disposable MySQL and read-only old Java jar not enabled',
)

# The real Java class creates the stored Argon2id hash and AES-GCM TOTP ciphertext.
# It does not mint tokens: tokens below must come from the unchanged Java HTTP API.
JAVA_CRYPTO = '''
import com.smsinvit.common.security.SecretCodec;
import tools.jackson.databind.json.JsonMapper;
import java.util.Map;
public class LegacyAuthCrypto {
 public static void main(String[] args) {
  var codec=new SecretCodec(System.getenv("INVENTORY_ENCRYPTION_KEY"),System.getenv("INVENTORY_FINGERPRINT_KEY"));
  System.out.println(JsonMapper.builder().build().writeValueAsString(Map.of(
   "passwordHash",codec.hashPassword(System.getenv("FIXTURE_PASSWORD")),
   "totpCiphertext",codec.encrypt(System.getenv("FIXTURE_TOTP")))));
 }
}
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(client, method, path, **kwargs):
    # Explicit-body refresh and bearer requests must not accidentally reuse the
    # preceding client's cookie jar. Cookie authentication is tested separately.
    client.cookies.clear()
    return client.request(method, path, **kwargs)


def password_login(client, identifier, password):
    response = request(client, 'GET', '/api/v1/admin/auth/captcha?type=math')
    assert response.status_code == 200
    challenge = response.json()['data']
    match = re.fullmatch(r'(\d+) ([+−-]) (\d+) = \?', challenge['prompt'])
    assert match, 'The real service must supply a supported math captcha'
    answer = int(match[1]) + int(match[3]) if match[2] == '+' else int(match[1]) - int(match[3])
    response = request(client, 'POST', '/api/v1/admin/auth/login', json={
        'identifier': identifier, 'password': password,
        'captchaToken': challenge['captchaToken'], 'captchaAnswer': str(answer),
    })
    assert response.status_code == 200
    return response.json()['data']


@pytest.fixture(scope='module')
def migrated_java_auth(tmp_path_factory):
    sys.path.insert(0, str(WORK / 'scripts'))
    from backup import backup
    from database_tools import Credentials
    from migrate import run as migrate, migrations
    from restore import restore

    private = json.loads(Path(os.environ['SMSINVIT_TEST_PRIVATE_CONFIG']).read_text())
    assert private.get('host', '127.0.0.1') == '127.0.0.1' and int(private.get('port', 13308)) == 13308
    temp = tmp_path_factory.mktemp('java-auth-migration')
    suffix = secrets.token_hex(4)
    names = {'source': 'smsinvit_auth_source_' + suffix, 'target': 'smsinvit_auth_target_' + suffix}
    admin = pymysql.connect(host='127.0.0.1', port=13308, user='root', password=private['password'])
    connection = source_db = java_process = java_client = python_client = log = None
    java = shutil.which('java'); javac = shutil.which('javac')
    assert java and javac and shutil.which('mysql') and shutil.which('mysqldump')
    encryption = 'e7' * 32; fingerprint = 'old-java-auth-fixture-fingerprint'; jwt_key = 'legacy-admin-fixture-jwt-' + 'j' * 64
    password = 'legacy-admin-fixture-password'; totp = 'JBSWY3DPEHPK3PXP'
    report = {'format': 'smsinvit-java-auth-migration-v1', 'status': 'pending', 'assertions': [],
              'recordedAtUtc': datetime.now(timezone.utc).isoformat(),
              'testCodeSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'notVerified': ['No real user passwords or production-issued bearer tokens were used; compatibility is verified with unchanged Java-generated fixtures.']}
    try:
        with admin.cursor() as cursor:
            for name in names.values():
                assert re.fullmatch(r'smsinvit_auth_(source|target)_[0-9a-f]{8}', name)
                cursor.execute('CREATE DATABASE `' + name + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        admin.commit()
        extract = temp / 'old-java-copy'; extract.mkdir()
        jar_path = Path(os.environ['SMSINVIT_PARITY_LEGACY_JAR'])
        with zipfile.ZipFile(jar_path) as jar:
            assert all(not Path(item.filename).is_absolute() and '..' not in Path(item.filename).parts for item in jar.infolist())
            jar.extractall(extract)
        classpath = str(extract / 'BOOT-INF/classes') + os.pathsep + str(extract / 'BOOT-INF/lib/*')
        java_file = temp / 'LegacyAuthCrypto.java'; java_file.write_text(JAVA_CRYPTO)
        compiled = subprocess.run([javac, '-cp', classpath, '-d', str(temp), str(java_file)], capture_output=True, text=True)
        assert compiled.returncode == 0, compiled.stderr[:2000]
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'TMPDIR': str(temp),
               'INVENTORY_ENCRYPTION_KEY': encryption, 'INVENTORY_FINGERPRINT_KEY': fingerprint,
               'FIXTURE_PASSWORD': password, 'FIXTURE_TOTP': totp}
        generated = subprocess.run([java, '-cp', str(temp) + os.pathsep + classpath, 'LegacyAuthCrypto'],
                                   env=env, cwd=temp, capture_output=True, text=True, timeout=30)
        assert generated.returncode == 0, 'Unchanged Java SecretCodec fixture creation failed'
        crypto = json.loads(generated.stdout)
        assert crypto['passwordHash'].startswith('$argon2id$')

        port = free_port()
        env.update(JDBC_DATABASE_URL='jdbc:mysql://127.0.0.1:13308/' + names['source'] + '?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC&characterEncoding=utf8',
                   DATABASE_USER='root', DATABASE_PASSWORD=private['password'], JWT_SECRET=jwt_key,
                   SMSINVIT_TOTP_ENABLED='true', SMSINVIT_SECURE_COOKIES='false', SMSINVIT_LOG_DIR=str(temp / 'logs'),
                   PUBLIC_API_BASE='http://fixture.invalid')
        env.pop('FIXTURE_PASSWORD'); env.pop('FIXTURE_TOTP')
        log = (temp / 'old-java.log').open('w')
        # The actual application performs its original V1-V6 Flyway migrations.
        # No test launcher/bean substitutes any authentication or token logic.
        java_process = subprocess.Popen([java, '-cp', classpath, 'com.smsinvit.SmsInvitApplication',
            '--server.address=127.0.0.1', '--server.port=' + str(port)],
            cwd=temp, env=env, stdout=log, stderr=subprocess.STDOUT)
        java_client = httpx.Client(base_url='http://127.0.0.1:' + str(port), timeout=30, trust_env=False)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if java_process.poll() is not None:
                raise RuntimeError('Old Java fixture exited; private diagnostic log: ' + str(temp / 'old-java.log'))
            try:
                if java_client.get('/actuator/health').status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.15)
        else:
            raise RuntimeError('Old Java fixture did not become ready')

        source = Credentials('127.0.0.1', 13308, 'root', private['password'], names['source'], 'disabled')
        target = Credentials('127.0.0.1', 13308, 'root', private['password'], names['target'], 'disabled')
        source_db = source.connect()
        owner, other = str(uuid4()), str(uuid4())
        with source_db.transaction():
            for uid, username in ((owner, 'legacy-owner'), (other, 'legacy-other')):
                source_db.execute("INSERT INTO admin_users(id,username,email,password_hash,status,totp_enabled_at,totp_secret_encrypted) VALUES(?,?,?,?,'ACTIVE',NOW(3),?)",
                                  uid, username, username + '@example.test', crypto['passwordHash'], crypto['totpCiphertext'])
                source_db.execute("INSERT INTO admin_user_roles(id,admin_user_id,role_id) SELECT ?,?,id FROM roles WHERE code='SUPER_ADMIN'", str(uuid4()), uid)
        first = password_login(java_client, 'legacy-owner', password)
        assert first['needTotp'] and first['challengeToken']
        signed = request(java_client, 'POST', '/api/v1/admin/auth/login/totp', json={'challengeToken': first['challengeToken'], 'code': totp_code(totp)})
        assert signed.status_code == 200
        old_tokens = signed.json()['data']
        assert old_tokens['accessToken'] and old_tokens['refreshToken']
        old_me = request(java_client, 'GET', '/api/v1/admin/auth/me', headers={'Authorization': 'Bearer ' + old_tokens['accessToken']})
        assert old_me.status_code == 200 and old_me.json()['data']['id'] == owner
        old_session = source_db.one('SELECT * FROM admin_sessions WHERE refresh_token_hash=?', hashlib.sha256(old_tokens['refreshToken'].encode()).hexdigest())
        assert old_session and old_session['admin_user_id'] == owner
        old_user = source_db.one('SELECT * FROM admin_users WHERE id=?', owner)
        assert old_user['password_hash'] == crypto['passwordHash'] and old_user['totp_secret_encrypted'] == crypto['totpCiphertext']
        history = source_db.all('SELECT version,checksum,success FROM flyway_schema_history ORDER BY installed_rank')
        assert [str(row['version']) for row in history] == [str(i) for i in range(1, 7)] and all(row['success'] for row in history)
        legacy_versions = [int(row['version']) for row in history]
        forward_versions = [item.version for item in migrations() if item.version not in legacy_versions]
        source_db.close(); source_db = None
        java_process.terminate(); java_process.wait(timeout=20); java_process = None
        java_client.close(); java_client = None

        dump = temp / 'old-auth.sql'
        backed = backup(source, dump)
        restored = restore(target, dump)
        assert backed['status'] == 'complete' and restored['status'] == 'restored_and_verified'
        migrated = migrate(target)
        assert migrated['status'] == 'ready' and migrated['appliedVersions'] == forward_versions
        assert migrate(target)['appliedVersions'] == []
        connection = target.connect()
        assert connection.one('SELECT * FROM admin_users WHERE id=?', owner) == old_user
        assert connection.one('SELECT * FROM admin_sessions WHERE id=?', old_session['id']) == old_session
        config = Config({'smsinvit.inventory-encryption-key': encryption, 'smsinvit.inventory-fingerprint-key': fingerprint,
                         'smsinvit.jwt-secret': jwt_key, 'runtime.totp_enabled': True, 'runtime.secure_cookies': False,
                         'SMSINVIT_ENV': 'test', 'smsinvit.worker-enabled': False}, environ={})
        services = Services(connection, config, SecretCodec(encryption, fingerprint))
        app = create_app(config, connection, services=services, include_domains=False, load_runtime=False)
        python_client = TestClient(app, raise_server_exceptions=False)
        report.update(legacyJarSha256=hashlib.sha256(jar_path.read_bytes()).hexdigest(),
                      backupSha256=backed['sha256'], legacyMigrations=legacy_versions, pythonMigrations=forward_versions,
                      realJavaHttpTokenIssuer=True, realJavaArgonAndAesCodec=True, backupRestoreVerified=True,
                      storedPasswordCiphertextAndSessionUnchanged=True, migrationRepeatNoop=True,
                      isolation={'schemas': 'two random disposable MySQL8.4 schemas on localhost:13308; removed afterward',
                                 'accounts': 'synthetic owner and other administrator', 'keys': 'same synthetic retained keys',
                                 'originalSourceModified': False, 'realUpstreamBusinessRequests': 0, 'pythonWorkersStarted': False})
        yield dict(client=python_client, db=connection, owner=owner, other=other, old_tokens=old_tokens,
                   old_session=old_session, password=password, totp=totp, report=report)
    finally:
        if python_client: python_client.close()
        if java_client: java_client.close()
        if java_process:
            java_process.terminate()
            try: java_process.wait(timeout=15)
            except subprocess.TimeoutExpired: java_process.kill(); java_process.wait(timeout=5)
        if log: log.close()
        if source_db: source_db.close()
        if connection: connection.close()
        with admin.cursor() as cursor:
            for name in names.values(): cursor.execute('DROP DATABASE IF EXISTS `' + name + '`')
        admin.close()
        evidence = os.environ.get('SMSINVIT_AUTH_MIGRATION_EVIDENCE')
        if evidence:
            Path(evidence).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


def test_real_java_credentials_after_restore_and_python_migration(migrated_java_auth):
    f = migrated_java_auth; client = f['client']; db = f['db']; old = f['old_tokens']; report = f['report']
    auth = {'Authorization': 'Bearer ' + old['accessToken']}
    me = request(client, 'GET', '/api/v1/admin/auth/me', headers=auth)
    assert me.status_code == 200 and me.json()['data']['id'] == f['owner']
    assert me.json()['data']['totpEnabled'] is True and me.json()['data']['roles'] == ['SUPER_ADMIN']
    report['assertions'].append('Original Java-issued JWT authenticates the original owner and retained TOTP/role profile through Python HTTP')

    # A validly signed old JWT is still tied to its exact owner/session database row.
    db.execute('UPDATE admin_sessions SET admin_user_id=? WHERE id=?', f['other'], f['old_session']['id'])
    assert request(client, 'GET', '/api/v1/admin/auth/me', headers=auth).status_code == 401
    db.execute('UPDATE admin_sessions SET admin_user_id=? WHERE id=?', f['owner'], f['old_session']['id'])
    db.execute('UPDATE admin_sessions SET expires_at=DATE_SUB(NOW(3),INTERVAL 1 SECOND) WHERE id=?', f['old_session']['id'])
    assert request(client, 'GET', '/api/v1/admin/auth/me', headers=auth).status_code == 401
    db.execute('UPDATE admin_sessions SET expires_at=? WHERE id=?', f['old_session']['expires_at'], f['old_session']['id'])
    assert request(client, 'GET', '/api/v1/admin/auth/me', headers=auth).status_code == 200
    report['assertions'].append('Session owner substitution and expired session reject the unchanged valid Java JWT')

    client.cookies.clear(); client.cookies.set('lc_admin_access', old['accessToken'])
    assert client.get('/api/v1/admin/auth/me').json()['data']['id'] == f['owner']
    report['assertions'].append('Original Java JWT also works through the preserved admin-access cookie name')

    rotated = request(client, 'POST', '/api/v1/admin/auth/refresh', json={'refreshToken': old['refreshToken']})
    assert rotated.status_code == 200
    new = rotated.json()['data']
    assert new['refreshToken'] != old['refreshToken']
    assert request(client, 'POST', '/api/v1/admin/auth/refresh', json={'refreshToken': old['refreshToken']}).status_code == 401
    assert request(client, 'GET', '/api/v1/admin/auth/me', headers=auth).status_code == 401
    new_auth = {'Authorization': 'Bearer ' + new['accessToken']}
    assert request(client, 'GET', '/api/v1/admin/auth/me', headers=new_auth).json()['data']['id'] == f['owner']
    assert db.scalar('SELECT revoked_at FROM admin_sessions WHERE id=?', f['old_session']['id']) is not None
    assert db.scalar('SELECT admin_user_id FROM admin_sessions WHERE refresh_token_hash=?', hashlib.sha256(new['refreshToken'].encode()).hexdigest()) == f['owner']
    report['assertions'].append('Original Java refresh token rotates once in Python HTTP; reuse and old JWT fail, new session retains its owner')
    assert request(client, 'POST', '/api/v1/admin/auth/logout', headers=new_auth).status_code == 200
    assert request(client, 'GET', '/api/v1/admin/auth/me', headers=new_auth).status_code == 401
    assert request(client, 'POST', '/api/v1/admin/auth/refresh', json={'refreshToken': new['refreshToken']}).status_code == 401
    report['assertions'].append('Logout revokes the rotated access and refresh credentials')

    again = password_login(client, 'legacy-owner', f['password'])
    assert again['needTotp'] and not again.get('accessToken')
    logged_in = request(client, 'POST', '/api/v1/admin/auth/login/totp', json={'challengeToken': again['challengeToken'], 'code': totp_code(f['totp'])})
    assert logged_in.status_code == 200
    final_auth = {'Authorization': 'Bearer ' + logged_in.json()['data']['accessToken']}
    assert request(client, 'GET', '/api/v1/admin/auth/me', headers=final_auth).json()['data']['id'] == f['owner']
    assert request(client, 'POST', '/api/v1/admin/auth/login/totp', json={'challengeToken': again['challengeToken'], 'code': totp_code(f['totp'])}).status_code == 401
    report['assertions'].append('Retained real Java Argon2id password and Java AES-GCM TOTP ciphertext complete Python HTTP login; TOTP challenge cannot be reused')
    report['status'] = 'PASS'

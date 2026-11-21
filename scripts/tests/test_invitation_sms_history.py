"""Historical imports use synthetic evidence, authenticated archives and no live DB."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'apps/api/tests'))

from invitation_sms_sqlite import isolated_sms_database
import invitation_sms_history as history
import private_archive as archive
from smsinvit.invitation import mysql, sms_state
from smsinvit.security import SecretCodec


@pytest.fixture
def repository(tmp_path, monkeypatch):
    db = isolated_sms_database(tmp_path / 'sms-history.sqlite3')
    codec = SecretCodec('b1' * 32, 'isolated-history-fingerprint')
    monkeypatch.setattr(mysql, '_services', None)
    monkeypatch.setattr(mysql, '_runtime_owner', None)
    monkeypatch.setattr(mysql, '_cache', {})
    monkeypatch.setattr(mysql, '_redaction_values', set())
    mysql.configure(SimpleNamespace(db=db, codec=codec))
    yield db, codec
    db.close()


def evidence(phone):
    return f'12:34:56 [INFO] auth_flow: [sms] 📤 准备 POST add-phone/send (phone={phone}) ...\n'.encode()


def test_collect_accepts_only_proven_send_or_validate_templates_and_used_cache():
    members = {
        'private-run-name.log': b''.join([
            evidence('+12025550101'),
            '12:34:57 [INFO] auth_flow: [sms] ✅ POST add-phone/send 成功 (phone=+12025550101)\n'.encode(),
            '12:34:58 [INFO] auth_flow: [sms] ✅ phone-otp/validate 通过 (phone=+12025550102 code=123456) next=https://auth.openai.com/\n'.encode(),
            '12:34:59 [WARNING] auth_flow: [sms] validate 失败 (phone=+12025550103 code=654321): rejected\n'.encode(),
            '[sms] 已租到号码: +12025550104 (activation_id=unused-order)\n'.encode(),
            '[sms] 验证码=12025550105\n'.encode(),
            '[sms] 文档示例：准备 POST add-phone/send fake (phone=+12025550106)\n'.encode(),
            'Error includes quoted user content: [sms] 📤 准备 POST add-phone/send (phone=+12025550107) ...\n'.encode(),
            '[sms] 准备 POST add-phone/send malformed (phone=+12025550108)\n'.encode(),
        ]),
        'not-a-log.txt': evidence('+12025550109'),
    }
    caches = [
        ('used-cache.json', {'phone_number': '0012025550110', 'use_count': 1}),
        ('unused-cache.json', {'phone_number': '+12025550111', 'use_count': 0, 'used_codes': []}),
        ('failed-validation-cache.json', {'phone_number': '+12025550112', 'use_count': 0, 'used_codes': ['123456']}),
    ]
    phones, report = history.collect(members, caches)
    assert set(phones) == {'+12025550101', '+12025550102', '+12025550103', '+12025550110', '+12025550112'}
    assert report == {'scanned_files': 4, 'evidence_records': 6, 'unique_phones': 5, 'invalid_records': 0}
    assert all('private-run-name' not in source and len(source) < 160 for source in phones.values())


def test_preview_performs_no_writes_and_apply_is_idempotent_and_private(repository):
    db, codec = repository
    assert sms_state.import_phone('+12025550101', 'existing-fixture')
    members = {'first-private-name.log': evidence('+12025550101') + evidence('+12025550102')}
    before = db.all('SELECT * FROM invitation_v2_sms_phone_uses')
    preview = history.import_history(db, codec, members)
    assert preview['existing_phones'] == preview['new_phones'] == 1
    assert preview['applied'] is False
    assert db.all('SELECT * FROM invitation_v2_sms_phone_uses') == before
    assert db.scalar('SELECT COUNT(*) FROM invitation_v2_secret_values') == 0
    applied = history.import_history(db, codec, members, apply=True)
    assert applied['new_phones'] == 1 and applied['applied'] is True
    again = history.import_history(db, codec, members, apply=True)
    assert again['new_phones'] == 0 and again['existing_phones'] == 2
    stored = json.dumps(db.all('SELECT * FROM invitation_v2_sms_phone_uses'))
    reports = json.dumps([preview, applied, again])
    assert all(phone not in stored + reports for phone in ('+12025550101', '+12025550102'))
    assert 'first-private-name' not in stored + reports
    assert applied['historical_coverage'] == 'available-evidence-only'
    with sms_state.task_run('later-runtime-run'):
        assert sms_state.claim_phone('+12025550102') is False


def test_failed_import_rolls_back_earlier_records_in_the_same_batch(repository, monkeypatch):
    db, codec = repository
    original = sms_state.import_phone
    calls = []
    def fail_second(phone, source, used_at=None):
        calls.append(phone)
        if len(calls) == 2:
            raise RuntimeError('synthetic history-storage failure')
        return original(phone, source, used_at)
    monkeypatch.setattr(sms_state, 'import_phone', fail_second)
    with pytest.raises(RuntimeError, match='history-storage failure'):
        history.import_history(db, codec,
                               {'two.log': evidence('+12025550130') + evidence('+12025550131')},
                               apply=True)
    assert db.scalar('SELECT COUNT(*) FROM invitation_v2_sms_phone_uses') == 0


def test_archive_reads_authenticated_members_in_memory_without_creating_plaintext_output(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    payload = evidence('+12025550141')
    (source / 'one.log').write_bytes(payload)
    key = os.urandom(32)
    encrypted = tmp_path / 'historical.enc'
    archive.encrypt_directory(source, encrypted, key)
    before = {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob('*')}
    assert archive.read_archive(encrypted, key) == {'one.log': payload}
    assert before == {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob('*')}
    assert payload not in encrypted.read_bytes()
    with pytest.raises(Exception):
        archive.read_archive(encrypted, os.urandom(32))
    damaged = tmp_path / 'damaged.enc'
    data = bytearray(encrypted.read_bytes())
    data[-1] ^= 1
    damaged.write_bytes(data)
    with pytest.raises(Exception):
        archive.read_archive(damaged, key)


def authenticated_archive(tmp_path, records, manifest):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as stream:
        for name, data in records:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
        data = json.dumps({'files': manifest}).encode()
        member = tarfile.TarInfo('.private-backup-manifest.json')
        member.size = len(data)
        stream.addfile(member, io.BytesIO(data))
    key, nonce = os.urandom(32), os.urandom(12)
    target = tmp_path / 'authenticated-malformed.enc'
    target.write_bytes(archive.MAGIC + nonce + AESGCM(key).encrypt(nonce, buffer.getvalue(), archive.MAGIC))
    return target, key


@pytest.mark.parametrize('records,manifest', [
    ([('../escape.log', b'x')], [{'name': '../escape.log', 'sha256': hashlib.sha256(b'x').hexdigest()}]),
    ([('./one.log', b'x')], [{'name': './one.log', 'sha256': hashlib.sha256(b'x').hexdigest()}]),
    ([('dir//one.log', b'x')], [{'name': 'dir//one.log', 'sha256': hashlib.sha256(b'x').hexdigest()}]),
    ([('one.log', b'x'), ('./one.log', b'x')], [{'name': 'one.log', 'sha256': hashlib.sha256(b'x').hexdigest()}, {'name': './one.log', 'sha256': hashlib.sha256(b'x').hexdigest()}]),
    ([('one.log', b'x')], [{'name': 'one.log', 'sha256': hashlib.sha256(b'y').hexdigest()}]),
    ([('one.log', b'x'), ('one.log', b'x')], [{'name': 'one.log', 'sha256': hashlib.sha256(b'x').hexdigest()}]),
])
def test_authenticated_archive_rejects_unsafe_members_duplicates_and_manifest_mismatch(tmp_path, records, manifest):
    target, key = authenticated_archive(tmp_path, records, manifest)
    with pytest.raises(ValueError):
        archive.read_archive(target, key)
    destination = tmp_path / 'restore-destination'
    with pytest.raises(ValueError):
        archive.restore_archive(target, destination, key)
    assert not destination.exists()
    assert not (tmp_path / 'escape.log').exists()


def test_cli_authentication_failure_never_connects_or_prints_secret_or_phone(tmp_path, monkeypatch, capsys):
    keys = tmp_path / 'keys.json'
    keys.write_text(json.dumps({'inventory_encryption_key': 'c1' * 32,
                               'inventory_fingerprint_key': 'synthetic-private-fingerprint'}))
    keys.chmod(0o600)
    archive_keys = tmp_path / 'archive-keys.json'
    archive_keys.write_text(json.dumps({'backup_key': 'd1' * 32}))
    archive_keys.chmod(0o600)
    corrupt = tmp_path / 'corrupt.enc'
    corrupt.write_bytes(b'synthetic corrupt archive +12025550166 password=private-fixture')
    monkeypatch.setattr(sys, 'argv', ['invitation_sms_history.py', '--keys-file', str(keys),
                                   '--archive', str(corrupt), '--archive-keys-file', str(archive_keys)])
    monkeypatch.setattr(history, 'credentials', lambda *args: pytest.fail('failed archive cannot connect'))
    assert history.main() == 1
    captured = capsys.readouterr()
    assert 'Historical SMS import refused' in captured.err
    assert not captured.out
    assert all(secret not in captured.err for secret in ('+12025550166', 'private-fixture', 'synthetic-private-fingerprint'))

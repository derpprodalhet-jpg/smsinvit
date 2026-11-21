import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
spec=importlib.util.spec_from_file_location('private_archive',Path(__file__).parents[1]/'private_archive.py')
archive=importlib.util.module_from_spec(spec);spec.loader.exec_module(archive)

def test_restore_exact_bytes_and_refuse_overwrite_or_corruption(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'one.log').write_bytes(b'private credential fixture');(source/'nested').mkdir();(source/'nested/two.log').write_bytes(b'\x00\xff')
    key=os.urandom(32);output=tmp_path/'backup.enc'
    assert archive.encrypt_directory(source,output,key)['files']==2
    assert b'private credential fixture' not in output.read_bytes()
    restored=tmp_path/'restored';archive.restore_archive(output,restored,key)
    assert (restored/'one.log').read_bytes()==(source/'one.log').read_bytes()
    assert (restored/'nested/two.log').read_bytes()==b'\x00\xff'
    with pytest.raises(ValueError):archive.restore_archive(output,restored,key)
    corrupted=tmp_path/'corrupt.enc';data=bytearray(output.read_bytes());data[-1]^=1;corrupted.write_bytes(data)
    with pytest.raises(Exception):archive.restore_archive(corrupted,tmp_path/'no-output',key)
    assert not (tmp_path/'no-output').exists()

def test_authenticated_traversal_rejected_before_destination_created(tmp_path):
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w') as tar:
        item=tarfile.TarInfo('../escape');item.size=1;tar.addfile(item,io.BytesIO(b'x'))
    key=os.urandom(32);nonce=os.urandom(12);source=tmp_path/'bad.enc';source.write_bytes(archive.MAGIC+nonce+AESGCM(key).encrypt(nonce,buffer.getvalue(),archive.MAGIC))
    with pytest.raises(ValueError):archive.restore_archive(source,tmp_path/'no-output',key)
    assert not (tmp_path/'no-output').exists() and not (tmp_path/'escape').exists()

def test_independent_key_generation_permissions_and_legacy_compatibility(tmp_path):
    keyfile=tmp_path/'private/shop-key.json'
    assert archive.generate_key(keyfile)=={'status':'generated','keyPrinted':False,'independentBackupKey':True}
    assert keyfile.stat().st_mode&0o777==0o600
    assert keyfile.parent.stat().st_mode&0o777==0o700
    key=archive.key_from_file(keyfile)
    assert len(key)==32 and json.loads(keyfile.read_text())['backup_key']==key.hex()
    before=keyfile.read_bytes()
    with pytest.raises(FileExistsError):archive.generate_key(keyfile)
    assert keyfile.read_bytes()==before
    source=tmp_path/'private-files';source.mkdir();(source/'config.yml').write_bytes(b'synthetic-config')
    encrypted=tmp_path/'shop.enc';archive.encrypt_directory(source,encrypted,key)
    restored=tmp_path/'restored';archive.restore_archive(encrypted,restored,archive.key_from_file(keyfile))
    assert (restored/'config.yml').read_bytes()==b'synthetic-config'
    retained=tmp_path/'legacy-key.json';retained.write_text(json.dumps({'inventory_encryption_key':key.hex()}));retained.chmod(0o600)
    assert archive.key_from_file(retained)==key
    retained.chmod(0o644)
    with pytest.raises(ValueError):archive.key_from_file(retained)

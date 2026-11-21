#!/usr/bin/env python3
"""Authenticated, private file backups; only explicit new destinations are written."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b'SMSINVIT-PRIVATE-FILES-V1\n'

def key_from_file(path):
    path = Path(path)
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('Keys must be in a regular private 0600 file')
    values = json.loads(path.read_text())
    value = values.get('backup_key', values.get('inventory_encryption_key'))
    if not isinstance(value,str): raise ValueError('An explicit independent backup key is required')
    key = bytes.fromhex(value)
    if len(key) != 32: raise ValueError('A retained 32-byte encryption key is required')
    return key

def generate_key(path):
    path = Path(path)
    if 'codex_invit' in path.resolve().parts:
        raise ValueError('The protected original invitation project cannot be a backup-key destination')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump({'backup_key':os.urandom(32).hex()},stream)
        stream.write('\n');stream.flush();os.fsync(stream.fileno())
    return {'status':'generated','keyPrinted':False,'independentBackupKey':True}

def encrypt_directory(directory, output, key):
    directory, output = Path(directory).resolve(), Path(output)
    if output.exists() or directory == output.resolve() or directory in output.resolve().parents:
        raise ValueError('Backup destination must be new and outside the source directory')
    files = []; buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for path in sorted(directory.rglob('*')):
            if path.is_symlink(): raise ValueError('Symlinks are not accepted in private backups')
            if not path.is_file(): continue
            before = path.stat(); data = path.read_bytes(); after = path.stat()
            if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ValueError('A source file changed while copying; retry after quiescing writers')
            name = path.relative_to(directory).as_posix()
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600
            archive.addfile(info, io.BytesIO(data)); files.append({'name': name, 'sha256': hashlib.sha256(data).hexdigest()})
        data = json.dumps({'files': files}, ensure_ascii=False).encode()
        info = tarfile.TarInfo('.private-backup-manifest.json'); info.size = len(data); info.mode = 0o600
        archive.addfile(info, io.BytesIO(data))
    nonce = os.urandom(12); ciphertext = MAGIC + nonce + AESGCM(key).encrypt(nonce,buffer.getvalue(),MAGIC)
    output.parent.mkdir(parents=True,exist_ok=True)
    fd = os.open(output, os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream: stream.write(ciphertext); stream.flush(); os.fsync(stream.fileno())
    return {'files':len(files),'sha256':hashlib.sha256(ciphertext).hexdigest(),'encrypted':True}

def read_archive(source,key):
    """Authenticate and validate members in memory without extracting plaintext."""
    data = Path(source).read_bytes()
    if not data.startswith(MAGIC): raise ValueError('Unsupported private backup format')
    nonce = data[len(MAGIC):len(MAGIC)+12]
    plaintext = AESGCM(key).decrypt(nonce,data[len(MAGIC)+12:],MAGIC)
    # Authenticate and validate every member before creating any destination.
    with tarfile.open(fileobj=io.BytesIO(plaintext),mode='r:') as archive:
        members = archive.getmembers(); names = set(); records = {}
        for member in members:
            name = PurePosixPath(member.name)
            if not member.isfile() or not name.parts or member.name != name.as_posix() or name.is_absolute() or any(part in ('..','.') for part in name.parts) or '\\' in member.name or member.name in names:
                raise ValueError('Unsafe or duplicate member in private backup')
            names.add(member.name); records[member.name] = archive.extractfile(member).read()
        manifest = json.loads(records.pop('.private-backup-manifest.json'))
        expected = {r['name']:r['sha256'] for r in manifest['files']}
        if len(expected)!=len(manifest['files']) or set(expected)!=set(records) or any(hashlib.sha256(data).hexdigest()!=expected[name] for name,data in records.items()):
            raise ValueError('Private backup manifest does not match')
    return records

def restore_archive(source,directory,key):
    directory = Path(directory)
    if directory.exists(): raise ValueError('Restore directory must be new')
    records = read_archive(source,key)
    directory.mkdir(parents=True,mode=0o700)
    for name,data in records.items():
        target=directory/Path(name); target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with os.fdopen(os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:stream.write(data)
    return {'files':len(records),'restored':True}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('keygen','encrypt','restore'));parser.add_argument('--keys-file',type=Path,required=True)
    parser.add_argument('--input',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        if args.mode=='keygen':
            if args.input or args.output:raise ValueError('Key generation accepts only a new keys-file destination')
            print(json.dumps(generate_key(args.keys_file)));return
        if not args.input or not args.output:raise ValueError('Backup/restore requires explicit input and new output paths')
        key=key_from_file(args.keys_file)
        result=encrypt_directory(args.input,args.output,key) if args.mode=='encrypt' else restore_archive(args.input,args.output,key)
        print(json.dumps(result))
    except Exception as error:parser.exit(1,'Private backup failed ('+type(error).__name__+'). No credentials are printed.\n')

if __name__=='__main__':main()

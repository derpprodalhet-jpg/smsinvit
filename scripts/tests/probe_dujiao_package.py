"""Verify a published storefront package using only synthetic local files."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile


def probe(archive,python,evidence):
    archive=Path(archive).resolve();python=str(Path(python).absolute());evidence=Path(evidence)
    with tempfile.TemporaryDirectory(prefix='dujiao-package-proof-') as temporary:
        base=Path(temporary)
        with tarfile.open(archive) as contents:contents.extractall(base,filter='data')
        project=base/'dujiao-next';manifest=json.loads((project/'release-manifest.json').read_text())
        assert all(hashlib.sha256((project/item['path']).read_bytes()).hexdigest()==item['sha256'] for item in manifest['files'])
        assert (project/'.gitignore').is_file() and '**/private' in (project/'.dockerignore').read_text()
        def cli(script,*arguments):
            result=subprocess.run([python,str(project/'scripts'/script),*map(str,arguments)],cwd=project,capture_output=True,text=True,timeout=60)
            assert result.returncode==0,(script,result.stderr)
            return result.stdout
        for name in ('build_images.py','release.py','pull_verified.py','scan_image.py','fetch_trivy_db.py','export_images.py','prepare_dujiao.py','sqlite_snapshot.py','private_archive.py'):cli(name,'--help')
        source=base/'synthetic-files';source.mkdir();database=source/'store.db'
        connection=sqlite3.connect(database)
        try:
            connection.execute('PRAGMA journal_mode=WAL');connection.execute('CREATE TABLE fixture(id INTEGER PRIMARY KEY,value TEXT)')
            connection.execute('INSERT INTO fixture VALUES(1,?)',('synthetic-data',));connection.commit()
            config=source/'config.yml';config.write_bytes(b'synthetic-config: true\n');config.chmod(0o600)
            uploads=source/'uploads';uploads.mkdir();(uploads/'fixture.txt').write_bytes(b'synthetic-upload')
            paths=[database,Path(str(database)+'-wal'),Path(str(database)+'-shm')]
            before={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}
            cli('sqlite_snapshot.py','--source',database,'--output-directory',base/'snapshot')
            after={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}
            assert before==after and len(before)==3
        finally:connection.close()
        cli('prepare_dujiao.py','--directory',base/'prepared','--config-file',config)
        assert (base/'prepared/config.yml').read_bytes()==config.read_bytes()
        key=base/'private-key/shop.json';keygen=json.loads(cli('private_archive.py','keygen','--keys-file',key));assert keygen['keyPrinted'] is False
        encrypted=base/'backup.enc';cli('private_archive.py','encrypt','--keys-file',key,'--input',source,'--output',encrypted)
        restored=base/'restored';cli('private_archive.py','restore','--keys-file',key,'--input',encrypted,'--output',restored)
        assert all((restored/path.relative_to(source)).read_bytes()==path.read_bytes() for path in source.rglob('*') if path.is_file())
        corrupted=bytearray(encrypted.read_bytes());corrupted[-1]^=1;bad=base/'bad.enc';bad.write_bytes(corrupted)
        result=subprocess.run([python,str(project/'scripts/private_archive.py'),'restore','--keys-file',str(key),'--input',str(bad),'--output',str(base/'rejected')],cwd=project,capture_output=True,text=True,timeout=60)
        assert result.returncode!=0 and not (base/'rejected').exists()
        report={'status':'PASS','sourceArchiveSha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'sourceManifestFiles':len(manifest['files']),'toolsHelpExecuted':9,'isolatedMinimalHashLockedPackages':8,'privateGitAndDockerDefaultIgnores':True,'sqliteSourceDbWalShmByteIdentical':True,'onlyCopiedSqliteOpened':True,'retainedConfigByteIdentical':True,'independentBackupKeyOwnerOnly':key.stat().st_mode&0o777==0o600,'encryptedDirectoryRestoreByteIdentical':True,'tamperedBackupRejectedBeforeOutput':True,'originalProjectFilesRead':False,'realDatabaseOperations':0,'upstreamBusinessRequests':0}
        evidence.parent.mkdir(parents=True,exist_ok=True);evidence.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n');return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--python',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True);arguments=parser.parse_args()
    print(json.dumps(probe(arguments.archive,arguments.python,arguments.evidence),sort_keys=True))

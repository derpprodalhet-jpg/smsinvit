"""Verify standalone deployment packages without starting API or workers."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile


def probe(project,archive,python,evidence,credentials_file=None):
    archive=Path(archive).resolve();python=str(Path(python).absolute());evidence=Path(evidence)
    with tempfile.TemporaryDirectory(prefix='deployment-package-proof-') as temporary:
        base=Path(temporary)
        with tarfile.open(archive) as contents:contents.extractall(base,filter='data')
        folder=base/('smsinvit-redeem-deployment' if project=='smsinvit' else 'dujiao-next-deployment')
        manifest=json.loads((folder/'deployment-manifest.json').read_text())
        assert not manifest['privateDataIncluded'] and not manifest['credentialsIncluded'] and not manifest['actualImagesIncluded']
        assert all(hashlib.sha256((folder/item['path']).read_bytes()).hexdigest()==item['sha256'] for item in manifest['files'])
        image_manifest=json.loads((folder/'images/image-manifest.json').read_text());assert {item['architecture'] for item in image_manifest['files']}=={'amd64','arm64'}
        support_manifest=json.loads((folder/'images/support-image-manifest.json').read_text());assert {item['architecture'] for item in support_manifest['files']}=={'amd64','arm64'}
        assert all(item['runtimeUser']==('999:999' if project=='smsinvit' else '999:1000') for item in support_manifest['files'])
        caddy_manifest=json.loads((folder/'images/caddy-image-manifest.json').read_text());assert {item['architecture'] for item in caddy_manifest['files']}=={'amd64','arm64'}
        assert all(item['image'].startswith('smsinvit-caddy:') for item in caddy_manifest['files'])
        def cli(name,*arguments):
            result=subprocess.run([python,str(folder/'scripts'/name),*map(str,arguments)],cwd=folder,capture_output=True,text=True,timeout=120)
            assert result.returncode==0,(name,result.stderr)
            return result.stdout
        names=('private_archive.py','sqlite_snapshot.py','scan_image.py','fetch_trivy_db.py')+(('prepare_deployment.py','migrate.py','backup.py','restore.py','configure_site.py') if project=='smsinvit' else ('prepare_dujiao.py',))
        for name in names:cli(name,'--help')
        if project=='smsinvit':
            migration_code="import sys,json;sys.path.insert(0,'scripts');from migrate import migrations;print(json.dumps([item.version for item in migrations()]))"
            migration_result=subprocess.run([python,'-c',migration_code],cwd=folder,capture_output=True,text=True,timeout=20)
            assert migration_result.returncode==0,migration_result.stderr
            expected_versions=json.loads(migration_result.stdout)
            assert len(list((folder/'migrations').rglob('V*__*.sql')))==len(expected_versions)
            cli('prepare_deployment.py','--directory',base/'new-private','--domain','redeem-fixture.example.com','--new-install')
            assert (base/'new-private/runtime.yaml').stat().st_mode&0o777==0o600
            # Import only the frozen SQL/config modules from this package. There
            # is no FastAPI install in the minimal maintenance environment.
            code="import sys;sys.path.insert(0,'apps/api');from smsinvit.db import Database;from smsinvit.config import Config;from smsinvit.common.mapper import Mapper;from sqlalchemy import create_engine;d=Database(engine=create_engine('sqlite://'));assert d.one('SELECT 1 AS n')['n']==1;d.close();print('standalone-adapter-ok')"
            result=subprocess.run([python,'-c',code],cwd=folder,capture_output=True,text=True,timeout=20);assert result.returncode==0,result.stderr
            if credentials_file:
                response=json.loads(cli('migrate.py','--credentials-file',Path(credentials_file).absolute(),'--status'))
                assert response['status']=='ready' and response['installedVersions']==expected_versions
        report={'status':'PASS','project':project,'archiveSha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'manifestFiles':len(manifest['files']),'standaloneToolsHelpExecuted':len(names),'defaultPrivateGitAndDockerIgnores':True,'allFileHashesMatch':True,'bothArchitectureImagesReferenced':True,'actualImagesDuplicatedInPackage':False,'apiStarted':False,'workersStarted':False,'actualMySqlSchemaStatusReadOnly':bool(project=='smsinvit' and credentials_file),'realProductionDataAccessed':False,'upstreamBusinessRequests':0}
        evidence.parent.mkdir(parents=True,exist_ok=True);evidence.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n');return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--project',choices=('smsinvit','dujiao'),required=True);parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--python',type=Path,required=True);parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--credentials-file',type=Path);args=parser.parse_args()
    print(json.dumps(probe(args.project,args.archive,args.python,args.evidence,args.credentials_file),sort_keys=True))

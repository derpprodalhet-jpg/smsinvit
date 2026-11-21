#!/usr/bin/env python3
"""Download the official public Trivy database with verified OCI digests.

This GET-only workaround does not read Docker credentials or install software.
It supplies Trivy's documented offline cache layout. Public tokens stay in
memory. Reference: https://trivy.dev/docs/latest/guide/advanced/air-gap/
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path,PurePosixPath
import tarfile
import tempfile
import requests
from release import ReleaseError

REPOSITORY='https://ghcr.io/v2/aquasecurity/trivy-db/'
MAX_LAYER=512*1024*1024

def digest(data):return 'sha256:'+hashlib.sha256(data).hexdigest()

def fetch(cache):
    cache=Path(cache).resolve()
    if 'codex_invit' in cache.parts:raise ReleaseError('The original invitation project cannot be used as a scanner cache.')
    cache.mkdir(parents=True,exist_ok=True)
    session=requests.Session()
    token_response=session.get('https://ghcr.io/token',params={'service':'ghcr.io','scope':'repository:aquasecurity/trivy-db:pull'},timeout=25)
    token_response.raise_for_status()
    headers={'Authorization':'Bearer '+token_response.json()['token'],'Accept':'application/vnd.oci.image.manifest.v1+json'}
    response=session.get(REPOSITORY+'manifests/2',headers=headers,timeout=30);response.raise_for_status()
    manifest_digest=response.headers.get('Docker-Content-Digest')
    if len(response.content)>1024*1024 or manifest_digest!=digest(response.content):raise ReleaseError('The official database manifest digest did not match.')
    manifest=response.json();layers=manifest.get('layers',[])
    if len(layers)!=1 or layers[0].get('mediaType')!='application/vnd.aquasec.trivy.db.layer.v1.tar+gzip':raise ReleaseError('Unsupported official database artifact format.')
    layer=layers[0]
    if not isinstance(layer.get('size'),int) or not 0<layer['size']<=MAX_LAYER:raise ReleaseError('The official database artifact exceeds the download limit.')
    with tempfile.TemporaryDirectory(prefix='verified-trivy-db-',dir=cache) as temporary:
        archive=Path(temporary)/'db.tar.gz';length=0;hashvalue=hashlib.sha256()
        with session.get(REPOSITORY+'blobs/'+layer['digest'],headers=headers,timeout=(25,60),stream=True) as blob:
            blob.raise_for_status()
            with archive.open('xb') as output:
                for chunk in blob.iter_content(1024*1024):
                    length+=len(chunk)
                    if length>layer['size']:raise ReleaseError('The official database artifact size did not match.')
                    hashvalue.update(chunk);output.write(chunk)
        if length!=layer['size'] or 'sha256:'+hashvalue.hexdigest()!=layer['digest']:raise ReleaseError('The official database layer digest did not match.')
        extracted={}
        with tarfile.open(archive,'r:gz') as source:
            for member in source.getmembers():
                name=PurePosixPath(member.name)
                if member.isdir() and name.parts==():continue
                if len(name.parts)!=1 or name.name not in ('trivy.db','metadata.json') or not member.isfile() or name.name in extracted or member.size>4*1024*1024*1024:
                    raise ReleaseError('Unexpected database archive member '+json.dumps(member.name[:160])+': no archive paths are extracted.')
                file=source.extractfile(member)
                if file is None:raise ReleaseError('The database archive member is unreadable.')
                destination=Path(temporary)/name.name
                with destination.open('xb') as output:
                    while chunk:=file.read(1024*1024):output.write(chunk)
                extracted[name.name]=destination
        if set(extracted)!= {'trivy.db','metadata.json'}:raise ReleaseError('The official database archive is incomplete.')
        metadata=json.loads(extracted['metadata.json'].read_text())
        now=datetime.now(timezone.utc)
        updated=datetime.fromisoformat(metadata['UpdatedAt'].replace('Z','+00:00'))
        if metadata.get('Version')!=2 or not 0<=(now-updated).total_seconds()<=48*3600:raise ReleaseError('The official vulnerability database is incompatible or more than 48 hours old.')
        directory=cache/'db'
        if directory.is_symlink():raise ReleaseError('Scanner database directory cannot be a symlink.')
        directory.mkdir(exist_ok=True)
        files=[]
        for name,file in extracted.items():
            if (directory/name).is_symlink():raise ReleaseError('Scanner database files cannot be symlinks.')
            with file.open('rb') as source:checksum=hashlib.file_digest(source,'sha256').hexdigest()
            files.append({'path':name,'size':file.stat().st_size,'sha256':checksum})
        for name,file in extracted.items():file.replace(directory/name)
        evidence={'format':'smsinvit-trivy-db-v1','source':REPOSITORY+'manifests/2','manifestDigest':manifest_digest,'layerDigest':layer['digest'],'downloadedAt':now.isoformat(),'metadata':metadata,'files':files,'contentVerified':True}
        (directory/'download-evidence.json').write_text(json.dumps(evidence,sort_keys=True,indent=2)+'\n')
        return evidence

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--cache-dir',type=Path,required=True);args=parser.parse_args()
    try:print(json.dumps(fetch(args.cache_dir),sort_keys=True),flush=True)
    except ReleaseError as error:parser.exit(1,str(error)+'\n')
    except (requests.RequestException,KeyError,ValueError,OSError) as error:parser.exit(1,'Official scanner database download failed ('+type(error).__name__+').\n')

if __name__=='__main__':main()

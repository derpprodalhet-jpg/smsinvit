"""Fetch official OCI content with TLS and digest checks, then load an immutable local cache.

This optional path works around registry HEAD failures. Every index, manifest,
configuration, compressed layer, and uncompressed diff ID is verified. No Docker
login file or user credential is read; the public pull token is memory-only.
"""
from __future__ import annotations
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import requests
from release import ReleaseError

ACCEPT='application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json'

def check(data,digest):
    if 'sha256:'+hashlib.sha256(data).hexdigest()!=digest:raise ReleaseError('Official OCI content digest did not match; refusing the image.')

def pull(image,architecture,cache,key):
    repository,digest=image.split('@');repository='library/'+repository;session=requests.Session()
    token=None
    for attempt in range(3):
        try:
            response=session.get('https://auth.docker.io/token',params={'service':'registry.docker.io','scope':'repository:'+repository+':pull'},timeout=30);response.raise_for_status();token=response.json()['token'];break
        except (requests.RequestException,KeyError,ValueError):continue
    if not token:raise ReleaseError('The official public image download token was unavailable.') from None
    headers={'Authorization':'Bearer '+token,'Accept':ACCEPT}
    def fetch(path,expected):
        for attempt in range(3):
            try:
                response=session.get('https://registry-1.docker.io/v2/'+repository+'/'+path,headers=headers,timeout=60);response.raise_for_status();data=response.content;check(data,expected);return data
            except requests.RequestException:continue
        raise ReleaseError('Official verified OCI content download failed.')
    index=json.loads(fetch('manifests/'+digest,digest))
    platform=next((item for item in index.get('manifests',[]) if item.get('platform',{}).get('os')=='linux' and item['platform'].get('architecture')==architecture),None)
    if platform is None:raise ReleaseError('The official index does not contain the selected Linux architecture.')
    manifest=json.loads(fetch('manifests/'+platform['digest'],platform['digest']));config_digest=manifest['config']['digest'];config_data=fetch('blobs/'+config_digest,config_digest);config=json.loads(config_data)
    if config['architecture']!=architecture or config['os']!='linux':raise ReleaseError('Official image config platform mismatch.')
    tag='smsinvit-verified-'+key+':'+digest.split(':')[1][:16]+'-'+architecture
    def matches(item):
        runtime_fields=('Env','Entrypoint','Cmd','User','WorkingDir','Volumes','ExposedPorts','Healthcheck','Labels','StopSignal','OnBuild','Shell')
        return item['Architecture']==architecture and item['Os']=='linux' and item['RootFS']['Layers']==config['rootfs']['diff_ids'] and all(item['Config'].get(field)==config['config'].get(field) for field in runtime_fields)
    existing=subprocess.run(['docker','image','inspect',tag],capture_output=True,text=True,check=False)
    if existing.returncode==0:
        item=json.loads(existing.stdout)[0]
        if matches(item):return tag,{'officialIndex':digest,'platformManifest':platform['digest'],'configDigest':config_digest,'loadedImageId':item['Id'],'contentVerified':True}
        raise ReleaseError('A local verified-image cache tag has unexpected content.')
    cache=Path(cache);cache.mkdir(parents=True,exist_ok=True);directory=cache/(key+'-'+architecture);directory.mkdir(exist_ok=True);layers=[]
    if len(config['rootfs']['diff_ids'])!=len(manifest['layers']):raise ReleaseError('Official image layer count mismatch.')
    for ordinal,layer in enumerate(manifest['layers']):
        compressed=directory/(layer['digest'].split(':')[1]+'.blob');raw=directory/(str(ordinal)+'-layer.tar')
        valid=False
        if compressed.exists():valid='sha256:'+hashlib.sha256(compressed.read_bytes()).hexdigest()==layer['digest']
        if not valid:
            data=fetch('blobs/'+layer['digest'],layer['digest']);compressed.write_bytes(data)
        if not raw.exists() or 'sha256:'+hashlib.sha256(raw.read_bytes()).hexdigest()!=config['rootfs']['diff_ids'][ordinal]:
            media=layer.get('mediaType','');data=compressed.read_bytes()
            if media.endswith('+gzip') or media.endswith('.gzip'):data=gzip.decompress(data)
            elif media.endswith('+zstd'):raise ReleaseError('zstd layers are not supported by the optional download workaround.')
            check(data,config['rootfs']['diff_ids'][ordinal]);raw.write_bytes(data)
        layers.append(raw)
    archive=directory/'docker-load.tar'
    with tarfile.open(archive,'w') as output:
        name=config_digest.split(':')[1]+'.json';info=tarfile.TarInfo(name);info.size=len(config_data);output.addfile(info,io.BytesIO(config_data))
        layer_names=[]
        for ordinal,raw in enumerate(layers):
            entry=str(ordinal)+'/layer.tar';output.add(raw,arcname=entry);layer_names.append(entry)
        data=json.dumps([{'Config':name,'RepoTags':[tag],'Layers':layer_names}]).encode();info=tarfile.TarInfo('manifest.json');info.size=len(data);output.addfile(info,io.BytesIO(data))
    result=subprocess.run(['docker','load','--input',str(archive)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,check=False)
    if result.returncode:raise ReleaseError('The verified official image could not be loaded into Docker.')
    archive.unlink()
    descriptor=json.loads(subprocess.run(['docker','image','inspect',tag],capture_output=True,text=True,check=True).stdout)[0]
    # Containerd may normalize the Docker archive manifest, producing a new
    # manifest ID. Verified rootfs diff IDs and all execution config must match.
    if not matches(descriptor):raise ReleaseError('Loaded image content or execution config does not match the verified official image.')
    return tag,{'officialIndex':digest,'platformManifest':platform['digest'],'configDigest':config_digest,'loadedImageId':descriptor['Id'],'contentVerified':True}

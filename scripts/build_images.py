#!/usr/bin/env python3
"""Build digest-pinned allowlisted images and perform isolated architecture smoke checks."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile
import requests
from release import ROOT,ReleaseError,stage

BASE_IMAGES={'python':'python:3.12-slim-trixie','node':'node:24-trixie-slim','mysql':'mysql:8.4','caddy':'caddy:2-alpine','redis':'redis:7.4-alpine','dujiao_node':'node:24-alpine','dujiao_go':'golang:1.27.1-alpine','dujiao_alpine':'alpine:latest'}

def command(args,*,capture=True,timeout=300):
    result=subprocess.run(args,text=True,stdout=subprocess.PIPE if capture else None,stderr=subprocess.PIPE if capture else None,timeout=timeout,check=False)
    if result.returncode:raise ReleaseError(f'Command failed with exit {result.returncode}: '+' '.join(args[:3]))
    return result.stdout or ''

def resolve_images(path,keys,refresh=False):
    lock=json.loads(path.read_text()) if path.exists() and not refresh else {'format':'smsinvit-image-lock-v1','images':{}}
    for key in keys:
        if key in lock['images'] and lock['images'][key]['requested']==BASE_IMAGES[key]:continue
        tag=BASE_IMAGES[key];repository,version=tag.rsplit(':',1)
        # Docker Hub's official metadata endpoint avoids CLI credential-helper
        # stalls. Pulls still validate the exact official content digest.
        endpoint=f'https://hub.docker.com/v2/namespaces/library/repositories/{repository}/tags/{version}'
        metadata=None
        for attempt in range(3):
            try:
                response=requests.get(endpoint,timeout=25);response.raise_for_status();metadata=response.json();break
            except (requests.RequestException,ValueError):continue
        if metadata is None:raise ReleaseError(f'Official Docker Hub metadata could not be read for {tag}.') from None
        digest=metadata.get('digest','');architectures={item.get('architecture') for item in metadata.get('images',[])}
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest) or not {'amd64','arm64'}.issubset(architectures):raise ReleaseError(f'Official manifest for {tag} does not include both required architectures.')
        lock['images'][key]={'requested':tag,'digest':digest,'image':repository+'@'+digest,'metadataSource':endpoint}
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(lock,sort_keys=True,indent=2)+'\n')
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(lock,sort_keys=True,indent=2)+'\n');return lock

def smoke(image,architecture,project,target):
    platform='linux/'+architecture
    if project=='smsinvit':
        prefix=['docker','run','--rm','--platform',platform,'--network','none','--read-only','--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--entrypoint']
        version=command(prefix+['codex',image,'--version']).strip()
        helptext=command(prefix+['codex',image,'exec','--help'])
        required=['--skip-git-repo-check','--ignore-user-config','--ephemeral','--json','--sandbox']
        missing=[flag for flag in required if flag not in helptext]
        if missing:raise ReleaseError('Pinned Codex CLI does not support source argv: '+','.join(missing))
        modules='import fastapi,sqlalchemy,cryptography,curl_cffi,requests,PIL,pyotp; import sys; print(sys.version.split()[0])'
        if target=='runtime':modules+='; import smsinvit.app; print("native_api_import_ok")'
        python=command(prefix+['python',image,'-c',modules]).strip()
        return {'network':'none','cliVersion':version,'sourceArgvCompatible':True,'python':python,'upstreamBusinessRequests':0}
    # The main binary parses ordinary flags after startup/migrations. This help
    # branch exits before config, database, file operations, or service startup.
    command(['docker','run','--rm','--platform',platform,'--network','none','--read-only','--entrypoint','./dujiao-next',image,'rollback','--help'])
    return {'network':'none','nativeBinaryHelpExecuted':True,'upstreamBusinessRequests':0}

def build(args):
    args.builder=args.builder or command(['docker','context','show']).strip()
    keys=['python','node','mysql','caddy','redis'] if args.project=='smsinvit' else ['dujiao_node','dujiao_go','dujiao_alpine','caddy','redis']
    lock=resolve_images(args.lock_file,keys,args.refresh_lock)
    downloaded={}
    def image_for(key,architecture):
        official=lock['images'][key]['image']
        if args.verified_local_cache:
            from pull_verified import pull
            tag,evidence=pull(official,architecture,ROOT/'artifacts/base-image-cache',key);downloaded[key+'-'+architecture]=evidence;return tag
        if not args.registry_mirror:return official
        if not re.fullmatch('[A-Za-z0-9.-]+(?::[0-9]+)?',args.registry_mirror):raise ReleaseError('Registry mirror must be an explicit hostname without credentials or URL components.')
        return args.registry_mirror+'/library/'+official
    records=[]
    with tempfile.TemporaryDirectory(prefix='smsinvit-image-') as temporary:
        context=Path(temporary)/'source';manifest=stage(args.source,context,args.project,args.version)
        native=command(['docker','info','--format','{{.Architecture}}']).strip();native={'aarch64':'arm64','x86_64':'amd64'}.get(native,native)
        original_dockerfile=(context/'Dockerfile').read_text() if args.project=='dujiao' else ''
        for arch in args.architectures.split(','):
            if arch not in ('amd64','arm64'):raise ReleaseError('Only tested amd64 and arm64 architectures are supported.')
            if args.project=='smsinvit':
                dockerfile=context/'deploy/Dockerfile';buildargs=['--build-arg','PYTHON_IMAGE='+image_for('python',arch),'--build-arg','NODE_IMAGE='+image_for('node',arch),'--build-arg','NODE_BUILD_IMAGE='+image_for('node',native),'--build-arg','VERSION='+args.version]
            else:
                dockerfile=context/'Dockerfile';text=re.sub(r'^# syntax=.*\n','',original_dockerfile)
                for key,prefix in [('dujiao_node','node'),('dujiao_go','golang'),('dujiao_alpine','alpine')]:
                    pinned=image_for(key,arch if key=='dujiao_alpine' else native)
                    text=re.sub(r'^(FROM\s+(?:--platform=\S+\s+)?)'+prefix+r':[^\s]+',lambda match:match[1]+pinned,text,flags=re.M)
                dockerfile.write_text(text);buildargs=['--build-arg','APP_VERSION='+args.version]
            if args.build_proxy:
                from urllib.parse import urlsplit
                proxy=urlsplit(args.build_proxy)
                if proxy.scheme not in ('http','https') or not proxy.hostname or proxy.username or proxy.password or proxy.query or proxy.fragment:raise ReleaseError('Build proxy must have no embedded credentials.')
                buildargs+=['--build-arg','HTTP_PROXY='+args.build_proxy,'--build-arg','HTTPS_PROXY='+args.build_proxy]
            prefix='smsinvit-python' if args.project=='smsinvit' else 'dujiao-next'
            tag=f'{prefix}:{args.version}-{arch}-local'+('-deps' if args.target=='dependencies' else '-runtime-base' if args.target=='runtime-base' else '')
            argv=['docker','buildx','build','--builder',args.builder,'--platform','linux/'+arch,'--load','--tag',tag,'--file',str(dockerfile),'--provenance=false','--sbom=false']
            if args.project=='smsinvit':argv+=['--target',args.target]
            argv+=buildargs+[str(context)]
            command(argv,capture=False,timeout=3600)
            evidence=smoke(tag,arch,args.project,args.target)
            descriptor=json.loads(command(['docker','image','inspect',tag]))[0]
            if descriptor['Architecture']!=arch:raise ReleaseError('Built image architecture did not match requested target.')
            if args.target in ('runtime','runtime-base') and descriptor['Config'].get('User','')!='10001:10001':raise ReleaseError('Runtime must execute as the dedicated unprivileged account.')
            from hashlib import sha256
            records.append({'image':tag,'imageId':descriptor['Id'],'architecture':arch,'runtimeUser':descriptor['Config'].get('User',''),'target':args.target,'effectiveDockerfileSha256':sha256(dockerfile.read_bytes()).hexdigest(),'status':'passed','smoke':evidence})
            args.evidence.parent.mkdir(parents=True,exist_ok=True)
            args.evidence.write_text(json.dumps({'format':'smsinvit-build-evidence-v1','builtAt':datetime.now(timezone.utc).isoformat(),'project':args.project,'version':args.version,'sourceFiles':manifest['files'],'baseImages':lock['images'],'downloadMirror':args.registry_mirror,'verifiedLocalCache':downloaded,'builds':records},sort_keys=True,indent=2)+'\n')
            print(json.dumps(records[-1],ensure_ascii=False),flush=True)
    return records

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--project',choices=['smsinvit','dujiao'],default='smsinvit');parser.add_argument('--source',type=Path,default=ROOT);parser.add_argument('--version',default='v1.0.0-python');parser.add_argument('--architectures',default='amd64,arm64');parser.add_argument('--target',choices=['dependencies','runtime-base','runtime'],default='runtime');parser.add_argument('--builder',help='Explicit buildx builder; defaults to the current Docker context builder.');parser.add_argument('--lock-file',type=Path,default=ROOT/'deploy/images.lock.json');parser.add_argument('--refresh-lock',action='store_true');parser.add_argument('--registry-mirror',help='Optional download host; exact verified official content digest is retained.');parser.add_argument('--evidence',type=Path,default=ROOT/'artifacts/build-evidence.json')
    parser.add_argument('--verified-local-cache',action='store_true',help='Work around registry HEAD failures with verified official GET downloads.');parser.add_argument('--build-proxy',help='Optional credential-free build-only HTTP proxy; not retained in the runtime image.')
    args=parser.parse_args()
    try:
        if not re.fullmatch('[A-Za-z0-9_.-]{1,64}',args.version):raise ReleaseError('Invalid image version.')
        build(args)
    except (ReleaseError,subprocess.TimeoutExpired) as error:parser.exit(1,str(error)+'\n')

if __name__=='__main__':main()

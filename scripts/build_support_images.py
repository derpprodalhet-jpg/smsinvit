#!/usr/bin/env python3
"""Build minimally patched, digest-pinned support images without business startup."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import tempfile
import requests
from pull_verified import pull
from release import ROOT, ReleaseError

USERS = {'mysql': '999:999', 'redis': '999:1000', 'caddy':'10001:10001'}
PREFIXES = {'mysql': 'smsinvit-mysql', 'redis': 'dujiao-redis', 'caddy':'smsinvit-caddy'}

def package_files(architecture, context):
    lock=json.loads((ROOT/'deploy/support/packages.lock.json').read_text())
    selected=[item for item in lock['packages'] if item['architecture']==architecture]
    if {item['filename'] for item in selected}!={'libcrypto3-3.3.7-r2.apk','libssl3-3.3.7-r2.apk'}:
        raise ReleaseError('The exact signed runtime-library package lock is required.')
    destination=context/'packages';destination.mkdir()
    for item in selected:
        from urllib.parse import urlsplit
        parsed=urlsplit(item['source'])
        if parsed.scheme!='https' or parsed.hostname not in ('dl-4.alpinelinux.org','dl-cdn.alpinelinux.org','dl-5.alpinelinux.org') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ReleaseError('A frozen official HTTPS package source is required.')
        cached=ROOT/'artifacts/support-apk-cache'/item['apkArchitecture']/item['filename']
        cached.parent.mkdir(parents=True,exist_ok=True)
        if not cached.is_file() or hashlib.sha256(cached.read_bytes()).hexdigest()!=item['sha256']:
            data=None
            for attempt in range(3):
                try:
                    response=requests.get(item['source'],params={'smsinvit_security_build':'20261007'},timeout=40)
                    response.raise_for_status();candidate=response.content
                    if len(candidate)==item['bytes'] and hashlib.sha256(candidate).hexdigest()==item['sha256']:
                        data=candidate;break
                except requests.RequestException:continue
            if data is None:raise ReleaseError('The frozen official signed library package is unavailable or changed.')
            cached.write_bytes(data)
        shutil.copyfile(cached,destination/item['filename'])

def run(argv, timeout=300):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ReleaseError('Support-image command failed: ' + ' '.join(argv[:3]) + '\n' + result.stderr[-4000:])
    return result.stdout

def build(kinds, architectures, version, evidence, proxy=None,builder=None):
    builder=builder or run(['docker','context','show']).strip()
    lock = json.loads((ROOT/'deploy/images.lock.json').read_text())
    builds = []
    for kind in kinds:
        if kind not in USERS:
            raise ReleaseError('Only the frozen MySQL/Redis/Caddy support images are allowed.')
        source = ROOT/'deploy/support'/(kind+'.Dockerfile')
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        for architecture in architectures:
            if architecture not in ('amd64', 'arm64'):
                raise ReleaseError('Only amd64 and arm64 support images are allowed.')
            base, base_proof = pull(lock['images'][kind]['image'], architecture, ROOT/'artifacts/verified-support-cache', kind)
            image = PREFIXES[kind] + ':' + version + '-' + architecture + '-local'
            argv = ['docker', 'buildx', 'build', '--builder', builder, '--platform', 'linux/'+architecture, '--load', '--provenance=false', '--sbom=false', '--tag', image, '--file', str(source), '--build-arg', kind.upper()+'_BASE='+base]
            if proxy:
                from urllib.parse import urlsplit
                parsed = urlsplit(proxy)
                if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ReleaseError('Build proxy must be an explicit credential-free HTTP endpoint.')
                argv += ['--build-arg','HTTP_PROXY='+proxy,'--build-arg','HTTPS_PROXY='+proxy]
            print(json.dumps({'building': image}), flush=True)
            with tempfile.TemporaryDirectory(prefix='smsinvit-support-image-') as temporary:
                context=Path(temporary)
                if kind=='redis':package_files(architecture,context)
                argv += [str(context)]
                output = run(argv, 900)
            actual = json.loads(run(['docker','image','inspect',image]))[0]
            if actual['Architecture'] != architecture or actual['Config']['User'] != USERS[kind]:
                raise ReleaseError('Support image architecture or unprivileged account mismatch.')
            command = ['mysqld','--version'] if kind=='mysql' else ['redis-server','--version'] if kind=='redis' else ['caddy','version']
            smoke = run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--entrypoint',command[0],image]+command[1:])
            if (kind=='mysql' and '8.4.11' not in smoke) or (kind=='redis' and '7.4.11' not in smoke) or (kind=='caddy' and not smoke.startswith('v2.')):
                raise ReleaseError('The support component business version changed.')
            builds.append({'image': image,'imageId':actual['Id'],'architecture':architecture,'runtimeUser':USERS[kind],'target':'runtime','kind':kind,'dockerfileSha256':source_hash,'officialBase':base_proof,'status':'passed','smoke':{'version':smoke.strip(),'network':'none','businessServicesStarted':False}})
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(json.dumps({'format':'smsinvit-support-build-v1','builtAt':datetime.now(timezone.utc).isoformat(),'builds':builds},indent=2,sort_keys=True)+'\n')
            print(json.dumps(builds[-1]),flush=True)
    return builds

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kinds',default='mysql,redis,caddy');parser.add_argument('--architectures',default='amd64,arm64')
    parser.add_argument('--version',default='v1.0.0-python');parser.add_argument('--evidence',type=Path,default=ROOT/'artifacts/support-build-evidence.json');parser.add_argument('--build-proxy');parser.add_argument('--builder',help='Explicit buildx builder; defaults to the current Docker context builder.')
    args=parser.parse_args()
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',args.version):parser.error('Invalid version.')
    try:build(args.kinds.split(','),args.architectures.split(','),args.version,args.evidence,args.build_proxy,args.builder)
    except (ReleaseError,subprocess.TimeoutExpired) as error:parser.exit(1,str(error)+'\n')

#!/usr/bin/env python3
"""Export verified local deployment images without starting containers."""
import argparse
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

RUNTIME_USERS={'smsinvit-python':'10001:10001','dujiao-next':'10001:10001','smsinvit-mysql':'999:999','dujiao-redis':'999:1000','smsinvit-caddy':'10001:10001'}

def inspect(image):
    result=subprocess.run(['docker','image','inspect',image],capture_output=True,text=True,timeout=30,check=False)
    if result.returncode:raise ValueError('A requested image is not available locally.')
    item=json.loads(result.stdout)[0]
    return {'image':image,'imageId':item['Id'],'architecture':item['Architecture'],'runtimeUser':item['Config']['User']}

def export(images,output,evidence):
    output=Path(output).resolve()
    if output.exists():raise ValueError('The image export directory must be new; existing artifacts are never overwritten.')
    if 'codex_invit' in output.parts:raise ValueError('The protected original invitation project cannot be an export destination.')
    verified=json.loads(Path(evidence).read_text());records={record['image']:record for record in verified['builds'] if record.get('status')=='passed' and record.get('target')=='runtime'}
    if not images or len(images)!=len(set(images)):raise ValueError('Select distinct explicitly verified runtime images.')
    descriptors=[]
    for image in images:
        if not re.fullmatch(r'(?:smsinvit-python|dujiao-next|smsinvit-mysql|dujiao-redis|smsinvit-caddy):[A-Za-z0-9_.-]+',image):raise ValueError('Only explicit locally built deployment tags may be exported.')
        descriptor=inspect(image)
        if image not in records or descriptor['imageId']!=records[image]['imageId'] or descriptor['architecture']!=records[image]['architecture'] or descriptor['runtimeUser']!=RUNTIME_USERS[image.split(':')[0]]:raise ValueError('The runtime image does not match completed build evidence.')
        descriptors.append(descriptor)
    output.mkdir(mode=0o700,parents=True)
    files=[]
    for descriptor in descriptors:
        image=descriptor['image'];name=image.replace(':','-')+'.tar.gz'
        with tempfile.TemporaryDirectory(prefix='local-image-export-',dir=output) as temporary:
            archive=Path(temporary)/'image.tar'
            result=subprocess.run(['docker','image','save','--output',str(archive),image],capture_output=True,timeout=300,check=False)
            if result.returncode:raise ValueError('The verified image could not be exported.')
            if inspect(image)!=descriptor:raise ValueError('The local image changed while exporting.')
            destination=output/name
            with destination.open('xb') as raw,gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0,compresslevel=6) as compressed,archive.open('rb') as source:shutil.copyfileobj(source,compressed,1024*1024)
        destination.chmod(0o600)
        with destination.open('rb') as source:checksum=hashlib.file_digest(source,'sha256').hexdigest()
        files.append({**descriptor,'archive':name,'size':destination.stat().st_size,'sha256':checksum})
    with Path(evidence).open('rb') as source:build_checksum=hashlib.file_digest(source,'sha256').hexdigest()
    manifest={'format':'smsinvit-image-export-v1','exportedAt':datetime.now(timezone.utc).isoformat(),'buildEvidenceSha256':build_checksum,'businessServicesStarted':False,'runtimeDataMounted':False,'files':files}
    target=output/'image-manifest.json';target.write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n');target.chmod(0o600)
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--image',action='append',required=True);parser.add_argument('--output-directory',type=Path,required=True);parser.add_argument('--build-evidence',type=Path,required=True);args=parser.parse_args()
    try:print(json.dumps(export(args.image,args.output_directory,args.build_evidence),sort_keys=True))
    except (ValueError,OSError,KeyError,subprocess.TimeoutExpired) as error:parser.exit(1,'Image export refused ('+type(error).__name__+').\n')

if __name__=='__main__':main()

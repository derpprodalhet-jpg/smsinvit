#!/usr/bin/env python3
"""Record bounded local-image CVE scans without registry or online fallbacks."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import subprocess
import tarfile
import tempfile

def export_rootfs(image,directory):
    """Export a never-started local container and extract within a new directory.

    Docker 29 containerd layer archives are not understood by every scanner.
    Trivy documents merged-rootfs scanning using docker create/export. This
    checks installed runtime packages, rather than deleted historical layers.
    """
    created=subprocess.run(['docker','create','--network','none','--read-only','--entrypoint','/bin/true',image],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30,check=False)
    if created.returncode or not re.fullmatch('[0-9a-f]{64}',created.stdout.strip()):raise ValueError('The isolated export container could not be created.')
    identifier=created.stdout.strip();archive=directory/'rootfs.tar';rootfs=directory/'rootfs';rootfs.mkdir(mode=0o700)
    try:
        result=subprocess.run(['docker','export','--output',str(archive),identifier],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=180,check=False)
        if result.returncode:raise ValueError('The isolated container filesystem could not be exported.')
        links=[];size=0;files=0
        with tarfile.open(archive,'r:') as contents:
            for member in contents:
                relative=PurePosixPath(member.name)
                if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe exported filesystem archive path.')
                target=rootfs.joinpath(*relative.parts)
                if member.isdir():target.mkdir(parents=True,exist_ok=True)
                elif member.isfile() or member.islnk():
                    size+=member.size;files+=1
                    if size>8*1024*1024*1024 or files>200000:raise ValueError('Exported filesystem exceeds scan bounds.')
                    data=contents.extractfile(member)
                    if data is None:raise ValueError('Exported package file is unreadable.')
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with target.open('xb') as output:
                        while chunk:=data.read(1024*1024):output.write(chunk)
                    # Go binary discovery requires executable mode. Preserve
                    # that fact without carrying setuid/setgid into host files.
                    target.chmod(0o755 if member.mode&0o111 else 0o644)
                elif member.issym():links.append((target,member.linkname))
        # Rebase absolute system symlinks into this export; no host path can be
        # reached. Create links only after writes have finished.
        for target,link in links:
            linked=rootfs/link.lstrip('/') if link.startswith('/') else target.parent/link
            linked=linked.resolve()
            if not linked.is_relative_to(rootfs.resolve()):continue
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists() and not target.is_symlink():target.symlink_to(os.path.relpath(linked,target.parent))
        with archive.open('rb') as data:checksum=hashlib.file_digest(data,'sha256').hexdigest()
        return rootfs,{'method':'never_started_docker_create_export','archiveSha256':checksum,'regularFiles':files,'containerStarted':False,'network':'none','volumesMounted':False,'coverage':'installed_runtime_packages'}
    finally:
        subprocess.run(['docker','rm','--force',identifier],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,check=False)

def descriptor(image):
    result=subprocess.run(['docker','image','inspect',image],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30,check=False)
    if result.returncode:raise ValueError('The requested image is not available locally.')
    item=json.loads(result.stdout)[0]
    return {'imageId':item['Id'],'architecture':item['Architecture']}

def scan(image,directory,trivy=None,cache=None,timeout=240,rootfs=False):
    if not re.fullmatch('[A-Za-z0-9_./:@-]+',image):raise ValueError('An explicit local image tag is required.')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    backend='trivy-rootfs' if trivy and rootfs else 'trivy' if trivy else 'docker-scout';name=re.sub('[^A-Za-z0-9_.-]','_',image)
    report=directory/(name+'.'+backend+'.json');status=directory/(name+'.'+backend+'.status.json')
    summary={'format':'smsinvit-image-security-v1','image':image,'scannedAt':datetime.now(timezone.utc).isoformat(),'severity':'critical,high','backend':backend,'status':'unavailable','findings':None,'report':None}
    try:
        before=descriptor(image);summary.update(before)
        identifier=before['imageId'].split(':')[-1][:16]
        report=directory/(name+'-'+identifier+'.'+backend+'.json');status=directory/(name+'-'+identifier+'.'+backend+'.status.json')
        if report.exists():report.unlink()
        if trivy:
            trivy=Path(trivy).resolve(strict=True)
            if cache is None:raise ValueError('Trivy requires an explicitly downloaded offline database cache.')
            cache=Path(cache).resolve(strict=True);metadata=json.loads((cache/'db/metadata.json').read_text())
            updated=datetime.fromisoformat(metadata['UpdatedAt'].replace('Z','+00:00'))
            age=(datetime.now(timezone.utc)-updated).total_seconds()
            if metadata.get('Version')!=2 or not 0<=age<=48*3600:raise ValueError('The scanner database is incompatible or more than 48 hours old.')
            summary['database']=metadata;summary['scannerSha256']=hashlib.sha256(trivy.read_bytes()).hexdigest()
            context=subprocess.run(['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30,check=False)
            if context.returncode or not context.stdout.strip().startswith('unix://'):raise ValueError('An explicit local Docker socket is required for scanning.')
            argv=[str(trivy),'image','--image-src','docker','--docker-host',context.stdout.strip(),'--platform','linux/'+before['architecture'],'--cache-dir',str(cache),'--skip-db-update','--skip-java-db-update','--skip-check-update','--skip-version-check','--offline-scan','--scanners','vuln','--severity','HIGH,CRITICAL','--format','json','--output',str(report),'--timeout',str(timeout)+'s','--exit-code','1',image]
        else:
            argv=['docker','scout','cves','local://'+image,'--only-severity','critical,high','--format','sarif','--output',str(report),'--exit-code']
        with tempfile.TemporaryDirectory(prefix='smsinvit-image-scan-') as temporary:
            if trivy and rootfs:
                exported,summary['filesystemExport']=export_rootfs(image,Path(temporary))
                argv=[str(trivy),'rootfs','--cache-dir',str(cache),'--skip-db-update','--skip-java-db-update','--skip-check-update','--skip-version-check','--offline-scan','--scanners','vuln','--severity','HIGH,CRITICAL','--format','json','--output',str(report),'--timeout',str(timeout)+'s','--exit-code','1',str(exported)]
            result=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,check=False,timeout=timeout)
        summary['exitCode']=result.returncode
        if report.is_file():
            data=json.loads(report.read_text());summary['report']=report.name;summary['reportSha256']=hashlib.sha256(report.read_bytes()).hexdigest()
            if trivy:
                if not isinstance(data.get('Results'),list):raise ValueError('The scanner did not provide an image inventory.')
                summary['inventoryTypes']=[{'type':item.get('Type'),'packages':len(item.get('Packages',[]))} for item in data['Results']]
                types={item.get('Type') for item in data['Results']}
                if image.startswith('dujiao-next:') and 'gobinary' not in types:raise ValueError('The scanner did not inspect the storefront Go binary.')
                if image.startswith('smsinvit-python:') and not {'python-pkg','node-pkg'}.issubset(types):raise ValueError('The scanner did not inspect Python and CLI package inventories.')
                findings=sum(len(item.get('Vulnerabilities',[])) for item in data['Results'])
            else:
                if not isinstance(data.get('runs'),list):raise ValueError('The scanner report is incomplete.')
                findings=sum(len(item.get('results',[])) for item in data['runs'])
            if descriptor(image)!=before:raise ValueError('The local image changed during scanning.')
            summary['findings']=findings
            if result.returncode==0 and findings==0:summary['status']='passed'
            elif findings>0:summary['status']='findings'
    except subprocess.TimeoutExpired:
        summary['exitCode']=124;summary['reason']='scanner_timeout'
    except (OSError,ValueError,KeyError,IndexError,tarfile.TarError):
        summary['reason']='scan_or_database_unavailable'
    status.write_text(json.dumps(summary,sort_keys=True,indent=2)+'\n');print(json.dumps(summary),flush=True)
    return 0 if summary['status']=='passed' else 2 if summary['status']=='findings' else 1

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--image',required=True)
    parser.add_argument('--output-directory',type=Path,default=Path('artifacts/image-security'));parser.add_argument('--trivy',type=Path)
    parser.add_argument('--cache-dir',type=Path);parser.add_argument('--timeout',type=int,default=240)
    parser.add_argument('--export-rootfs',action='store_true',help='Scan installed packages in the filesystem of a never-started local container.');args=parser.parse_args()
    if not 1<=args.timeout<=600:parser.error('Scanner timeout must be between 1 and 600 seconds.')
    raise SystemExit(scan(args.image,args.output_directory,args.trivy,args.cache_dir,args.timeout,args.export_rootfs))

if __name__=='__main__':main()

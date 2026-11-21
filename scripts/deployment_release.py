#!/usr/bin/env python3
"""Deterministic deployment archives from fixed public templates/tools only."""
import argparse
import gzip
import io
import json
from pathlib import Path
import tarfile
from release import ROOT,ReleaseError,scan_bytes,sha256

COMMON_TOOLS=('private_archive.py','sqlite_snapshot.py','database_tools.py','scan_image.py','fetch_trivy_db.py','release.py')
SMSINVIT_TOOLS=('prepare_deployment.py','migrate.py','backup.py','restore.py','configure_site.py')
DUJIAO_TOOLS=('prepare_dujiao.py',)

def files(project):
    template=ROOT/'deploy'/project
    known=('compose.yml','compose.ca.yml','Caddyfile','environment.example','runtime.example.yaml') if project=='smsinvit' else ('compose.yml','Caddyfile','environment.example')
    result=[(Path('deploy')/project/name,template/name) for name in known]
    if project=='dujiao':
        result.append((Path('deploy/dujiao/VALIDATION.md'),template/'VALIDATION.md'))
        for evidence in sorted((template/'validation').glob('*.json')):
            result.append((Path('deploy/dujiao/validation')/evidence.name,evidence))
    result.append((Path('README.md'),ROOT/'deploy/README.md' if project=='smsinvit' else template/'README.md'))
    result.extend((Path('scripts')/name,ROOT/'scripts'/name) for name in COMMON_TOOLS+(SMSINVIT_TOOLS if project=='smsinvit' else DUJIAO_TOOLS))
    lock='maintenance-requirements.lock' if project=='smsinvit' else 'tool-requirements.lock'
    result.extend((Path('deploy')/name,ROOT/'deploy'/name) for name in ('images.lock.json',lock))
    result.extend((Path(name),ROOT/name) for name in ('.gitignore','.dockerignore') if project=='smsinvit')
    if project=='smsinvit':
        # A small dependency-free package namespace plus the actual frozen DB
        # adapter lets schema/backup maintenance run without API/worker startup.
        for name in ('__init__.py','db.py'):result.append((Path('apps/api/smsinvit')/name,ROOT/'apps/api/smsinvit'/name))
        for name in ('config.py','common/__init__.py','common/mapper.py','common/mapper_sql.json','common/runtime_catalog.json'):
            result.append((Path('apps/api/smsinvit')/name,ROOT/'apps/api/smsinvit'/name))
        for path in sorted((ROOT/'migrations').rglob('V*__*.sql')):
            relative=path.relative_to(ROOT)
            if path.name.startswith(tuple('V'+str(v)+'__' for v in range(1,9))):result.append((relative,path))
        result.append((Path('deploy/database-grants.sql.template'),ROOT/'scripts/database_grants.sql.template'))
        result.append((Path('docs/CONTAINER_CVE_REVIEW.md'),ROOT/'docs/validation/CONTAINER_CVE_REVIEW.md'))
    else:
        source=ROOT/'dujiao-source-work'
        result.extend((Path(name),source/name) for name in ('.gitignore','.dockerignore','config.yml.example'))
    result.append((Path('images/image-manifest.json'),ROOT/'artifacts/releases'/('smsinvit-images' if project=='smsinvit' else 'dujiao-images')/'image-manifest.json'))
    result.append((Path('images/support-image-manifest.json'),ROOT/'artifacts/releases'/('mysql-images' if project=='smsinvit' else 'redis-images')/'image-manifest.json'))
    result.append((Path('images/caddy-image-manifest.json'),ROOT/'artifacts/releases/caddy-images/image-manifest.json'))
    return sorted(result,key=lambda item:str(item[0]))


def package(project,output,version):
    output=Path(output).resolve()
    if output.exists() or 'codex_invit' in output.parts:raise ReleaseError('Deployment archive destination must be new and outside the protected original project.')
    entries=[];records=[]
    for relative,source in files(project):
        if source.is_symlink() or not source.is_file():raise ReleaseError('Missing/unsafe public deployment input: '+str(relative))
        before=source.stat();data=source.read_bytes();after=source.stat()
        if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ReleaseError('Deployment input changed while reading.')
        if relative==Path('README.md'):
            if project=='smsinvit':
                text=data.decode().split('## 独角服务器',1)[0]
                text='\n'.join(line for line in text.splitlines() if not line.startswith('独角使用固定 Go'))
                data=('# 兑换站独立部署包\n\n本包仅包含兑换站的公开部署模板和维护工具。先使用 Python 3.12 安装 `deploy/maintenance-requirements.lock` 中的哈希锁依赖。镜像文件单独提供，核对 `images/image-manifest.json` 的 SHA-256 后导入服务器对应架构。重建镜像或重新生成源码包的命令须在单独的兑换站源码包内执行；本部署包不会启动任何业务。MySQL维护工具使用同包的原生数据库适配器，无需另一台独角服务器。\n\n'+text).encode()
            else:
                data=('# 独角独立部署包\n\n本包仅包含独角的公开模板及独立配置/SQLite/加密备份工具。先安装 `deploy/tool-requirements.lock` 的哈希锁依赖。实际镜像单独提供，导入前核对 `images/image-manifest.json` 中对应架构的 SHA-256。构建镜像、导出镜像和重新生成源码包的命令须在独角自己的源码包内执行；本包不依赖兑换站项目。\n\n'+data.decode()).encode()
        scan_bytes(relative,data)
        entries.append((relative,data));records.append({'path':str(relative),'sha256':sha256(data),'bytes':len(data),'mode':'0o644'})
    if project=='dujiao':
        # The same independent layouts used by its standalone source package.
        mapped=[]
        for relative,data in entries:
            if relative.parts[:2]==('deploy','dujiao'):relative=Path('deploy')/Path(*relative.parts[2:])
            mapped.append((relative,data))
        entries=mapped
        for item in records:
            if item['path'].startswith('deploy/dujiao/'):item['path']=item['path'].replace('deploy/dujiao/','deploy/',1)
    manifest={'format':'smsinvit-deployment-release-v1','project':project,'version':version,'files':records,'privateDataIncluded':False,'credentialsIncluded':False,'actualImagesIncluded':False,'offlineImagesReference':'images/image-manifest.json','offlineSupportImagesReference':'images/support-image-manifest.json','servicesStarted':False,'securityScan':'passed','securityScanScope':'fixed_public_allowlist_and_sensitive_data_patterns'}
    entries.append((Path('deployment-manifest.json'),(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()))
    title=('smsinvit-redeem' if project=='smsinvit' else 'dujiao-next')+'-deployment'
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as raw,gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0) as compressed,tarfile.open(fileobj=compressed,mode='w',format=tarfile.PAX_FORMAT) as archive:
        for relative,data in sorted(entries,key=lambda item:str(item[0])):
            info=tarfile.TarInfo(title+'/'+relative.as_posix());info.size=len(data);info.mode=0o644;info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0;archive.addfile(info,io.BytesIO(data))
    summary={'project':project,'version':version,'archive':output.name,'sha256':sha256(output.read_bytes()),'files':len(records),'securityScan':'passed','runtimeDataIncluded':False,'imagesIncluded':False}
    output.with_suffix(output.suffix+'.manifest.json').write_text(json.dumps(summary,sort_keys=True,indent=2)+'\n');return summary

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--project',choices=('smsinvit','dujiao'),required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--version',default='v1.0.0-python');arguments=parser.parse_args()
    try:print(json.dumps(package(arguments.project,arguments.output,arguments.version),sort_keys=True))
    except ReleaseError as error:parser.exit(1,str(error)+'\n')

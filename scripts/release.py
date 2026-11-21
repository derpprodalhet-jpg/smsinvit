#!/usr/bin/env python3
"""Deterministic source archives from explicit allowlists; no runtime data is read."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DENIED_DIRS = {".git", ".github", ".idea", ".vscode", ".venv", ".venv.freeze", "node_modules", "__pycache__", ".pytest_cache", ".turbo", "dist", "build", "runtime", "logs", "private", "secrets", "backups", "artifacts", "uploads", "codex-homes", ".docker", ".codex"}
DENIED_NAMES = {"auth.json", "credentials.json", "config.yml", "config.yaml", "config.json", ".DS_Store", ".npmrc", ".pypirc", ".netrc", ".my.cnf", ".mylogin.cnf", "id_rsa", "id_ed25519"}
DENIED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".pem", ".key", ".p12", ".pfx", ".crt", ".dump", ".bak", ".tgz", ".gz", ".zip", ".tar", ".csv", ".xlsx"}
SOURCE_SUFFIXES = {".py", ".pyi", ".json", ".toml", ".tsx", ".ts", ".css", ".mjs", ".cjs", ".js", ".yaml", ".yml", ".sql", ".md", ".txt", ".vue", ".html", ".svg", ".ico", ".png", ".webp", ".sh", ".template", ".lock", ".example", ".map", ".patch"}
SMSINVIT_ROOT = {"package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "turbo.json", "eslint.config.mjs", "ruff.toml", ".prettierrc", ".dockerignore", ".gitignore", "THIRD_PARTY_NOTICES.md", "README.md"}
SMSINVIT_TREES = ("apps/api", "apps/web", "apps/admin", "packages/errors", "packages/tsconfig", "vendor/codex_invit", "migrations", "scripts", "deploy", "docs", "patches")
SMSINVIT_RETIRED_UI_TREES = tuple(Path('vendor/codex_invit/webui')/name for name in ('frontend','frontend_user','static','static_user'))
DUJIAO_ROOT = {"Dockerfile", ".dockerignore", ".gitignore", "go.mod", "go.sum", "config.yml.example", "README.md", "README_EN.md", "LICENSE", "THIRD_PARTY_NOTICES.md"}
DUJIAO_TREES = ("cmd", "internal", "frontend", "migrations", "pkg")
DUJIAO_TOOLS = ('build_images.py','build_support_images.py','release.py','pull_verified.py','scan_image.py','fetch_trivy_db.py','export_images.py','prepare_dujiao.py','sqlite_snapshot.py','database_tools.py','private_archive.py')
# No path exceptions remain: the retired admin runtime page and the retired Java parity
# launcher were the only entries, and both files are gone with their modules. Do not grant
# a generic exception to runtime data trees or arbitrary files under that path.
SOURCE_NAME_EXCEPTIONS = set()
# Preserve the original optional Telegram guides and their public source inputs.
# These exact names do not grant access to other docs, private .env files,
# generated binaries, or runtime deployment/data trees.
DUJIAO_PUBLIC_EXCEPTIONS = frozenset({
    'docs/telegram-bot.md', 'docs/telegram-deployment.md',
    'docs/telegram-operations.md', 'docs/telegram-acceptance.md',
    '.env.telegram-bot.example', '.env.telegram-stack.example',
    'compose.telegram-bot.yml', 'compose.telegram-stack.yml',
    'Dockerfile.telegram-bot',
    'scripts/telegram/backup_restore.py', 'scripts/telegram/build-package.sh',
    'scripts/telegram/diagnose.py', 'scripts/telegram/smoke_package.py',
    'scripts/telegram/tests/test_maintenance.py',
    'deploy/telegram/config.yml.example', 'deploy/telegram/run-bot.sh',
    'deploy/telegram/nginx/templates/telegram.conf.template',
    'deploy/telegram/systemd/dujiao-telegram-bot.service.example',
    'deploy/telegram/macos/com.dujiao.telegram-bot.plist.example',
})
SECRET_RULES = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/=\r\n]{64,}-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("openai_secret_key", re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b")),
    ("embedded_token", re.compile(rb'''["'](?:access_token|refresh_token|id_token|session_token)["']\s*:\s*["'](?:eyJ[A-Za-z0-9_.-]{32,}|v1\.[A-Za-z0-9_-]{32,})["']''')),
)

class ReleaseError(Exception): pass

def sha256(data): return hashlib.sha256(data).hexdigest()

def denied(path):
    if path.as_posix() in SOURCE_NAME_EXCEPTIONS | DUJIAO_PUBLIC_EXCEPTIONS: return False
    return any(part in DENIED_DIRS for part in path.parts) or path.name in DENIED_NAMES or path.name.startswith('.env') or path.name.endswith(('-wal', '-shm', '-journal')) or path.suffix.lower() in DENIED_SUFFIXES

def allowed(path, project='smsinvit'):
    if project=='smsinvit' and any(path==tree or tree in path.parents for tree in SMSINVIT_RETIRED_UI_TREES):return False
    if project=='smsinvit' and path.as_posix() in SOURCE_NAME_EXCEPTIONS: return True
    if project=='dujiao' and path.as_posix() in DUJIAO_PUBLIC_EXCEPTIONS: return True
    if denied(path): return False
    root_files,trees=(SMSINVIT_ROOT,SMSINVIT_TREES) if project=='smsinvit' else (DUJIAO_ROOT,DUJIAO_TREES)
    if path.as_posix() in root_files: return True
    if not any(path==Path(tree) or Path(tree) in path.parents for tree in trees): return False
    return path.name in {'Dockerfile', 'Caddyfile', '.dockerignore', 'Makefile', 'LICENSE'} or path.suffix.lower() in SOURCE_SUFFIXES or project=='dujiao' and path.suffix=='.go'

def scan_bytes(path,data):
    violations=[{'path':str(path),'rule':name} for name,pattern in SECRET_RULES if pattern.search(data)]
    if violations: raise ReleaseError('Potential embedded credentials: '+json.dumps(violations,separators=(',',':')))

def selected_files(source,project='smsinvit'):
    # Never descend into forbidden trees, including private credentials and data.
    source=Path(source).resolve(); found=[]
    trees=tuple(Path(tree) for tree in (SMSINVIT_TREES if project=='smsinvit' else DUJIAO_TREES))
    exceptions=SOURCE_NAME_EXCEPTIONS if project=='smsinvit' else DUJIAO_PUBLIC_EXCEPTIONS
    for directory,dirs,files in os.walk(source,followlinks=False):
        def source_directory(name):
            relative=(Path(directory)/name).relative_to(source)
            if project=='smsinvit' and any(relative==tree or tree in relative.parents for tree in SMSINVIT_RETIRED_UI_TREES):return False
            exact_source_parent=any(relative in Path(file).parents for file in exceptions)
            return (name not in DENIED_DIRS or exact_source_parent) and not name.startswith('.env') and (exact_source_parent or any(relative==tree or relative in tree.parents or tree in relative.parents for tree in trees))
        dirs[:]=sorted(name for name in dirs if source_directory(name))
        for name in sorted(files):
            candidate=Path(directory)/name;relative=candidate.relative_to(source)
            if not allowed(relative,project): continue
            if candidate.is_symlink(): raise ReleaseError(f'Symlink is not permitted in release source: {relative}')
            found.append((relative,candidate))
    return sorted(found,key=lambda item:item[0].as_posix())

def stage(source,destination,project='smsinvit',version='v1.0.0-python'):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if destination.exists(): raise ReleaseError('Staging directory must be new.')
    destination.mkdir(parents=True,mode=0o700);records=[]
    try:
        for relative,candidate in selected_files(source,project):
            before=candidate.stat();data=candidate.read_bytes();after=candidate.stat()
            if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns): raise ReleaseError(f'Source changed while packaging: {relative}')
            scan_bytes(relative,data)
            target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            mode=0o755 if before.st_mode&0o111 else 0o644;target.chmod(mode)
            records.append({'path':relative.as_posix(),'sha256':sha256(data),'bytes':len(data),'mode':oct(mode)})
        if not records: raise ReleaseError('No allowlisted project source found.')
        manifest={'format':'smsinvit-source-release-v1','project':project,'version':version,'files':records,'securityScan':{'status':'passed','rules':[name for name,_ in SECRET_RULES],'runtimeDataIncluded':False}}
        (destination/'release-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
        return manifest
    except BaseException:
        shutil.rmtree(destination);raise

def archive(source,output,project='smsinvit',version='v1.0.0-python',deployment_source=None):
    output=Path(output).resolve()
    if output.exists(): raise ReleaseError('Release archive already exists; no overwrite is performed.')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='smsinvit-release-') as temporary:
        directory=Path(temporary)/'source';manifest=stage(source,directory,project,version)
        if project=='dujiao' and deployment_source:
            candidates=[]
            deployment_source=Path(deployment_source)
            for folder,dirs,names in os.walk(deployment_source,followlinks=False):
                dirs[:]=sorted(name for name in dirs if name not in DENIED_DIRS and not name.startswith('.env'))
                for name in sorted(names):
                    candidate=Path(folder)/name;relative=candidate.relative_to(deployment_source)
                    if denied(relative) or candidate.is_symlink() or not (candidate.name=='Caddyfile' or candidate.suffix in SOURCE_SUFFIXES):continue
                    candidates.append((Path('deploy')/relative,candidate))
            candidates.extend((Path('scripts')/name,ROOT/'scripts'/name) for name in DUJIAO_TOOLS)
            candidates.extend((Path('deploy')/name,ROOT/'deploy'/name) for name in ('images.lock.json','tool-requirements.lock'))
            candidates.append((Path('deploy/support/redis.Dockerfile'),ROOT/'deploy/support/redis.Dockerfile'))
            candidates.append((Path('deploy/support/caddy.Dockerfile'),ROOT/'deploy/support/caddy.Dockerfile'))
            candidates.append((Path('deploy/support/packages.lock.json'),ROOT/'deploy/support/packages.lock.json'))
            for relative,candidate in sorted(candidates):
                if candidate.is_symlink():raise ReleaseError('Deployment tooling source cannot be a symlink.')
                before=candidate.stat()
                data=candidate.read_bytes();scan_bytes(relative,data)
                after=candidate.stat()
                if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ReleaseError('Deployment source changed while packaging.')
                target=directory/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);target.chmod(0o644)
                manifest['files'].append({'path':target.relative_to(directory).as_posix(),'sha256':sha256(data),'bytes':len(data),'mode':'0o644'})
            manifest['files'].sort(key=lambda entry:entry['path'])
            (directory/'release-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
        name='smsinvit-redeem' if project=='smsinvit' else 'dujiao-next'
        with output.open('xb') as raw,gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0) as compressed,tarfile.open(fileobj=compressed,mode='w',format=tarfile.PAX_FORMAT) as tar:
            for candidate in sorted(directory.rglob('*')):
                if not candidate.is_file():continue
                data=candidate.read_bytes();info=tarfile.TarInfo(name+'/'+candidate.relative_to(directory).as_posix());info.size=len(data);info.mode=candidate.stat().st_mode&0o777;info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0;tar.addfile(info,io.BytesIO(data))
    result={'project':project,'version':version,'archive':output.name,'sha256':sha256(output.read_bytes()),'files':len(manifest['files']),'securityScan':'passed'}
    output.with_suffix(output.suffix+'.manifest.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path,default=ROOT);parser.add_argument('--project',choices=['smsinvit','dujiao'],default='smsinvit');parser.add_argument('--version',default='v1.0.0-python');parser.add_argument('--output',type=Path);parser.add_argument('--check',action='store_true');parser.add_argument('--deployment-source',type=Path)
    args=parser.parse_args()
    try:
        if not re.fullmatch('[A-Za-z0-9_.-]{1,64}',args.version):raise ReleaseError('Invalid release version.')
        if args.check:
            files=selected_files(args.source,args.project)
            for relative,path in files:scan_bytes(relative,path.read_bytes())
            print(json.dumps({'status':'passed','project':args.project,'files':len(files),'runtimeDataIncluded':False}));return
        if not args.output:raise ReleaseError('--output is required for archive generation.')
        print(json.dumps(archive(args.source,args.output,args.project,args.version,args.deployment_source),ensure_ascii=False))
    except ReleaseError as error:parser.exit(1,str(error)+'\n')

if __name__=='__main__':main()

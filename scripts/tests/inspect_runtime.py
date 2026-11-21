"""Inspect disposable images without service startup or network requests."""
import json
from pathlib import Path
import subprocess
import sys

def run(argv):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=10,check=False)
    return result.returncode,result.stdout.strip()

targets=[Path(sys.executable),Path('/opt/smsinvit-bin/node')]
for root in (Path('/usr/local/lib/python3.12'),Path('/opt/codex')):
    for file in root.rglob('*'):
        if not file.is_file() or file.is_symlink():continue
        try:
            with file.open('rb') as source:is_elf=source.read(4)==b'\x7fELF'
            if is_elf:targets.append(file)
        except OSError:continue
dependencies={}
for target in sorted(set(targets)):
    if target.exists():
        code,output=run(['/usr/bin/ldd',str(target)])
        dependencies[str(target)]={'exitCode':code,'ldd':output}
packages={}
for name in ('bsdutils','util-linux','mount','login','passwd','libsystemd0','libudev1','libacl1','libncursesw6','libtinfo6','ncurses-base','ncurses-bin','perl-base','libuuid1','libmount1','libblkid1','libsmartcols1','liblastlog2-2'):
    code,output=run(['/usr/bin/dpkg-query','-W','-f=${binary:Package}\t${Status}\t${Version}\t${Essential}',name])
    packages[name]={'exitCode':code,'package':output}
programs={name:bool(Path(name).exists()) for name in ('/usr/bin/infocmp','/usr/bin/perl','/usr/bin/nsenter','/usr/bin/mount','/usr/bin/umount','/usr/lib/systemd/systemd-homed','/usr/bin/su','/usr/bin/passwd')}
print(json.dumps({'architecture':__import__('platform').machine(),'nativeDependencies':dependencies,'packages':packages,'programs':programs,'upstreamBusinessRequests':0},sort_keys=True))

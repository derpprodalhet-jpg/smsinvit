#!/usr/bin/env python3
"""Copy retained storefront config into a new private deployment directory.

Configuration bytes and all existing keys are retained unchanged. This never
opens databases or starts either project. Docker's runtime is UID/GID 10001.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

def prepare(directory,config):
    directory=Path(directory).resolve();config=Path(config)
    if 'codex_invit' in directory.parts or 'codex_invit' in config.resolve().parts:raise ValueError('The protected original invitation project is not a storefront deployment path.')
    if directory.exists():raise ValueError('Private deployment directory must be new; no existing configuration is replaced.')
    if config.is_symlink() or not config.is_file():raise ValueError('Retained config must be an explicit regular file.')
    before=config.stat()
    if stat.S_IMODE(before.st_mode)&0o077 or before.st_size>4*1024*1024:raise ValueError('Retained config must be owner-only and at most 4 MiB.')
    data=config.read_bytes();after=config.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('Retained config changed during copying.')
    directory.mkdir(mode=0o700,parents=True)
    target=directory/'config.yml';fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as output:output.write(data)
    if os.geteuid()==0:os.chown(target,10001,10001)
    result={'status':'prepared','directory':str(directory),'runtimeUid':10001,'runtimeGid':10001,'retainedConfigUnchanged':True,'configSha256':hashlib.sha256(data).hexdigest(),'ownershipPrepared':os.geteuid()==0,'servicesStarted':False,'databasesOpened':False}
    fd=os.open(directory/'preparation.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as output:output.write(json.dumps(result,sort_keys=True,indent=2)+'\n')
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--config-file',type=Path,required=True);args=parser.parse_args()
    try:print(json.dumps(prepare(args.directory,args.config_file),sort_keys=True))
    except (ValueError,OSError) as error:parser.exit(1,str(error)+'\n')

if __name__=='__main__':main()

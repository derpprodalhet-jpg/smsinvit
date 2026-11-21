#!/usr/bin/env python3
"""Explicitly update only the public API base and CORS origins after a server migration."""
import argparse
import json
from urllib.parse import urlsplit
from uuid import uuid4
from database_tools import MaintenanceError,arguments,credentials,maintenance_lock

def origins(web_origin,admin_origin=None):
    result=[]
    for raw in [web_origin,admin_origin]:
        if not raw:continue
        parsed=urlsplit(raw)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path not in ('','/') or parsed.query or parsed.fragment or '*' in raw or any(char in raw for char in '\r\n\0'):raise MaintenanceError('Production origins must be explicit HTTPS host origins without credentials, paths, queries or fragments.')
        value=raw.rstrip('/')
        if value not in result:result.append(value)
    return result

def configure(selected,web_origin,admin_origin=None,apply=False):
    allowed=origins(web_origin,admin_origin)
    # runtime.web_origin 已从运行时目录删除（没有任何读取方），这里不再写入。
    values={'runtime.public_api_base':allowed[0],'runtime.cors_origins':','.join(allowed)}
    if not apply:return {'status':'preview','values':values,'businessSettingsChanged':False,'requiresExplicitApply':True}
    from smsinvit.common.mapper import Mapper
    from smsinvit.config import RUNTIME_BY_KEY,validate_runtime
    db=selected.connect()
    try:
        with maintenance_lock(db,selected.database),db.transaction():
            mapper=Mapper(db,'SettingsMapper')
            audit=Mapper(db,'AuditLogMapper')
            for key,value in values.items():
                validate_runtime(key,value)
                if mapper.existsSecret(key):raise MaintenanceError('Site origin metadata unexpectedly contains a secret row; no changes were committed.')
                mapper.upsertRuntime(str(uuid4()),key,RUNTIME_BY_KEY[key]['category'],json.dumps({'value':value}),None)
                # This explicit offline CLI uses the same frozen audit mapper,
                # without importing API routes/authentication or starting workers.
                audit.insert(str(uuid4()),None,'deployment.site_origin_update','setting',key,json.dumps({'key':key,'origin':value,'source':'explicit_maintenance_cli'},ensure_ascii=False,separators=(',',':')),str(uuid4()))
        return {'status':'applied','values':values,'businessSettingsChanged':False,'restartApiRequired':True}
    finally:db.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__);arguments(parser);parser.add_argument('--web-origin',required=True);parser.add_argument('--admin-origin');parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    try:print(json.dumps(configure(credentials(args),args.web_origin,args.admin_origin,args.apply)))
    except MaintenanceError as error:parser.exit(1,str(error)+'\n')
    except Exception:parser.exit(1,'Site metadata update failed. No private driver information is printed.\n')

if __name__=='__main__':main()

#!/usr/bin/env python3
"""Check retained private keys against allowlisted source, without disclosing keys."""
import argparse
import hashlib
import json
from pathlib import Path
from release import selected_files

def verify(source,files,project='smsinvit'):
    values=set()
    for file in files:
        data=json.loads(Path(file).read_text())
        for name,value in data.items():
            if not isinstance(value,str) or len(value)<12:raise ValueError('Expected retained private keys with sufficient length')
            values.add(value.encode())
    if not values:raise ValueError('At least one private-key file is required')
    checked=selected_files(source,project);matches=[]
    for path,file in checked:
        content=file.read_bytes()
        if any(value in content for value in values):matches.append(str(path))
    return {'format':'smsinvit-private-literal-scan-v1','status':'FAIL' if matches else 'PASS','project':project,'sourceFiles':len(checked),'retainedSecretValues':len(values),'matchingPaths':matches,'secretValuesPublished':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--keys-file',type=Path,action='append',required=True);parser.add_argument('--project',choices=['smsinvit','dujiao'],default='smsinvit');parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();result=verify(args.source,args.keys_file,args.project)
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({key:value for key,value in result.items() if key!='matchingPaths'}));return 1 if result['status']!='PASS' else 0
if __name__=='__main__':raise SystemExit(main())

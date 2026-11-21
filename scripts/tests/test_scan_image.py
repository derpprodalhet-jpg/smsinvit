import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scan_image

@pytest.mark.parametrize('mode,expected',[('complete','passed'),('finding','findings'),('incomplete','unavailable'),('timeout','unavailable'),('changed','unavailable')])
def test_scanner_cannot_claim_clean_without_completed_inventory(tmp_path,monkeypatch,mode,expected):
    inspected=[]
    def descriptor(image):
        inspected.append(image)
        return {'imageId':'sha256:changed' if mode=='changed' and len(inspected)>1 else 'sha256:tested','architecture':'arm64'}
    def run(argv,**kwargs):
        if mode=='timeout':raise subprocess.TimeoutExpired(argv,1)
        report=Path(argv[argv.index('--output')+1])
        report.write_text(json.dumps({} if mode=='incomplete' else {'runs':[{'results':[{'ruleId':'actual-cve'}] if mode=='finding' else []}]}))
        return SimpleNamespace(returncode=2 if mode=='finding' else 0)
    monkeypatch.setattr(scan_image,'descriptor',descriptor);monkeypatch.setattr(scan_image.subprocess,'run',run)
    result=scan_image.scan('fixture:local',tmp_path)
    status=json.loads(next(tmp_path.glob('*.status.json')).read_text())
    assert status['status']==expected and (result==0)==(expected=='passed')
    assert status['imageId']=='sha256:tested'

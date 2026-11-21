import gzip
import httpx
import pytest
from smsinvit.config import Config
from smsinvit.errors import Error
from smsinvit.security import SupplierClientGuard

class Policy:
    def require_allowed(self,url,**kwargs):return ('203.0.113.20',)
    def denied(self):return Error('DENIED','denied',403)

def guarded(monkeypatch,body,headers):
    config=Config(environ={});guard=SupplierClientGuard(config,Policy());seen=[];real=httpx.Client
    def handler(request):seen.append(request);return httpx.Response(200,headers=headers,stream=httpx.ByteStream(body))
    monkeypatch.setattr(httpx,'Client',lambda **kw:real(**kw,transport=httpx.MockTransport(handler)))
    return guard,seen

def test_pinned_supplier_response_preserves_gzip_json_without_double_decoding(monkeypatch):
    guard,seen=guarded(monkeypatch,gzip.compress(b'{"ok":true}'),{'content-encoding':'gzip','content-type':'application/json'})
    response=guard.get('https://supplier.example/api',headers={'Authorization':'Bearer private'})
    assert response.json()=={'ok':True}
    request=seen[0];assert request.url.host=='203.0.113.20' and request.headers['Host']=='supplier.example' and request.extensions['sni_hostname']=='supplier.example'
    assert request.headers['Accept-Encoding']=='identity'

def test_supplier_gzip_bomb_bounded_before_response_returns(monkeypatch):
    guard,seen=guarded(monkeypatch,gzip.compress(b'x'*(4*1024*1024+1)),{'content-encoding':'gzip'})
    with pytest.raises(Error) as error:guard.get('https://supplier.example/api')
    assert error.value.code=='SUPPLIER_RESPONSE_TOO_LARGE' and len(seen)==1

def test_supplier_raw_body_cap_and_tls_downgrade_fail_closed(monkeypatch):
    guard,seen=guarded(monkeypatch,b'x'*(4*1024*1024+1),{})
    with pytest.raises(Error) as error:guard.get('https://supplier.example/api')
    assert error.value.code=='SUPPLIER_RESPONSE_TOO_LARGE'
    before=len(seen)
    with pytest.raises(Error):guard.get('https://supplier.example/api',verify=False)
    assert len(seen)==before

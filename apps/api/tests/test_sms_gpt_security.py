"""SMS router security boundaries; forbidden calls cannot reach business methods."""
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from smsinvit.errors import Error,error_body,response
from smsinvit.security import TokenService,PermissionService,Principal
from smsinvit.sms.routes import router as sms_router
from smsinvit.sms.support import BusinessSettings

class Settings:
    def get(self,k,d=None):return {'webhookEnabled':True,'webhookToken':'valid-token','webhookAllowedIps':'203.0.113.7'}.get(k,d)
    def secret(self,k):return 'signed-webhook-secret'

@pytest.fixture
def api():
    app=FastAPI();sms=Mock();sms.settings=Settings()
    db=SimpleNamespace(all=lambda *args:[])
    services=SimpleNamespace(sms=sms,tokens=TokenService('x'*64),permissions=PermissionService(db),audit=Mock(),limiter=SimpleNamespace(check=lambda *args:None));app.state.services=services
    @app.exception_handler(Error)
    async def expected(request,error):return response(error_body(error),error.status)
    @app.middleware('http')
    async def auth(request,call_next):
        request.state.client_ip='203.0.113.7';request.state.principal=None
        if request.headers.get('x-test-low-permission'):request.state.principal=Principal(str(uuid4()),str(uuid4()),('VIEWER',))
        return await call_next(request)
    app.include_router(sms_router)
    return TestClient(app),services

@pytest.mark.parametrize('path,method',[
 ('','get'),('/fetch-code','post'),('/retry','post'),('/next-sms','post')])
def test_missing_and_other_session_capability_rejected_without_business_calls(api,path,method):
    client,s=api;sid=str(uuid4());other=str(uuid4());scope='sms-session'
    url=f'/api/v1/sms/sessions/{sid}{path}'
    r=getattr(client,method)(url);assert r.status_code==401;r=getattr(client,method)(url,headers={'X-Session-Secret':s.tokens.issue(scope,other)});assert r.status_code==401
    assert not s.sms.method_calls

@pytest.mark.parametrize('path',[
 '/api/v1/admin/sms/overview','/api/v1/admin/sms/settings','/api/v1/admin/sms/catalog/countries'])
def test_admin_anonymous_and_low_permission_rejected_without_business_calls(api,path):
    client,s=api;assert client.get(path).status_code==401;assert client.get(path,headers={'X-Test-Low-Permission':'true'}).status_code==403;assert not s.sms.method_calls

def test_sms_wrong_token_or_missing_activation_rejected_before_business(api):
    c,s=api;assert c.post('/api/v1/sms/webhook/forged-token',json={'activationId':'a','code':'1'}).status_code==403;assert not s.sms.method_calls

def test_secret_blank_mask_and_clear_never_persist_plaintext():
    class DB:
        def __init__(self):self.rows={}
        def one(self,sql,*args):
            if args and args[0] in self.rows:return {'valueJson':json.dumps(self.rows[args[0]])}
            return None
        def execute(self,sql,*args):self.rows[args[1]]=json.loads(args[3])
    codec=SimpleNamespace(encrypt=lambda v:'encrypted:'+v,decrypt=lambda v:v.removeprefix('encrypted:'));db=DB();root=__import__('pathlib').Path(__file__).parents[1]/'smsinvit'
    sms=BusinessSettings(db,codec,{},'sms.smsbower',root/'sms/defaults.json')
    sms.save_body({'apiKey':'sms-key','webhookSecret':'whsec'})
    sms.save_body({'apiKey':''});assert sms.secret()=='sms-key'
    sms.save_body({'apiKey':'sm****ey'});assert sms.secret()=='sms-key'
    sms.save_body({'clearApiKey':True});assert not sms.secret();assert sms.secret('webhookSecret')=='whsec'
    assert all('apiKey' not in row and 'webhookSecret' not in row for row in db.rows.values())

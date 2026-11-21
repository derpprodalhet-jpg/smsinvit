"""Supplier protocol tests use httpx MockTransport exclusively; no live requests."""
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import httpx,pytest
from smsinvit.errors import Error
from smsinvit.sms.client import SmsbowerClient,NumberOrder,service_map,cost_usd_to_cny_minor
from smsinvit.sms.service import normalize_exception,exception_prefix,merge_exceptions,excluded,parse_codes,mask_phone
from smsinvit.sms.support import Mapper

class Settings:
    def __init__(self,**values):self.values={'baseUrl':'https://smsbower.page/stubs/handler_api.php','unavailableMessage':'maintenance','requestTimeoutMs':20000,**values}
    def get(self,k,default=None):return self.values.get(k,default)
    def load(self):return self.values
    def secret(self,k='apiKey'):return self.values.get(k,'test-key')

def sms_client(raw,status=200):
    requests=[]
    def respond(request):requests.append(request);return httpx.Response(status,text=raw)
    return SmsbowerClient(Settings(),httpx.Client(transport=httpx.MockTransport(respond))),requests

@pytest.mark.parametrize('raw,aid,phone,cost',[
 ('{"activationId":123,"phoneNumber":"+12025550123","activationCost":"0.12"}','123','+12025550123',Decimal('0.12')),
 ('{"activationId":"a","phoneNumber":"12025550123","activationCost":"invalid"}','a','12025550123',None),
 ('{"activationId":"a","activationCost":"-1"}','a','',None),
 ('ACCESS_NUMBER:123:12025550123','123','12025550123',None)])
def test_sms_number_preserves_activation_even_bad_optional_cost(raw,aid,phone,cost):
    client,requests=sms_client(raw);assert client.get_number('ot',0,Decimal('0.01'),Decimal('0.3'),'1,2','1202555')==NumberOrder(aid,phone,cost)
    req=requests[0];assert req.method=='GET';assert req.url.params['api_key']=='test-key';assert req.url.params['providerIds']=='1,2';assert req.url.params['phoneException']=='1202555'

@pytest.mark.parametrize('raw,expected',[('NO_NUMBERS','SMS_UNAVAILABLE'),('NO_BALANCE','SMS_UNAVAILABLE'),('WRONG_EXCEPTION_PHONE','SMS_WRONG_EXCEPTION_PHONE'),('TIMEOUT','SMS_PROVIDER_ERROR'),('{"phoneNumber":"123"}','SMS_PROVIDER_ERROR')])
def test_sms_rejections_distinguish_paid_ambiguity(raw,expected):
    client,_=sms_client(raw)
    with pytest.raises(Error) as error:client.get_number('ot',0,0,1,None,None)
    assert error.value.code==expected

@pytest.mark.parametrize('raw,kind,code',[("STATUS_OK:'123456'",'OK','123456'),('STATUS_WAIT_RETRY:123456','WAIT_RETRY','123456'),('STATUS_WAIT_CODE','WAIT',None),('STATUS_CANCEL','CANCEL',None),('NO_ACTIVATION','ERROR',None),('{"status":"OK","smsCode":"654321"}','OK','654321')])
def test_sms_status_protocol(raw,kind,code):
    client,_=sms_client(raw);result=client.get_status('a');assert (result.kind,result.code)==(kind,code)

def test_phone_exceptions_provider_price_shapes_and_exact_rounding():
    assert exception_prefix('+12025550123')=='1202555';assert exception_prefix('123') is None
    assert normalize_exception('1202，1202 123 +4400')=='1202,4400'
    assert merge_exceptions('1202',['12025550123','+12025559999'])=='1202,1202555'
    assert excluded('+12025550123','1202555');assert not excluded('44205550123','1202555')
    for raw in ({'0':{'ot':{'1':{'count':20,'price':'0.1'}}}},{'ot':{'1':{'count':20,'price':'0.1'}}},{'ot':{'0':{'1':{'count':20,'price':'0.1'}}}}):assert service_map(raw,'ot',0)=={'1':{'count':20,'price':'0.1'}}
    assert cost_usd_to_cny_minor('0.001','7.13')==1;assert cost_usd_to_cny_minor('0.1','7.13')==72
    assert parse_codes({'smsCodes':'["1","2"]'})==['1','2'];assert parse_codes({'smsCode':'3'})==['3']

class CaptureDB:
    def __init__(self):self.calls=[]
    def one(self,sql,*args):self.calls.append((sql,args));return {'value':1}
    def all(self,sql,*args):self.calls.append((sql,args));return []
    def execute(self,sql,*args):self.calls.append((sql,args));return SimpleNamespace(rowcount=1)

def test_every_preserved_mapper_operation_binds_all_values():
    db=CaptureDB();mapper=Mapper(db,Path(__file__).parents[1]/'smsinvit'/'sms'/'queries.json')
    for name,query in mapper.queries.items():
        values=['x' for _ in query['params']];getattr(mapper,name)(*values);sql,args=db.calls[-1]
        assert '#{' not in sql and '<script>' not in sql and '<if' not in sql,name
        assert sql.count('?')==len(args),name
        if name in ('markSessionSuccess','markSessionWaitingCode','startAcquisition'):assert 'WHERE' in sql,name

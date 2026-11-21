from types import SimpleNamespace
import httpx,pytest
from smsinvit.sms.line_type import FreeLineChecker
from smsinvit.errors import Error
from test_sms_gpt_alignment import sms

PHONE='+18325550123'
def checker(respond,keys=None):
    keys=keys if keys is not None else {'numlookupApiKey':'num-key','abstractApiKey':'abs-key'}
    calls=[]
    def transport(request):calls.append(request);return respond(request)
    return FreeLineChecker(SimpleNamespace(secret=lambda k:keys.get(k)),httpx.Client(transport=httpx.MockTransport(transport))),calls

def response(kind='mobile',**extra):return httpx.Response(200,json={'valid':True,'international_format':PHONE,'line_type':kind,**extra})

def test_primary_mobile_never_calls_secondary():
    c,calls=checker(lambda r:response())
    assert c.check(PHONE).line_type=='mobile' and len(calls)==1
    assert calls[0].headers['apikey']=='num-key' and 'num-key' not in str(calls[0].url)

@pytest.mark.parametrize('kind',['landline','unknown','special_services','toll_free'])
def test_non_mobile_never_falls_back_to_get_more_favorable_result(kind):
    c,calls=checker(lambda r:response(kind));assert c.check(PHONE).line_type==kind.replace('_','') and len(calls)==1

def test_voip_number_type_overrides_mobile():
    c,_=checker(lambda r:response('mobile',number_type='voip'));assert c.check(PHONE).line_type=='voip'

def test_monthly_exhaustion_switches_and_caches_primary():
    def respond(r):
        if r.url.host=='phonevalidation.abstractapi.com':return response()
        if r.url.path.endswith('/status'):return httpx.Response(200,json={'quotas':{'month':{'remaining':0}}})
        return httpx.Response(429,json={'message':'quota'})
    c,calls=checker(respond)
    result=c.check(PHONE);assert result.provider=='abstractapi' and result.reasons=={'numlookupapi':'quota_exhausted'}
    assert len(calls)==3
    assert c.check(PHONE).provider=='abstractapi' and len(calls)==4


def test_both_exhausted_skip_without_throwing():
    def respond(r):
        if r.url.path.endswith('/status'):return httpx.Response(200,json={'quotas':{'month':{'remaining':0}}})
        return httpx.Response(429,json={'error':{'message':'Monthly limit exceeded'}})
    c,calls=checker(respond);result=c.check(PHONE)
    assert result.skipped and set(result.reasons.values())=={'quota_exhausted'}
    assert c.check(PHONE).skipped and len(calls)==3


def test_invalid_credentials_are_distinct_from_quota_and_fall_back():
    c,_=checker(lambda r:httpx.Response(401,json={}) if r.url.host=='api.numlookupapi.com' else response())
    result=c.check(PHONE);assert result.provider=='abstractapi' and result.reasons['numlookupapi']=='invalid_credentials'


def test_no_credentials_no_network():
    c,calls=checker(lambda r:pytest.fail('network'),{})
    assert c.check(PHONE).skipped and not calls


def test_timeout_or_wrong_number_never_verified():
    c,_=checker(lambda r:response(international_format='+12025550123'))
    assert c.check(PHONE).skipped
    def timeout(r):raise httpx.ReadTimeout('synthetic')
    c,_=checker(timeout);assert c.check(PHONE).skipped


def test_abstract_legacy_schema():
    c,_=checker(lambda r:httpx.Response(200,json={'valid':True,'phone':PHONE[1:],'type':'voip'}),{'abstractApiKey':'abs-key'})
    assert c.check(PHONE).line_type=='voip'


def test_exhausted_services_do_not_block_acquisition():
    s,p=sms();s.line_checker,_=checker(lambda r:httpx.Response(429,json={'error':{'message':'quota exceeded'}}),{'abstractApiKey':'abs-key'})
    assert s.redeem('valid-code')['status']=='WAITING_CODE' and p.calls==1


def test_confirmed_voip_cancels_and_learns_entire_area():
    s,p=sms();p.phone=PHONE[1:];s.line_checker,_=checker(lambda r:response('landline',number_type='voip'))
    with pytest.raises(Error) as e:s.redeem('valid-code')
    assert e.value.code=='SMS_LINE_NOT_MOBILE' and s.m.listAutoExcludedAreas()==['1832']
    assert s.m.key['status']=='UNUSED' and p.set_status_calls==[('activation-1',8)]


def test_new_classifier_secrets_are_encrypted_and_blank_preserves():
    import json
    from pathlib import Path
    from smsinvit.sms.support import BusinessSettings
    class DB:
        row=None
        def one(self,*args):return {'valueJson':json.dumps(self.row)} if self.row else None
        def execute(self,sql,*args):self.row=json.loads(args[3])
    db=DB();codec=SimpleNamespace(encrypt=lambda s:'encrypted:'+s,decrypt=lambda s:s.removeprefix('encrypted:'))
    settings=BusinessSettings(db,codec,{},'sms.smsbower',Path(__file__).parents[1]/'smsinvit/sms/defaults.json')
    settings.save_body({'numlookupApiKey':'num-secret','abstractApiKey':'abstract-secret'})
    assert 'numlookupApiKey' not in db.row and 'abstractApiKey' not in db.row
    assert db.row['numlookupApiKeyEncrypted']=='encrypted:num-secret'
    settings.save_body({'numlookupApiKey':'','abstractApiKey':''})
    assert settings.secret('abstractApiKey')=='abstract-secret'
    settings.save_body({'clearAbstractApiKey':True})
    assert not settings.secret('abstractApiKey') and settings.secret('numlookupApiKey')=='num-secret'


def test_month_quota_headers_are_observed_without_extra_requests():
    observations=[]
    monitor=SimpleNamespace(observe=lambda provider,key,values:observations.append((provider,values)))
    c,calls=checker(lambda r:httpx.Response(200,json={'valid':True,'international_format':PHONE,'line_type':'mobile'},headers={
        'X-RateLimit-Remaining-Quota-Month':'75','X-RateLimit-Limit-Quota-Month':'100',
        'X-RateLimit-Remaining-Quota-Minute':'3'}))
    c.monitor=monitor
    assert c.check(PHONE).line_type=='mobile' and len(calls)==1
    assert observations[0][1]['remaining']==75 and observations[0][1]['total']==100
    assert observations[-1][1]['state']=='available'
    assert 'phone' not in str(observations)


def test_monitor_failure_never_blocks_classifier():
    def failed(*args):raise RuntimeError('monitor offline')
    c,_=checker(lambda r:response());c.monitor=SimpleNamespace(observe=failed)
    assert c.check(PHONE).line_type=='mobile'

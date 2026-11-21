"""Explicit number changes preserve exclusions and do not stall behind read cadence."""
from smsinvit.sms.client import StatusResult
from smsinvit.errors import Error
from smsinvit.sms.service import exception_prefix, merge_exceptions,voip_area_prefix,normalize_area_prefix
import pytest
from test_sms_gpt_alignment import sms, waiting_code


@pytest.mark.parametrize('value',['+1 832','1832','832','+1832','1-832',' 832 '])
def test_manual_area_input_normalizes_to_one_canonical_key(value):
    assert normalize_area_prefix(value)=='1832'


@pytest.mark.parametrize('value',['+1 083','083','123','+44 832','+18325550123','1832 OR 1=1','1,832','', '1832\n1833'])
def test_manual_area_input_rejects_full_numbers_and_invalid_areas(value):
    with pytest.raises(Error):normalize_area_prefix(value)


def test_removal_during_acquisition_does_not_keep_stale_pool_prefix():
    s,p=sms();s.m.addAutoExcludedArea('1202','fixedVoip');original=p.get_number
    def number(*args):
        assert '1202' in args[-1].split(',')
        s.m.state['autoAreas'].pop('1202')
        return original(*args)
    p.get_number=number
    assert s.redeem('valid-code')['status']=='WAITING_CODE'


@pytest.mark.parametrize('phone,prefix',[('+18325550123','1832'),('12025550123','1202'),
                                        ('+442079460123',None),('8325550123',None),('123',None)])
def test_auto_pool_uses_only_country_one_and_three_digit_area(phone,prefix):
    assert voip_area_prefix(phone)==prefix




def test_pool_is_refreshed_after_provider_returns_a_number():
    s,p=sms();original=p.get_number
    def number(*args):
        s.m.addAutoExcludedArea('1202','fixedVoip')
        return original(*args)
    p.get_number=number
    assert s.redeem('valid-code')['status']=='UNAVAILABLE'
    assert s.m.key['attemptCount']==0


@pytest.mark.parametrize('prefix',['1800','1833','1844','1855','1866','1877','1888'])
def test_country_and_toll_free_area_prefix_is_sent_and_rechecked(prefix):
    s,p=sms();s.m.key['smsPhoneException']='1800,1833,1844,1855,1866,1877,1888'
    p.phone=prefix+'5550123'
    sent=[];original=p.get_number
    def number(*args):
        sent.append(args[-1]);return original(*args)
    p.get_number=number
    view=s.redeem('valid-code')
    assert prefix in sent[0].split(',')
    assert view['status']=='UNAVAILABLE' and not view.get('smsCode')
    assert s.m.key['attemptCount']==0 and s.m.key['status']=='UNUSED'
    assert p.calls==1 and p.set_status_calls==[('activation-1',8)]


def test_geographic_number_is_not_classified_as_virtual_by_area_code():
    s,p=sms();s.m.key['smsPhoneException']='1800,1833,1844,1855,1866,1877,1888'
    p.phone='18325550123'
    assert s.redeem('valid-code')['status']=='WAITING_CODE'
    assert not p.set_status_calls


def test_java_prefix_rules_and_deduplication():
    assert exception_prefix('+1 (202) 555-0123') == '1202555'
    assert merge_exceptions('1832，1925 1832', ['+12025550123', '12025559876']) == '1832,1925,1202555'


def test_manual_retry_immediately_after_poll_checks_old_number_and_buys_once(monkeypatch):
    s,p=sms();sid=waiting_code(s,p,expires_in=600)
    monkeypatch.setattr('smsinvit.sms.service.time.monotonic',lambda:100.0)
    assert s.poll(sid)['status']=='WAITING_CODE'
    p.phone='13105550123'
    view=s.retry(sid)
    assert view['id']!=sid and view['phone']==p.phone
    assert p.calls==2 and p.get_status_calls==2
    assert p.set_status_calls==[('activation-1',8)]


def test_manual_retry_after_poll_still_saves_newly_delivered_code(monkeypatch):
    s,p=sms();sid=waiting_code(s,p,expires_in=600)
    monkeypatch.setattr('smsinvit.sms.service.time.monotonic',lambda:100.0)
    s.poll(sid);p.status=StatusResult('OK','445566','STATUS_OK:445566')
    view=s.retry(sid)
    assert view['status']=='SUCCESS' and view['smsCode']=='445566'
    assert p.calls==1 and not p.set_status_calls


def test_manual_retry_unknown_status_does_not_cancel_or_buy_again():
    s,p=sms();sid=waiting_code(s,p,expires_in=600)
    def unavailable(aid):
        raise Error('SMS_PROVIDER_ERROR','unavailable',502)
    p.get_status=unavailable
    assert s.retry(sid)['id']==sid
    assert p.calls==1 and not p.set_status_calls and s.m.key['attemptCount']==1


def test_retry_sends_sku_and_previous_number_prefixes_to_provider():
    s,p=sms();s.m.key['smsPhoneException']='1832，1925'
    sid=waiting_code(s,p,expires_in=600)
    calls=[];original=p.get_number
    def number(*args):
        calls.append(args);return original(*args)
    p.get_number=number;p.phone='13105550123'
    view=s.retry(sid)
    assert view['phone']==p.phone
    assert calls[0][-1]=='1832,1925,1202555'


def test_rejected_number_prefix_survives_reusing_refunded_attempt():
    s,p=sms();s.m.key['smsPhoneException']='1202'
    first=s.redeem('valid-code')
    assert first['status']=='UNAVAILABLE' and s.m.key['attemptCount']==0
    s.m.key['smsPhoneException']=None
    calls=[];original=p.get_number
    def number(*args):
        calls.append(args);return original(*args)
    p.get_number=number;p.phone='13105550123'
    second=s.retry(first['id'])
    assert second['status']=='WAITING_CODE' and second['attempt']==1
    assert calls[0][-1]=='1202555'

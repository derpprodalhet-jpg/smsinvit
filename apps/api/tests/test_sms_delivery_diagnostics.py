"""Offline delivery failures must be distinguishable from an empty inbox."""
from datetime import timedelta

import pytest

from smsinvit.sms.client import StatusResult
from smsinvit.sms.support import now
from test_sms_gpt_alignment import sms, waiting_code


@pytest.mark.parametrize('failure', ['network', 'provider'])
def test_failed_query_is_visible_without_rebuying_or_discarding_a_later_code(failure, monkeypatch):
    service, provider = sms()
    sid = waiting_code(service, provider, expires_in=600)
    original = provider.get_status
    clock = [100.0]
    monkeypatch.setattr('smsinvit.sms.service.time.monotonic', lambda: clock[0])

    def unavailable(aid):
        if failure == 'network':
            raise TimeoutError('synthetic transport failure')
        return StatusResult('ERROR', None, 'BAD_KEY')

    provider.get_status = unavailable
    view = service.poll(sid)
    assert view['statusQueryUnavailable'] is True
    assert view['status'] == 'WAITING_CODE'
    assert provider.calls == 1 and not provider.set_status_calls
    assert 'BAD_KEY' not in str(view)
    # Throttled reads must not flash the warning off before a successful retry.
    clock[0] += 1
    assert service.poll(sid)['statusQueryUnavailable'] is True

    clock[0] += 5
    provider.get_status = original
    provider.status = StatusResult('OK', '012345', 'STATUS_OK:012345')
    view = service.poll(sid)
    assert view['statusQueryUnavailable'] is False
    assert view['smsCode'] == '012345' and view['keyStatus'] == 'USED'
    assert provider.calls == 1


def test_session_deadlines_are_explicit_utc_on_the_wire():
    service, provider = sms()
    sid = waiting_code(service, provider, expires_in=600)
    view = service.view(sid)
    for field in ('cancelEligibleAt', 'numberExpiresAt', 'keyExpiresAt'):
        assert view[field].endswith('Z'), (field, view[field])


def test_sms_received_between_five_and_ten_minutes_still_completes_original_number():
    service, provider = sms()
    sid = waiting_code(service, provider, expires_in=240)
    service.m.state['sessions'][sid]['cancelEligibleAt'] = now() - timedelta(seconds=60)
    provider.status = StatusResult('OK', '012345', 'STATUS_OK:012345')
    view = service.poll(sid)
    assert view['smsCode'] == '012345' and view['status'] == 'SUCCESS'
    assert provider.calls == 1 and not provider.set_status_calls

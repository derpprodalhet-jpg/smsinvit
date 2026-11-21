"""Offline failure injection for CF creation, reads and deployment checks."""
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from curl_cffi.curl import CurlError
from requests.exceptions import ConnectTimeout

from smsinvit.invitation import safety
from mail_providers import cf_temp
from mail_providers.cf_temp import (
    CFTempEmailProvider, MailboxConfigurationError, MailboxConnectionError,
    MailboxCreationUncertainError,
)


def response(status=200, data=None, text=''):
    return SimpleNamespace(status_code=status, text=text, json=lambda: data)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(safety, 'require_url', Mock(return_value=('fixture.test', 443, ('8.8.8.8',))))
    monkeypatch.setattr(safety, 'require_proxy', Mock())
    monkeypatch.setattr(cf_temp.time, 'sleep', Mock())
    client = Mock()
    p = CFTempEmailProvider('https://private-fixture.test', 'private-fixture-token',
                            'fixture.test', session=client)
    p.proxy = ''
    p._urllib_request = Mock()
    return p


@pytest.mark.parametrize('failure', [TimeoutError('private-fixture-token'), CurlError('private-fixture-token', code=28), RuntimeError('private-fixture.test'), ValueError('response too large')])
def test_creation_uncertainty_sends_once_without_fallback(provider, failure, caplog):
    provider._session.post.side_effect = failure
    with pytest.raises(MailboxCreationUncertainError) as caught:
        provider.create_mailbox()
    assert provider._session.post.call_count == 1
    assert provider._session.post.call_args.kwargs['allow_redirects'] is False
    assert json.loads(provider._session.post.call_args.kwargs['data'])['enablePrefix'] is True
    provider._urllib_request.assert_not_called()
    assert not provider._current_email
    assert 'private-fixture' not in str(caught.value) + caplog.text


@pytest.mark.parametrize('failure', [ConnectTimeout('fixture'), CurlError('fixture', code=6), CurlError('fixture', code=7)])
def test_proven_predispatch_failure_is_connection_error_but_still_never_resends(provider, failure):
    provider._session.post.side_effect = failure
    with pytest.raises(MailboxConnectionError):
        provider.create_mailbox()
    assert provider._session.post.call_count == 1
    provider._urllib_request.assert_not_called()


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308, 408, 500, 502])
def test_redirects_and_unconfirmed_http_outcomes_are_review_only(provider, status):
    provider._session.post.return_value = response(status, {'email': 'untrusted@fixture.test'}, 'private-fixture-token')
    with pytest.raises(MailboxCreationUncertainError):
        provider.create_mailbox()
    assert provider._session.post.call_count == 1
    provider._urllib_request.assert_not_called()


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422, 429])
def test_explicit_rejection_is_safe_without_response_body_or_retry(provider, status, caplog):
    provider._session.post.return_value = response(status, {}, 'private-fixture-token')
    with pytest.raises(MailboxConnectionError) as caught:
        provider.create_mailbox()
    assert provider._session.post.call_count == 1
    assert 'private-fixture' not in str(caught.value) + caplog.text


@pytest.mark.parametrize('data', [None, [], {}, {'email': 7}, {'email': 'not-an-address'}, {'email': 'bad address@fixture.test'}])
def test_success_without_an_usable_address_does_not_release_creation_uncertainty(provider, data):
    provider._session.post.return_value = response(data=data)
    with pytest.raises(MailboxCreationUncertainError):
        provider.create_mailbox()
    provider._urllib_request.assert_not_called()


def test_malformed_success_json_is_uncertain(provider):
    provider._session.post.return_value = SimpleNamespace(status_code=200, json=Mock(side_effect=ValueError('private-fixture-token')))
    with pytest.raises(MailboxCreationUncertainError):
        provider.create_mailbox()
    assert provider._session.post.call_count == 1


@pytest.mark.parametrize('field,token_field', [('email', 'token'), ('address', 'jwt')])
def test_existing_creation_wire_formats_and_pinned_mailbox_remain_supported(provider, field, token_field):
    provider._session.post.return_value = response(data={field: 'created@fixture.test', token_field: 'fixture-jwt'})
    assert provider.create_mailbox() == 'created@fixture.test'
    assert provider._jwt == 'fixture-jwt'
    provider.pin_mailbox('pinned@fixture.test')
    assert provider.create_mailbox() == 'pinned@fixture.test'
    assert provider._session.post.call_count == 1


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_policy_precheck_stops_before_every_transport(provider, method, monkeypatch):
    check = Mock(side_effect=ValueError('private-fixture.test is blocked'))
    monkeypatch.setattr(safety, 'require_url', check)
    with pytest.raises(MailboxConfigurationError):
        provider._request(method, '/admin/mails' if method == 'GET' else '/admin/new_address')
    check.assert_called_once()
    provider._session.get.assert_not_called()
    provider._session.post.assert_not_called()
    provider._urllib_request.assert_not_called()


def test_proxy_policy_is_prechecked_before_creation(provider, monkeypatch):
    provider.proxy = 'http://127.0.0.1:1234'
    monkeypatch.setattr(safety, 'require_proxy', Mock(side_effect=ValueError('private-fixture-token')))
    with pytest.raises(MailboxConfigurationError):
        provider.create_mailbox()
    provider._session.post.assert_not_called()


def test_dns_failure_before_dispatch_is_connection_error(provider, monkeypatch):
    monkeypatch.setattr(safety, 'require_url', Mock(side_effect=socket.gaierror('fixture')))
    with pytest.raises(MailboxConnectionError):
        provider.create_mailbox()
    provider._session.post.assert_not_called()


def test_read_network_failures_share_three_attempts_including_fallback(provider):
    provider._session.get.side_effect = TimeoutError('private-fixture-token')
    provider._urllib_request.side_effect = TimeoutError('private-fixture.test')
    with pytest.raises(MailboxConnectionError):
        provider._request('GET', '/admin/mails')
    assert provider._session.get.call_count == 2
    assert provider._urllib_request.call_count == 1


def test_read_retries_transient_http_response_then_stops_on_success(provider):
    provider._session.get.side_effect = [response(503), response(data={'results': []})]
    assert provider._get_mails('fixture@fixture.test') == []
    assert provider._session.get.call_count == 2
    provider._urllib_request.assert_not_called()


def test_read_policy_rejection_inside_transport_is_not_retried(provider):
    provider._session.get.side_effect = ValueError('private-fixture.test is blocked')
    with pytest.raises(MailboxConfigurationError):
        provider._request('GET', '/admin/mails')
    assert provider._session.get.call_count == 1
    provider._urllib_request.assert_not_called()


def test_selected_fallback_transport_cannot_replay_or_follow_creation_redirect(provider, monkeypatch):
    provider._session = None
    provider._urllib_request = CFTempEmailProvider._urllib_request.__get__(provider)
    client = Mock()
    client.request.return_value = response(307)
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock()
    monkeypatch.setattr(safety, 'GuardedRequestsSession', Mock(return_value=client))
    with pytest.raises(MailboxCreationUncertainError):
        provider.create_mailbox()
    assert client.request.call_count == 1
    assert client.request.call_args.kwargs['allow_redirects'] is False
    assert client.request.call_args.args[0] == 'POST'


def test_guarded_fallback_has_no_automatic_transport_retries():
    with safety.GuardedRequestsSession() as client:
        assert client.get_adapter('https://fixture.test/').max_retries.total == 0


@pytest.mark.parametrize('data', [{'results': []}, {'mails': []}, []])
def test_connection_test_uses_only_read_endpoint_and_uncreated_diagnostic_address(provider, data):
    provider.pin_mailbox('actual-mailbox@fixture.test')
    provider._session.get.return_value = response(data=data)
    assert provider.self_test()['ok'] is True
    provider._session.post.assert_not_called()
    request = provider._session.get.call_args
    assert request.args[0].endswith('/admin/mails')
    assert request.kwargs['params']['limit'] == 1
    assert request.kwargs['params']['address'].startswith('smsinvit-readonly-')
    assert request.kwargs['params']['address'].endswith('@fixture.test')
    assert provider._current_email == 'actual-mailbox@fixture.test'


def test_connection_test_does_not_claim_html_or_unknown_json_is_healthy(provider):
    provider._session.get.return_value = response(data={})
    assert provider.self_test()['ok'] is False
    provider._session.post.assert_not_called()


def test_incomplete_configuration_has_typed_safe_failure():
    with pytest.raises(MailboxConfigurationError):
        CFTempEmailProvider.from_config({'cf_api_url': 'https://private-fixture.test'})

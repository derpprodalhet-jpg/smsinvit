"""Proxy connectivity probe regressions; every network transfer is mocked."""
import logging
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest


def test_probe_reuses_guarded_session_proxy_and_tls_without_logging_ip(monkeypatch,caplog):
    import auth_flow
    import http_client
    from smsinvit.invitation import safety
    from curl_cffi import CurlOpt

    target='https://api.ipify.org?format=json'
    proxy='http://synthetic-user:synthetic-password@proxy.example.test:8080'
    guard_calls=[];transfers=[]
    def checked_url(url):
        assert url==target and urlsplit(url).hostname in safety.OFFICIAL_HOSTS
        guard_calls.append(('target',url))
        return 'api.ipify.org',443,('203.0.113.10',)
    def checked_proxy(value):
        assert value==proxy
        guard_calls.append(('proxy',value))
        return 'proxy.example.test',8080,('203.0.113.11',)
    def transfer(session,method,url,*args,**kwargs):
        transfers.append((session,method,url,kwargs))
        assert kwargs['verify'] is True and kwargs['timeout']==15
        assert kwargs['proxies']=={'https':proxy,'http':proxy}
        assert session.curl_options[CurlOpt.SSL_VERIFYPEER]==1
        assert session.curl_options[CurlOpt.SSL_VERIFYHOST]==2
        assert session.curl_options[CurlOpt.PROXY_SSL_VERIFYPEER]==1
        kwargs['content_callback'](b'{"ip":"203.0.113.99"}')
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(safety,'require_url',checked_url)
    monkeypatch.setattr(safety,'require_proxy',checked_proxy)
    monkeypatch.setattr(safety._cffi.Session,'request',transfer)
    caplog.set_level(logging.INFO,logger='auth_flow')
    flow=auth_flow.AuthFlow(SimpleNamespace(proxy=proxy))
    original=flow.session
    assert isinstance(original,http_client._TlsRetrySession)
    assert isinstance(original._inner,safety.GuardedCffiSession)
    try:
        assert flow.check_proxy() is True
        assert flow.session is original and len(transfers)==1
        assert guard_calls==[('target',target),('proxy',proxy),('proxy',proxy)]
        assert '203.0.113.99' not in caplog.text and proxy not in caplog.text
        assert flow._country_code==''
    finally:original.close()


@pytest.mark.parametrize('status',[200,503])
def test_probe_remains_advisory_without_json_or_ip(status,monkeypatch):
    import auth_flow
    flow=auth_flow.AuthFlow.__new__(auth_flow.AuthFlow)
    response=SimpleNamespace(status_code=status)
    flow.session=SimpleNamespace(get=Mock(return_value=response))
    rebuild=Mock(side_effect=AssertionError('The probe must not create another session'))
    monkeypatch.setattr(auth_flow,'create_http_session',rebuild)
    assert flow.check_proxy() is True
    # 探测现在是两次**都在白名单里**的请求：ipify 判连通、chatgpt.com trace 取国家
    called=[item.args[0] for item in flow.session.get.call_args_list]
    assert called[0]=='https://api.ipify.org?format=json'
    assert all(url.startswith('https://api.ipify.org') or url.startswith('https://chatgpt.com/')
               for url in called), called
    assert all(item.kwargs['timeout']==15 for item in flow.session.get.call_args_list)
    rebuild.assert_not_called()


def test_probe_guard_failure_remains_false_and_logs_no_exception_credentials(monkeypatch,caplog):
    import auth_flow
    from smsinvit.invitation import safety
    secret='synthetic-proxy-password'
    flow=auth_flow.AuthFlow.__new__(auth_flow.AuthFlow)
    session=safety.GuardedCffiSession()
    flow.session=session
    def deny(url):raise ValueError('synthetic guard rejection '+secret)
    monkeypatch.setattr(safety,'require_url',deny)
    transfer=Mock(side_effect=AssertionError('A rejected target must never be sent'))
    monkeypatch.setattr(safety._cffi.Session,'request',transfer)
    caplog.set_level(logging.ERROR,logger='auth_flow')
    try:
        assert flow.check_proxy() is False
        transfer.assert_not_called()
        assert 'ValueError' in caplog.text and secret not in caplog.text
    finally:session.close()

"""代理可见性：怎么知道这次到底走了哪个出口。

事故背景：注册结果/邮箱管理里显示「直连」，看起来像没走代理 —— 其实注册确实
用了代理，只是完成时没把代理写进账号行；而 `check_proxy` 虽然拿到了出口 IP，
却只打了一句"网络正常"，出事时无法判断用的是哪个出口/哪个国家。
"""
import logging
from types import SimpleNamespace

from auth_flow import AuthFlow


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def flow(proxy, ipify=None, trace=None, capture=None):
    item = AuthFlow.__new__(AuthFlow)
    item.config = SimpleNamespace(proxy=proxy)

    def get(url, timeout=0):
        if capture is not None:
            capture.append(url)
        if "ipify" in url:
            if isinstance(ipify, Exception):
                raise ipify
            return ipify
        if isinstance(trace, Exception):
            raise trace
        return trace

    item.session = SimpleNamespace(get=get)
    return item


def run_and_capture(caplog, item):
    with caplog.at_level(logging.INFO):
        ok = item.check_proxy()
    return ok, [record.getMessage() for record in caplog.records]


def test_country_and_proxy_usage_are_logged_but_never_the_exit_ip(caplog):
    """要能看出"用了代理、走的哪个国家"，但**不打出口 IP**（既有安全策略）。"""
    item = flow('http://u:p@1.2.3.4:8080',
                ipify=FakeResponse(payload={'ip': '203.0.113.9'}),
                trace=FakeResponse(text='fl=123\nip=203.0.113.9\nloc=US\ncolo=LAX\n'))
    ok, messages = run_and_capture(caplog, item)
    assert ok
    joined = ' | '.join(messages)
    assert '使用代理' in joined and '国家=US' in joined
    assert item._country_code == 'US'
    # 出口 IP 与代理口令都不得进日志
    assert '203.0.113.9' not in joined
    assert 'u:p@' not in joined


def test_direct_connection_is_called_out(caplog):
    item = flow('', ipify=FakeResponse(payload={'ip': '198.51.100.7'}),
                trace=FakeResponse(text='loc=PH\n'))
    ok, messages = run_and_capture(caplog, item)
    assert ok
    joined = ' | '.join(messages)
    assert '直连' in joined and '国家=PH' in joined and item._country_code == 'PH'
    assert '198.51.100.7' not in joined
    # 直连必须给出明确建议，而不是静悄悄
    assert any('未使用代理' in message for message in messages)


def test_country_lookup_failure_never_breaks_the_probe(caplog):
    item = flow('http://u:p@1.2.3.4:8080',
                ipify=FakeResponse(payload={'ip': '203.0.113.9'}),
                trace=RuntimeError('synthetic trace failure'))
    ok, messages = run_and_capture(caplog, item)
    assert ok and item._country_code == ''
    assert any('国家=未知' in message for message in messages)


def test_probe_failure_with_a_proxy_points_at_the_proxy(caplog):
    item = flow('http://u:p@1.2.3.4:8080',
                ipify=RuntimeError('curl: (7) CONNECT tunnel failed, response 403'))
    with caplog.at_level(logging.ERROR):
        assert item.check_proxy() is False
    assert any('代理可能不可用' in record.getMessage() for record in caplog.records)


def test_country_uses_an_allowlisted_host(caplog):
    """国家信息走 chatgpt.com 的 Cloudflare trace —— 不额外放宽出站白名单。"""
    capture = []
    item = flow('http://u:p@1.2.3.4:8080',
                ipify=FakeResponse(payload={'ip': '203.0.113.9'}),
                trace=FakeResponse(text='loc=US\n'), capture=capture)
    run_and_capture(caplog, item)
    assert any(url.startswith('https://chatgpt.com/cdn-cgi/trace') for url in capture), capture

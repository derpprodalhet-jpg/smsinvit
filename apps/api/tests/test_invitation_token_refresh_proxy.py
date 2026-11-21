"""A saved token refresh uses the same explicitly selected proxy, with no retries."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from webui import exporter


def test_refresh_uses_selected_proxy_and_preserves_tls(monkeypatch):
    post = Mock(return_value=SimpleNamespace(status_code=200,
        json=lambda: {'access_token': 'fixture-access', 'refresh_token': 'fixture-rotated'}))
    monkeypatch.setattr(exporter, '_import_cffi', lambda: SimpleNamespace(post=post))
    result = exporter.refresh_codex_token('fixture-refresh', proxy='socks5://fixture:secret@proxy.example:1080')
    assert result['refresh_token'] == 'fixture-rotated'
    assert post.call_count == 1
    args, kwargs = post.call_args
    assert args == (exporter.OPENAI_TOKEN_ENDPOINT,)
    assert kwargs['proxies'] == dict(http='socks5h://fixture:secret@proxy.example:1080',
                                    https='socks5h://fixture:secret@proxy.example:1080')
    assert kwargs['verify'] is True


def test_lost_refresh_response_is_not_replayed(monkeypatch):
    post = Mock(side_effect=TimeoutError('fixture timeout'))
    monkeypatch.setattr(exporter, '_import_cffi', lambda: SimpleNamespace(post=post))
    with pytest.raises(TimeoutError):
        exporter.refresh_codex_token('fixture-refresh', proxy='http://proxy.example:8080')
    assert post.call_count == 1

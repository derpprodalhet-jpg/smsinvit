"""Selected SMS platforms at config and country-list boundaries; no suppliers."""
import ast
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import HTTPException
from pydantic import BaseModel
from webui import sms_prices


@pytest.fixture
def routes(monkeypatch):
    source = Path(__file__).resolve().parents[3] / 'vendor/codex_invit/webui/app.py'
    names = {'SaveSmsConfigReq', 'api_save_sms_config', '_sms_country_quotes',
             'api_sms_all_countries', 'api_sms_top_countries'}
    nodes = [node for node in ast.parse(source.read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    cfg = {'sms_service': 'dr', 'sms_platform_mode': 'smsbower,herosms',
           **{'sms_api_key_' + pk: 'offline-fixture-key' for pk in sms_prices.PLATFORMS}}
    now = time.time()
    rows = [dict(platform=pk, country=country, price=price, count=50, updated_at=now)
            for pk, country, price in [('smsbower', '52', .08), ('herosms', '52', .06),
                                        ('herosms', '7', .09), ('smspool', '187', .01)]]
    writes = []
    repo = SimpleNamespace(get_sms_internal_config=lambda: dict(cfg),
        sms_price_state_list=lambda: [], sms_price_list=lambda **kw: rows,
        save_sms_config=lambda data: writes.append(data),
        get_sms_config=lambda: dict(cfg), _is_masked_secret=lambda value: value == '***')
    env = {'__file__': str(source), 'db': repo, 'sms_prices': sms_prices, 'time': time,
           'Path': Path, 'BaseModel': BaseModel, 'Optional': Optional, 'HTTPException': HTTPException}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), env)
    def forbidden(*args, **kwargs):
        pytest.fail('Fresh selected-platform snapshots must not call a supplier')
    monkeypatch.setattr(sms_prices.SmsPriceMonitor, '_readonly_provider', forbidden)
    return SimpleNamespace(env=env, cfg=cfg, writes=writes, rows=rows)


@pytest.mark.parametrize('mode,expected', [('', 'auto'), ('auto', 'auto'),
    ('HeroSMS', 'herosms'), ('smsbower, HeroSMS,smsbower', 'smsbower,herosms')])
def test_selection_save_is_normalized_without_changing_other_settings(routes, mode, expected):
    routes.env['api_save_sms_config'](routes.env['SaveSmsConfigReq'](sms_platform_mode=mode))
    assert routes.writes == [{'sms_platform_mode': expected}]


@pytest.mark.parametrize('mode', ['unknown', 'smsbower,unknown', 'auto,herosms'])
def test_invalid_selection_never_saves_or_expands_scope(routes, mode):
    with pytest.raises(HTTPException) as exc:
        routes.env['api_save_sms_config'](routes.env['SaveSmsConfigReq'](sms_platform_mode=mode))
    assert exc.value.status_code == 400 and routes.writes == []


def test_multiple_platform_country_list_merges_without_unselected_platform(routes):
    result = routes.env['api_sms_all_countries']('smsbower,herosms')
    assert result['source'] == 'cache'
    assert [(row['id'], row['price']) for row in result['countries']] == [('52', .06), ('7', .09)]


def test_single_platform_country_list_uses_only_that_platform(routes):
    result = routes.env['api_sms_all_countries']('smsbower')
    assert [(row['id'], row['price']) for row in result['countries']] == [('52', .08)]
    routes.cfg['sms_platform_mode'] = 'herosms'
    result = routes.env['api_sms_top_countries']()
    assert {row['platform'] for row in result['countries']} == {'herosms'}

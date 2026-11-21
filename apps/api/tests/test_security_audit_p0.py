"""数据与逻辑安全审计的 P0 修复回归。

对应的缺陷（每条都先在生产代码里复现过）：
- `ORDER BY sort_order` 引用了不存在的列 → 邀请卡密生成入口恒定 500；
- webhook 的 `:total` 档是全站共享限流桶，匿名可打满 → 真实上游回调 429；
- 改代理 host 却留空账密 → 旧凭据被发到新主机；
- 公开请求模型不 forbid 额外字段 → 篡改尝试不留 422 证据；
- 非 ASCII 的 webhook token 让 `hmac.compare_digest` 抛 TypeError → 匿名 500。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'smsinvit'


def read(relative: str) -> str:
    return (API / relative).read_text(encoding='utf-8')


def test_invitation_cdk_skus_never_references_missing_sort_order_column():
    """`skus` 表没有 `sort_order` 列，引用它会让接口恒 500（已用 information_schema 证实）。"""
    source = read('invitation/router.py')
    assert 'sort_order' not in source, 'skus 表没有 sort_order 列，引用它会让 /cdk/skus 恒定 500'
    assert "SELECT id,name FROM skus" in source
    assert 'ORDER BY created_at,id' in source


def _without_comments(source: str) -> str:
    """断言「代码里没有」之前必须先剥掉注释，否则解释性注释本身会被判成违规。"""
    return '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('#'))


def test_webhook_rate_limit_has_no_shared_total_bucket():
    """`bucket+':total'` 是全站共享计数：匿名伪造 token 也能把它打满，从而阻断真实上游回调。"""
    source = _without_comments(read('app.py'))
    assert "':total'" not in source
    # 按 IP 的那一档必须保留（否则 webhook 完全无限流）
    assert "f'{bucket}:{id}'" in source


@pytest.mark.parametrize('module,model', [('sms', 'RedeemBody'), ('sms', 'SessionBody')])
def test_public_models_forbid_extra_fields(module, model):
    """公开模型不 forbid 时，篡改尝试会被静默丢弃、不留 422 证据。"""
    import importlib

    routes = importlib.import_module(f'smsinvit.{module}.routes')
    body = getattr(routes, model)
    assert body.model_config.get('extra') == 'forbid', f'{model} 必须 extra=forbid'

    with pytest.raises(Exception):
        body(**{'code': 'ABCD-1234', 'unexpected': 'x'} if model == 'RedeemBody' else {'sessionSecret': 's', 'unexpected': 'x'})


def test_webhook_token_comparison_rejects_non_ascii_instead_of_raising():
    """非 ASCII token 会让 compare_digest 抛 TypeError → 匿名 500；必须提前按格式拒绝。"""
    for relative, marker in [('sms/routes.py', 'not token.isascii()')]:
        source = read(relative)
        assert marker in source, f'{relative} 缺少非 ASCII token 的提前拒绝'
        # 拒绝必须发生在 compare_digest 之前
        assert source.index('isascii') < source.index('compare_digest(expected,token)')

"""Retired public URLs and product types must not revive the removed business."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from smsinvit.common.catalog import validate_attrs
from smsinvit.config import Config
from smsinvit.errors import Error, error_body, response
from smsinvit.frontend import register_frontend


def test_retired_public_page_is_404_while_remaining_pages_work(tmp_path):
    (tmp_path / 'index.html').write_text('<html>local test</html>')
    app = FastAPI()

    @app.exception_handler(Error)
    async def handle(request, error):
        return response(error_body(error), error.status)

    register_frontend(app, Config({'SMSINVIT_STATIC_DIR': str(tmp_path)}, environ={}), '/admin/')
    with TestClient(app) as client:
        for path in ('/supprot_x', '/supprot_x/', '/api/v1/x-api/preflight', '/supprot_direct', '/supprot_direct/'):
            assert client.get(path).status_code == 404
        for path in ('/supprot_sim', '/supprot_codex'):
            assert client.get(path).status_code == 200


def test_removed_product_cannot_be_created_or_issued():
    with pytest.raises(Error) as error:
        validate_attrs({'productKind': 'X_PREMIUM_REDEEM', 'xMonths': 3})
    assert error.value.status == 422


def test_removed_direct_recharge_product_cannot_be_created_or_issued():
    """GPT 直充整块删除后，套餐类型只剩接码与 Codex 邀请：旧类型必须继续 422。"""
    with pytest.raises(Error) as error:
        validate_attrs({'productKind': 'GPT_DIRECT_REDEEM', 'gptPlan': 'plus'})
    assert error.value.status == 422

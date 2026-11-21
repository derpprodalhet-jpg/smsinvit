"""批次页必须覆盖**全部**卡密：已登记批次 + 未登记批次 = 全部。

真实事故形态（2026-10 运营反馈「codex 邀请的卡密和实际生成的卡密对不上，一个多一个少」）：
CODEX 套餐有 21 张 `inventory_items`，其中只有 10 张属于 `inventory_import_batches` 里
那个批次（c5388f7f…），另外 11 张是历史导入（邀请卡密表 `batch=20261007-014828`，10 张）
与人工创建（`batch=manual`，1 张），`import_batch_id IS NULL`。批次列表只列 10 张，
「卡密管理」页列 21 张 —— 两张页面相差 11，运营无法对账。

修法是展示层口径对齐：批次列表补一个保留 id 的「未登记批次」伪批次行（只读），
`meta.counts` 给出已登记 / 未登记 / 全部三组数字。**不改**卡密与批次数据本身。

这里的库、mapper、邀请模块都是假实现（不连真库）；权限与路由用真的 FastAPI 路由 + 真的
PermissionService 跑一遍，契约（路径 / 权限 / 422 边界）与数据口径一起钉住。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from smsinvit.common import inventory as inventory_module
from smsinvit.common.inventory import UNASSIGNED_BATCH_ID, Inventory
from smsinvit.errors import Error, error_body
from smsinvit.security import PermissionService, Principal

SKU_CODEX = '0e753535-58aa-4a17-a224-d4115443bd67'
SKU_X = '45094c32-772a-4502-bdf3-196af58f9350'
BATCH_CODEX = 'c5388f7f-e836-49d8-b267-b267a0906a15'
BATCH_X = '9a3e47ac-00d0-4c22-88d7-cbd3501cdcbe'
ORIGINS = {'20261007-014828': 10, 'manual': 1}


# ──────────────────────── 假实现 ────────────────────────

class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCodec:
    def decrypt(self, value):
        if not value or value == 'BROKEN':
            raise ValueError('cannot decrypt')
        return value


class FakeDb:
    """只服务 `inventory_code_map` 的按 id 读取。"""

    def __init__(self, items):
        self.items = items

    def transaction(self):
        return _Transaction()

    def all(self, sql, *params):
        wanted = {str(p) for p in params}
        return [{'id': item['id'], 'secretCiphertext': item['secretCiphertext']}
                for item in self.items if str(item['id']) in wanted]


class RecordingAudit:
    def __init__(self):
        self.records = []

    def record(self, actor, action, resource, target, meta=None):
        self.records.append({'actor': actor, 'action': action, 'resource': resource,
                             'target': target, 'meta': meta})


class FakeInvitation:
    """邀请模块只提供「原始批次名」，其它一概不参与。"""

    def __init__(self, origins=None):
        self.origins = dict(ORIGINS if origins is None else origins)

    def unassigned_origin_batches(self, sku_id):
        return [{'batch': batch, 'total': total} for batch, total in
                (self.origins.items() if str(sku_id) == SKU_CODEX else [])]


class FakeMapper:
    """按 `sku_id` / `import_batch_id` 过滤的内存版 InventoryMapper。"""

    def __init__(self, batches, items):
        self.batches, self.items = batches, items

    def listBatches(self, sku, limit, offset):
        rows = [dict(row) for row in self.batches if str(row['skuId']) == str(sku)]
        rows.sort(key=lambda row: str(row['createdAt']), reverse=True)
        return rows[offset:offset + limit]

    def countBatches(self, sku):
        return len([row for row in self.batches if str(row['skuId']) == str(sku)])

    def countCardsByBatch(self, sku):
        counts = {}
        for item in self.items:
            if str(item['skuId']) != str(sku) or not item['importBatchId']:
                continue
            key = str(item['importBatchId'])
            counts[key] = counts.get(key, 0) + 1
        return [{'batchId': key, 'total': total} for key, total in counts.items()]

    def countUnassignedCards(self, sku):
        return len([item for item in self.items
                    if str(item['skuId']) == str(sku) and not item['importBatchId']])

    def countItems(self, sku, status):
        return len([item for item in self.items if str(item['skuId']) == str(sku)
                    and (not status or item['status'] == status)])

    def findBatch(self, batch_id):
        return next((dict(row) for row in self.batches if str(row['id']) == str(batch_id)), None)

    def listBatchCards(self, batch_id, limit, offset):
        rows = [dict(item) for item in self.items
                if str(item['importBatchId'] or '') == str(batch_id)]
        return rows[offset:offset + limit]

    def countBatchCards(self, batch_id):
        return len([item for item in self.items
                    if str(item['importBatchId'] or '') == str(batch_id)])

    def listUnassignedCards(self, sku, limit, offset):
        rows = [dict(item) for item in self.items
                if str(item['skuId']) == str(sku) and not item['importBatchId']]
        return rows[offset:offset + limit]


def card(index, sku, batch_id=None, status='ISSUED'):
    return {'id': f'card-{index}', 'skuId': sku, 'importBatchId': batch_id, 'status': status,
            'issuedAt': '2026-10-05T13:12:56Z', 'secretFingerprint': (f'{index}-secret').ljust(64, '0'),
            'secretCiphertext': f'LC-CDK-SECRET-{index}'}


def fixture_items():
    """CODEX：10 张已登记 + 11 张未登记（10 历史导入 + 1 手工）= 21；X 套餐另有一批 2 张。"""
    items = [card(index, SKU_CODEX, BATCH_CODEX) for index in range(10)]
    items += [card(f'u{index}', SKU_CODEX) for index in range(11)]
    items += [card(f'x{index}', SKU_X, BATCH_X, status='CONSUMED') for index in range(2)]
    return items


def fixture_batches():
    return [
        {'id': BATCH_CODEX, 'skuId': SKU_CODEX, 'source': 'GENERATE', 'requestedCount': 10,
         'successCount': 10, 'duplicateCount': 0, 'invalidCount': 0, 'paramsJson': None,
         'attributesSnapshotJson': None, 'note': None, 'actorId': 'admin-1',
         'actorEmail': 'admin@example.test', 'createdAt': '2026-10-05 13:12:56.526000'},
        {'id': BATCH_X, 'skuId': SKU_X, 'source': 'GENERATE', 'requestedCount': 2,
         'successCount': 2, 'duplicateCount': 0, 'invalidCount': 0, 'paramsJson': None,
         'attributesSnapshotJson': None, 'note': None, 'actorId': 'admin-1',
         'actorEmail': 'admin@example.test', 'createdAt': '2026-10-09 17:35:24.975000'},
    ]


def engine_for(items=None, batches=None, invitation=True):
    items = fixture_items() if items is None else items
    batches = fixture_batches() if batches is None else batches
    services = SimpleNamespace(
        db=FakeDb(items),
        codec=FakeCodec(),
        audit=RecordingAudit(),
        config=SimpleNamespace(get=lambda key, default=None: default),
    )
    if invitation:
        services.invitation = FakeInvitation()
    engine = Inventory(services)
    engine.mapper = FakeMapper(batches, items)
    services.inventory = engine
    return engine, services


def counts_of(result):
    return result['meta']['counts']


# ──────────────────────── 口径：已登记 + 未登记 = 全部 ────────────────────────

def test_batch_rows_plus_unassigned_equal_every_card_of_the_sku():
    """核心断言：批次列表张数合计 + 未登记批次数 = 全部卡密数（21）。"""
    engine, _ = engine_for()
    result = engine.batches(SKU_CODEX, 1, 20)
    rows = result['data']
    assert [row['id'] for row in rows] == [BATCH_CODEX, UNASSIGNED_BATCH_ID]
    assert counts_of(result) == {'registeredCards': 10, 'unassignedCards': 11, 'totalCards': 21}
    # 「批次列表张数合计」按页面实际列出的张数算，必须正好等于全部卡密数。
    assert sum(row['cardCount'] for row in rows) == counts_of(result)['totalCards'] == 21
    assert counts_of(result)['registeredCards'] + counts_of(result)['unassignedCards'] == 21


def test_unassigned_row_never_invents_a_batch_number_or_time():
    """历史卡密没有批次凭据：批次号、来源、生成时间必须留空，原始批次名如实回填。"""
    engine, _ = engine_for()
    row = engine.batches(SKU_CODEX, 1, 20)['data'][1]
    assert row['unassigned'] is True and row['id'] == UNASSIGNED_BATCH_ID
    assert row['batchNo'] == '' and row['source'] == '' and row['createdAt'] is None
    assert row['cardCount'] == 11 and row['successCount'] is None
    assert row['originBatches'] == [{'batch': '20261007-014828', 'total': 10},
                                    {'batch': 'manual', 'total': 1}]
    assert UNASSIGNED_BATCH_ID not in str(row['batchNo'])


def test_registered_rows_use_the_live_card_count_and_keep_their_batch_number():
    engine, _ = engine_for()
    row = engine.batches(SKU_CODEX, 1, 20)['data'][0]
    assert row['cardCount'] == 10 and row['unassigned'] is False
    assert row['batchNo'] == 'BATCH-20261005-C5388F7F'


def test_counts_are_per_sku_and_never_borrow_another_skus_cards():
    """X 套餐的 2 张在它自己的批次里，不能算进 CODEX 的 21。"""
    engine, _ = engine_for()
    result = engine.batches(SKU_X, 1, 20)
    assert [row['id'] for row in result['data']] == [BATCH_X]
    assert counts_of(result) == {'registeredCards': 2, 'unassignedCards': 0, 'totalCards': 2}
    assert result['meta']['total'] == 1, '没有未登记卡密时不应该多出一行伪批次'


def test_pseudo_row_is_the_last_page_and_counts_stay_page_independent():
    """伪批次排在所有真实批次之后：分页时三组数字仍然是整表口径。"""
    items = [card(index, SKU_CODEX, BATCH_CODEX) for index in range(3)]
    items += [card(f'u{index}', SKU_CODEX) for index in range(4)]
    batches = [dict(fixture_batches()[0], requestedCount=1, successCount=1),
               dict(fixture_batches()[1], skuId=SKU_CODEX, id='batch-2',
                    createdAt='2026-10-04 01:00:00.000000')]
    engine, _ = engine_for(items, batches)
    first = engine.batches(SKU_CODEX, 1, 2)
    assert [row['id'] for row in first['data']] == [batches[0]['id'], 'batch-2']
    assert counts_of(first) == {'registeredCards': 3, 'unassignedCards': 4, 'totalCards': 7}
    second = engine.batches(SKU_CODEX, 2, 2)
    assert [row['id'] for row in second['data']] == [UNASSIGNED_BATCH_ID]
    assert second['data'][0]['cardCount'] == 4
    assert counts_of(second) == counts_of(first)
    assert first['meta']['total'] == second['meta']['total'] == 3


def test_pseudo_row_is_omitted_when_every_card_has_a_batch():
    engine, _ = engine_for()
    result = engine.batches(SKU_X, 1, 20)
    assert all(row['id'] != UNASSIGNED_BATCH_ID for row in result['data'])
    assert result['meta']['total'] == 1


def test_origin_batches_degrade_to_empty_without_the_invitation_module():
    engine, _ = engine_for(invitation=False)
    assert engine.batches(SKU_CODEX, 1, 20)['data'][1]['originBatches'] == []


# ──────────────────────── 未登记批次的只读卡密列表 ────────────────────────

def test_unassigned_cards_list_only_that_skus_unregistered_cards():
    engine, _ = engine_for()
    actor = SimpleNamespace(user_id='admin-1')
    result = engine.cards(UNASSIGNED_BATCH_ID, 1, 20, actor, False, sku=SKU_CODEX)
    assert result['meta']['total'] == 11
    assert {row['id'] for row in result['data']} == {f'card-u{index}' for index in range(11)}
    for row in result['data']:
        assert 'secret' not in row, '无 inventory.export 时不得返回明文'
        assert row['secretMasked'] and row['secretMasked'] != '—'


def test_unassigned_cards_reveal_is_audited_against_the_reserved_id():
    engine, services = engine_for()
    actor = SimpleNamespace(user_id='admin-1')
    result = engine.cards(UNASSIGNED_BATCH_ID, 1, 20, actor, True, sku=SKU_CODEX)
    assert all(row['secret'].startswith('LC-CDK-SECRET') for row in result['data'])
    assert services.audit.records == [{'actor': 'admin-1', 'action': 'inventory.batch_reveal',
                                       'resource': 'inventory_batch',
                                       'target': UNASSIGNED_BATCH_ID, 'meta': {'count': 11}}]


def test_unassigned_cards_require_a_sku():
    engine, _ = engine_for()
    actor = SimpleNamespace(user_id='admin-1')
    with pytest.raises(Error) as error:
        engine.cards(UNASSIGNED_BATCH_ID, 1, 20, actor, False)
    assert error.value.code == 'VALIDATION_ERROR'
    assert error.value.status == 422


def test_registered_batch_reading_is_unchanged_and_unknown_batches_still_404():
    engine, services = engine_for()
    actor = SimpleNamespace(user_id='admin-1')
    assert engine.cards(BATCH_CODEX, 1, 20, actor, True)['meta']['total'] == 10
    assert [row['action'] for row in services.audit.records] == ['inventory.batch_reveal']
    with pytest.raises(Error) as error:
        engine.cards('00000000-0000-4000-8000-000000000000', 1, 20, actor, False)
    assert error.value.code == 'INVENTORY_BATCH_NOT_FOUND'


def test_pseudo_batch_has_no_export_path():
    """`unassigned` 不是 `inventory_import_batches` 的行，导出必须 404 而不是伪造批次。"""
    engine, _ = engine_for()
    with pytest.raises(Error) as error:
        engine.export(UNASSIGNED_BATCH_ID, SimpleNamespace(user_id='admin-1'))
    assert error.value.code == 'INVENTORY_BATCH_NOT_FOUND'


# ──────────────────────── 路由 / 权限契约 ────────────────────────

class Grants:
    def __init__(self, allowed):
        self.allowed = set(allowed)

    def all(self, sql, *args):
        return [{'permission_scope': 'ALL'}] if args[-1] in self.allowed else []


def client_for(engine, services, grants, principal=None):
    services.permissions = PermissionService(Grants(grants))
    app = FastAPI()
    app.state.services = services

    @app.middleware('http')
    async def principal_fixture(request, call_next):
        if principal is not None:
            request.state.principal = principal
        return await call_next(request)

    @app.exception_handler(Error)
    async def expected(request, error):
        return JSONResponse(error_body(error), status_code=error.status)

    app.include_router(inventory_module.router)
    return TestClient(app)


def test_cards_route_is_registered_with_the_reserved_id_path():
    registered = {(getattr(route, 'path', ''), method)
                  for route in inventory_module.router.routes
                  for method in getattr(route, 'methods', set())}
    assert ('/api/v1/admin/inventory/batches/{id}/cards', 'GET') in registered


def test_unassigned_cards_route_enforces_login_permission_and_sku():
    engine, services = engine_for()
    admin = Principal('admin-1', 'session-1', ('ADMIN',), False)
    with client_for(engine, services, ('inventory.read',)) as anonymous:
        assert anonymous.get(f'/api/v1/admin/inventory/batches/{UNASSIGNED_BATCH_ID}/cards'
                             f'?skuId={SKU_CODEX}').status_code == 401
    with client_for(engine, services, (), admin) as denied:
        response = denied.get(f'/api/v1/admin/inventory/batches/{UNASSIGNED_BATCH_ID}/cards'
                              f'?skuId={SKU_CODEX}')
        assert response.status_code == 403 and response.json()['error']['code'] == 'PERMISSION_DENIED'
    with client_for(engine, services, ('inventory.read',), admin) as allowed:
        response = allowed.get(f'/api/v1/admin/inventory/batches/{UNASSIGNED_BATCH_ID}/cards'
                               f'?skuId={SKU_CODEX}')
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'
        body = response.json()
        assert body['meta']['total'] == 11 and len(body['data']) == 11
        assert body['data'][0]['secretMasked'] and 'secret' not in body['data'][0]
        # 未登记批次必须带套餐，否则范围无法确定。
        assert allowed.get(f'/api/v1/admin/inventory/batches/{UNASSIGNED_BATCH_ID}/cards'
                           ).status_code == 422
        # 只有 UUID 或保留值能进服务层，其余输入仍是 422（不落进 SQL）。
        assert allowed.get('/api/v1/admin/inventory/batches/not-a-batch/cards'
                           f'?skuId={SKU_CODEX}').status_code == 422


def test_unassigned_cards_route_reveals_with_export_permission():
    engine, services = engine_for()
    admin = Principal('admin-1', 'session-1', ('ADMIN',), False)
    with client_for(engine, services, ('inventory.read', 'inventory.export'), admin) as client:
        body = client.get(f'/api/v1/admin/inventory/batches/{UNASSIGNED_BATCH_ID}/cards'
                          f'?skuId={SKU_CODEX}').json()
    assert body['data'][0]['secret'].startswith('LC-CDK-SECRET')
    assert services.audit.records[-1]['target'] == UNASSIGNED_BATCH_ID


def test_batches_route_exposes_the_three_counts_and_the_pseudo_row():
    engine, services = engine_for()
    admin = Principal('admin-1', 'session-1', ('ADMIN',), False)
    with client_for(engine, services, ('inventory.read',), admin) as client:
        body = client.get(f'/api/v1/admin/inventory/batches?skuId={SKU_CODEX}').json()
    assert body['meta']['counts'] == {'registeredCards': 10, 'unassignedCards': 11, 'totalCards': 21}
    assert [row['id'] for row in body['data']] == [BATCH_CODEX, UNASSIGNED_BATCH_ID]

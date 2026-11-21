"""批次导出必须能被独角的导入真正解析。

结论来自对 `/srv/workspace/dujiao/dujiao-next` 的**只读核对**（未修改、未连接独角）：

* 手动录入：`CardSecretBatchCreateModal.vue` 把粘贴框按 `\\r?\\n` 切开，后端
  `application/import.go::normalizeSecrets` 逐行 `TrimSpace`、跳过空行、默认去重。
* CSV 导入：`application/import.go::parseCSVSecrets` 用 Go `encoding/csv`
  （`TrimLeadingSpace=true`）读；第一行若存在名为 `secret` 的列（对 BOM 容忍）就跳过；
  只取该列。
* 独角**自己的导出**是多列 CSV（`id,secret,status,...`），但它自己的导入只读 `secret`
  列 —— 所以本项目只输出单列 `secret`。

这里的两个解析器是上述 Go 逻辑的等价重写，用作「兼容性规约」：把 API 真正返回的
字符串喂进去，必须逐字还原出 `secrets`。它们不是 Go 代码的调用，也不证明独角
实例能导入；真实导入仍需人工在独角执行（见 docs/validation/2026-10-10-batch-cardkey-stop-zh-ui.md）。
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from smsinvit.common.cdk import mask_cdk, inventory_code_map
from smsinvit.common.inventory import Inventory, generate_cdk

BATCH_ID = '9a3e47ac-00d0-4000-8000-000000000001'
SECRETS = ['LC-CDK-AAAABBBBCCCC1111', 'LC-CDK-DDDDEEEEFFFF2222', 'LC-CDK-GGGGHHHHIIII3333']


# ──────────────────────── 独角解析规约（等价重写） ────────────────────────

def dujiao_parse_paste(value, deduplicate=True):
    """等价 `application/import.go::normalizeSecrets`。"""
    seen, out = set(), []
    for line in str(value).split('\n'):
        trimmed = line.strip()
        if not trimmed:
            continue
        if deduplicate:
            if trimmed in seen:
                continue
            seen.add(trimmed)
        out.append(trimmed)
    return out


def dujiao_parse_csv(text):
    """等价 `application/import.go::parseCSVSecrets`（TrimLeadingSpace + BOM 容忍 + 表头跳过）。"""
    reader = csv.reader(io.StringIO(str(text)))
    secrets, header_read, secret_idx = [], False, 0
    for record in reader:
        if len(record) == 0:
            continue
        if not header_read:
            header_read = True
            skip_row = False
            for index, column in enumerate(record):
                if column.strip().lstrip('\ufeff').lower() == 'secret':
                    secret_idx, skip_row = index, True
                    break
            if skip_row:
                continue
        if secret_idx >= len(record):
            continue
        secret = record[secret_idx].strip().lstrip('\ufeff')
        if not secret:
            continue
        secrets.append(secret)
    return secrets


# ──────────────────────── 假实现 ────────────────────────

class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCodec:
    """加解密对测试无意义，用可逆的恒等变换；`BROKEN` 用来模拟密文损坏。"""

    def decrypt(self, value):
        if not value or value == 'BROKEN':
            raise ValueError('cannot decrypt')
        return value


class FakeDb:
    def __init__(self, items):
        self.items = items

    def transaction(self, *args, **kwargs):
        return _Transaction()

    def all(self, sql, *params):
        wanted = {str(p) for p in params}
        return [
            {'id': item['id'], 'secretCiphertext': item['secretCiphertext']}
            for item in self.items
            if str(item['id']) in wanted
        ]


class RecordingAudit:
    def __init__(self):
        self.records = []

    def record(self, actor, action, resource, target, meta=None):
        self.records.append({'actor': actor, 'action': action, 'resource': resource,
                             'target': target, 'meta': meta})


class FakeMapper:
    def __init__(self, batch, items):
        self.batch, self.items, self.events = batch, items, []

    def findBatch(self, batch_id):
        return self.batch if str(batch_id) == str(self.batch['id']) else None

    def listForBatchExport(self, batch_id):
        return self.items

    def listBatchCards(self, batch_id, limit, offset):
        return self.items[offset:offset + limit]

    def countBatchCards(self, batch_id):
        return len(self.items)

    def insertEvent(self, *args):
        self.events.append(args)


def inventory_with(zone='Asia/Shanghai', items=None, created_at=None):
    items = items if items is not None else [
        {'id': f'card-{index}', 'status': 'ISSUED', 'issuedAt': '2026-10-10T01:35:24Z',
         'secretFingerprint': 'a' * 64, 'secretCiphertext': secret}
        for index, secret in enumerate(SECRETS)
    ]
    batch = {'id': BATCH_ID, 'createdAt': created_at or datetime(2026, 10, 10, 1, 35, 24),
             'note': '', 'source': 'GENERATE', 'requestedCount': len(items),
             'successCount': len(items), 'duplicateCount': 0, 'invalidCount': 0}
    services = SimpleNamespace(
        db=FakeDb(items),
        codec=FakeCodec(),
        config=SimpleNamespace(get=lambda key, default=None: (
            zone if key == 'runtime.business_time_zone' else default)),
        audit=RecordingAudit(),
    )
    engine = Inventory(services)
    engine.mapper = FakeMapper(batch, items)
    return engine, services


# ──────────────────────── 批次号 ────────────────────────

def test_batch_no_shape_and_uppercase_suffix():
    engine, _ = inventory_with()
    assert engine._batch_no(BATCH_ID, datetime(2026, 10, 10, 1, 35, 24)) == 'BATCH-20261010-9A3E47AC'


def test_batch_no_uses_business_time_zone_not_utc():
    """UTC 17:30 在上海已是次日 —— 批次号必须跟界面显示的日期同一天。"""
    engine, _ = inventory_with()
    assert engine._batch_no(BATCH_ID, datetime(2026, 10, 10, 17, 30)) == 'BATCH-20261011-9A3E47AC'
    # 带时区的输入等价。
    aware = datetime(2026, 10, 10, 17, 30, tzinfo=timezone.utc)
    assert engine._batch_no(BATCH_ID, aware) == 'BATCH-20261011-9A3E47AC'
    # ISO 字符串也接受。
    assert engine._batch_no(BATCH_ID, '2026-10-10T17:30:00Z') == 'BATCH-20261011-9A3E47AC'


def test_batch_no_is_stable_and_only_date_varies_with_zone():
    engine, _ = inventory_with()
    # UTC 02:00 → 上海 10-10 10:00，纽约 10-09 22:00：日期必须不同，后缀必须相同。
    moment = datetime(2026, 10, 10, 2, 0)
    other, _ = inventory_with(zone='America/New_York')
    shanghai = engine._batch_no(BATCH_ID, moment)
    new_york = other._batch_no(BATCH_ID, moment)
    assert shanghai == engine._batch_no(BATCH_ID, moment), '同一输入必须稳定'
    assert shanghai == 'BATCH-20261010-9A3E47AC'
    assert new_york == 'BATCH-20261009-9A3E47AC'
    assert shanghai[:6] == new_york[:6] == 'BATCH-'
    assert shanghai.split('-')[-1] == new_york.split('-')[-1], '后缀来自主键，不随时区变化'


def test_batch_no_never_raises_on_missing_or_invalid_input():
    engine, _ = inventory_with()
    for value in (None, '', 'not-a-date', 0):
        assert re.fullmatch(r'BATCH-\d{8}-9A3E47AC', engine._batch_no(BATCH_ID, value))


def test_batch_no_falls_back_when_zone_is_unknown():
    engine, _ = inventory_with(zone='Not/AZone')
    assert re.fullmatch(r'BATCH-\d{8}-9A3E47AC',
                        engine._batch_no(BATCH_ID, datetime(2026, 10, 10, 1, 0)))


def test_batch_no_survives_missing_batch_identifier():
    engine, _ = inventory_with()
    assert re.fullmatch(r'BATCH-\d{8}-', engine._batch_no(None, datetime(2026, 10, 10, 1, 0)))


# ──────────────────────── 导出与独角兼容 ────────────────────────

def test_export_text_is_paste_ready_for_dujiao():
    engine, services = inventory_with()
    result = engine.export(BATCH_ID, SimpleNamespace(user_id='admin-1'))
    assert result['secrets'] == SECRETS
    assert result['count'] == len(SECRETS)
    # 独角「批量录入」路径：逐行去空白后必须逐字还原。
    assert dujiao_parse_paste(result['text']) == SECRETS
    # 不留空行、不留尾随换行，避免独角产生空记录。
    assert '\n\n' not in result['text']
    assert not result['text'].endswith('\n')
    # 导出必须记审计。
    assert [row['action'] for row in services.audit.records] == ['inventory.batch_export']


def test_export_csv_is_importable_by_dujiao():
    engine, _ = inventory_with()
    result = engine.export(BATCH_ID, SimpleNamespace(user_id='admin-1'))
    assert result['csv'].startswith('secret\n'), '独角靠 secret 表头识别并跳过第一行'
    assert dujiao_parse_csv(result['csv']) == SECRETS
    # 前端会给下载文件加 BOM（Excel 需要），独角对 BOM 容忍，加了也必须能解析。
    assert dujiao_parse_csv('\ufeff' + result['csv']) == SECRETS
    assert result['filename'] == 'cdk-BATCH-20261010-9A3E47AC.csv'


def test_export_does_not_use_dujiao_multi_column_shape():
    """独角自己的导出是多列，但它自己的导入只读 secret 列 —— 单列才可导入。"""
    engine, _ = inventory_with()
    csv_text = engine.export(BATCH_ID, SimpleNamespace(user_id='admin-1'))['csv']
    header = csv_text.splitlines()[0]
    assert header == 'secret'
    assert header.count(',') == 0


def test_generated_cdk_needs_no_csv_escaping():
    """字母表不含逗号/引号/空白/控制字符，所以单列 CSV 无需转义。"""
    for _ in range(200):
        value = generate_cdk(20)
        assert not set(value) & set(',;"\r\n\t ')
        assert value.startswith('LC-CDK-')
        assert len(value) == len('LC-CDK-') + 20


def test_missing_batch_is_reported_not_exported():
    from smsinvit.errors import Error

    engine, _ = inventory_with()
    with pytest.raises(Error) as error:
        engine.export('00000000-0000-4000-8000-000000000000', SimpleNamespace(user_id='admin-1'))
    assert error.value.code == 'INVENTORY_BATCH_NOT_FOUND'


# ──────────────────────── 脱敏与权限 ────────────────────────

def test_mask_cdk_boundaries():
    assert mask_cdk('LC-CDK-AAAABBBBCCCC1111') == 'LC-C…1111'
    assert mask_cdk('ABC') == 'AB…BC'
    assert mask_cdk('') == '—'
    assert mask_cdk(None) == '—'
    assert mask_cdk('   ') == '—'
    # 掩码不得泄露中段。
    masked = mask_cdk('LC-CDK-AAAABBBBCCCC1111')
    assert 'AAAABBBB' not in masked


def test_inventory_code_map_hides_plaintext_without_reveal():
    ids = [f'card-{index}' for index in range(len(SECRETS))]
    services = SimpleNamespace(db=FakeDb([
        {'id': item_id, 'secretCiphertext': secret} for item_id, secret in zip(ids, SECRETS)
    ]), codec=FakeCodec())
    masked = inventory_code_map(services.db, services.codec, ids, reveal=False)
    assert set(masked) == set(ids)
    for item_id, secret in zip(ids, SECRETS):
        assert masked[item_id]['codeMasked'] == mask_cdk(secret)
        assert 'code' not in masked[item_id], '无 inventory.export 时明文不得进入返回值'
        assert secret not in str(masked[item_id])


def test_inventory_code_map_reveals_only_when_asked_and_tolerates_broken_rows():
    ids = ['card-0', 'card-1']
    services = SimpleNamespace(db=FakeDb([
        {'id': 'card-0', 'secretCiphertext': SECRETS[0]},
        {'id': 'card-1', 'secretCiphertext': 'BROKEN'},
    ]), codec=FakeCodec())
    revealed = inventory_code_map(services.db, services.codec, ids, reveal=True)
    assert revealed['card-0']['code'] == SECRETS[0]
    # 密文损坏时退化为不可显示，而不是让整个列表 500。
    assert revealed['card-1']['code'] is None
    assert revealed['card-1']['codeMasked'] == '—'


def test_inventory_code_map_deduplicates_and_handles_empty_input():
    services = SimpleNamespace(db=FakeDb([
        {'id': 'card-0', 'secretCiphertext': SECRETS[0]},
    ]), codec=FakeCodec())
    assert inventory_code_map(services.db, services.codec, [], reveal=True) == {}
    assert inventory_code_map(services.db, services.codec, None, reveal=True) == {}
    assert set(inventory_code_map(services.db, services.codec,
                                  ['card-0', 'card-0', '  '], reveal=True)) == {'card-0'}


def test_batch_cards_endpoint_hides_secret_without_export_permission():
    engine, services = inventory_with()
    actor = SimpleNamespace(user_id='admin-1')
    expected = {f'card-{index}': mask_cdk(secret) for index, secret in enumerate(SECRETS)}

    masked = engine.cards(BATCH_ID, 1, 20, actor, False)
    assert masked['meta']['total'] == len(SECRETS)
    for item in masked['data']:
        assert 'secret' not in item, '无 inventory.export 时列表不得含明文字段'
        assert item['secretMasked'] == expected[item['id']]
        assert item['secretMasked'] != SECRETS[int(item['id'].split('-')[1])]
    assert services.audit.records == [], '脱敏读取不记明文审计'

    revealed = engine.cards(BATCH_ID, 1, 20, actor, True)
    assert [item['secret'] for item in revealed['data']] == SECRETS
    assert [row['action'] for row in services.audit.records] == ['inventory.batch_reveal']
    assert services.audit.records[0]['meta'] == {'count': len(SECRETS)}


# ──────────────────────── 批次号必须只有一套算法 ────────────────────────

def test_batch_number_algorithm_has_a_single_source():
    """库存页与邀请卡密页必须派生出同一个字符串，否则运营无法对账。

    这条断言挡住「某一边又自己写一份算法」的回归。
    """
    from smsinvit.common.inventory import Inventory
    from smsinvit.common.cdk import batch_no as shared

    engine, _ = inventory_with()
    moment = datetime(2026, 10, 5, 13, 12, 56)
    assert engine._batch_no(BATCH_ID, moment) == shared(BATCH_ID, moment, 'Asia/Shanghai')
    assert engine._batch_no(BATCH_ID, moment) == 'BATCH-20261005-9A3E47AC'


def test_invitation_card_records_the_same_batch_number_as_the_ledger():
    """邀请卡密表里的 batch 必须是派生批次号，不是库存批次的 UUID。"""
    from smsinvit.invitation.service import InvitationService

    class FakeDb:
        def __init__(self, row):
            self.row = row

        def one(self, sql, *params):
            return self.row

    batch_uuid = 'c5388f7f-e836-49d8-b267-b267a0906a15'
    harness = SimpleNamespace(
        db=FakeDb({'id': batch_uuid, 'createdAt': datetime(2026, 10, 5, 13, 12, 56)}),
        s=SimpleNamespace(config=SimpleNamespace(get=lambda key, default=None: default)),
    )
    assert InvitationService._inventory_batch_no(harness, 'item-1') == 'BATCH-20261005-C5388F7F'
    assert batch_uuid not in InvitationService._inventory_batch_no(harness, 'item-1')

    # 没有库存批次的卡（历史导入）返回空串，不猜一个批次出来。
    harness.db.row = None
    assert InvitationService._inventory_batch_no(harness, 'item-1') == ''

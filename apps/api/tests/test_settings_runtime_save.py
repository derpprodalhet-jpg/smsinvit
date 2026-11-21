"""运行时配置保存的回归测试。

F03：`Settings.save()` 的运行时分支曾读取未定义的局部变量 `secret`（该变量只存在于
`Settings.list()`）。因为 `NameError` 在 `serialized=...` 一行必然触发，**任何**运行时
配置的保存都会失败，包括后台强制两步验证这类安全开关。

本文件不连数据库：用替身记录 SQL 写入与审计调用，但校验、序列化、加密封装走的都是
真实实现——这样"保存是否成功""非法值是否被拒""明文是否被加密"都是真结论。
"""
from __future__ import annotations
from contextlib import contextmanager

import pytest

from smsinvit.common.settings import Settings
from smsinvit.config import RUNTIME_CATALOG
from smsinvit.errors import Error


class FakeDB:
    def __init__(self):
        self.statements = []

    @contextmanager
    def transaction(self, requires_new=False):
        yield self

    @contextmanager
    def advisory_lock(self, name, timeout=0):
        yield True

    def execute(self, sql, *params):
        self.statements.append((sql, params))
        return type('Result', (), {'rowcount': 1})()


class FakeMapper:
    """只实现 Settings 用到的 SettingsMapper 方法，并记录调用。"""

    def __init__(self, db):
        self.db = db
        self.upserted = []
        self.secret_upserts = 0
        self.audits = []

    def listAll(self):
        return []

    def upsertRuntime(self, *args):
        self.upserted.append(args)
        return 1

    def upsertSecret(self, *args):
        self.secret_upserts += 1
        return 1

    def existsSecret(self, key):
        return False


class FakeCodec:
    def encrypt(self, value):
        return 'encrypted:' + str(value)


class FakeAudit:
    def __init__(self, db):
        self.db = db
        self.records = []

    def record(self, actor, action, resource_type, resource_id, safe_facts=None):
        self.records.append({'action': action, 'key': resource_id, 'facts': safe_facts or {}})
        return 1


class FakeNotifications:
    def supports(self, key):
        return key.startswith('notify.')

    def admin_values(self):
        return []

    def save(self, key, value, actor):
        raise AssertionError('通知通道不应在运行时配置用例中被调用')


class FakeConfig:
    def __init__(self):
        self.runtime = {}

    def get(self, key, default=None):
        # 生产 Config.get 会先看运行时覆盖；替身必须一致，否则 list()/flag()/int()
        # 永远读回默认值，回读断言就失去意义。
        override = self.runtime.get(key)
        return default if override is None else override

    def flag(self, key, default=False):
        value = self.get(key, default)
        return str(value).lower() in ('true', '1', 'yes', 'on')

    def int(self, key, default=0):
        value = self.get(key, default)
        return default if value is None else int(value)


class Services:
    def __init__(self):
        self.db = FakeDB()
        self.mapper = FakeMapper(self.db)
        self.codec = FakeCodec()
        self.audit = FakeAudit(self.db)
        self.notifications = FakeNotifications()
        self.config = FakeConfig()


class Actor:
    user_id = 'admin-1'
    id = 'admin-1'


def settings():
    services = Services()
    service = Settings(services)
    service.mapper = services.mapper
    return service, services


def boolean_spec():
    return next(spec for spec in RUNTIME_CATALOG if spec['type'] == 'BOOLEAN')


def integer_spec():
    return next(spec for spec in RUNTIME_CATALOG if spec['type'] == 'INTEGER')


def text_spec():
    return next(spec for spec in RUNTIME_CATALOG if spec['type'] == 'TEXT' and not spec['allowBlank'])


@pytest.mark.parametrize('spec,value', [
    (boolean_spec(), True),
    (boolean_spec(), False),
    (integer_spec(), None),  # 用规格内的合法值，见下方测试体
    (text_spec(), None),
])
def test_runtime_save_never_raises_name_error(spec, value):
    """回归核心：保存任意类型的运行时配置都必须成功，而不是抛 NameError。"""
    service, services = settings()
    if spec['type'] == 'INTEGER':
        value = spec['min'] if spec['min'] is not None else int(spec['initialValue'])
    elif spec['type'] == 'TEXT':
        value = 'Asia/Shanghai' if spec['key'] == 'runtime.business_time_zone' else 'https://example.test'
    body = {'key': spec['key'], 'valueJson': {'value': value}}
    row = service.save(Actor(), body)
    assert row['key'] == spec['key']
    assert services.mapper.upserted, '运行时配置必须走 upsertRuntime 写入'
    assert services.mapper.secret_upserts == 0, '运行时目录没有密钥项，不得走密钥写入路径'


def test_boolean_roundtrips_through_config_runtime():
    """保存后的值必须落进 config.runtime，list() 才能回读出同一个值。"""
    service, services = settings()
    spec = boolean_spec()
    service.save(Actor(), {'key': spec['key'], 'valueJson': {'value': True}})
    assert services.config.runtime[spec['key']] == 'true'
    # list() 对 BOOLEAN 走 config.flag()，所以是真正的布尔值。
    services.config.runtime[spec['key']] = 'true'
    row = next(r for r in service.list() if r['key'] == spec['key'])
    assert row['valueJson']['value'] is True
    assert row['isSecret'] is False


def test_integer_roundtrips_through_config_runtime():
    service, services = settings()
    spec = integer_spec()
    value = int(spec['min']) + 1 if spec['min'] is not None else int(spec['initialValue'])
    service.save(Actor(), {'key': spec['key'], 'valueJson': {'value': value}})
    assert services.config.runtime[spec['key']] == str(value)
    services.config.runtime[spec['key']] = str(value)
    row = next(r for r in service.list() if r['key'] == spec['key'])
    assert row['valueJson']['value'] == value


def test_runtime_plaintext_is_stored_without_encryption():
    """运行时配置均为非密钥，写入的 JSON 里不应出现加密前缀。"""
    service, services = settings()
    spec = boolean_spec()
    service.save(Actor(), {'key': spec['key'], 'valueJson': {'value': True}})
    # upsertRuntime(id, key, category, valueJson, updatedBy)
    args = services.mapper.upserted[0]
    assert args[1] == spec['key']
    assert 'encrypted:' not in args[3]


def test_invalid_runtime_value_is_rejected_with_parameter_error():
    """非法值必须是明确的参数错误，而不是 NameError 或未处理异常。"""
    service, _ = settings()
    spec = integer_spec()
    body = {'key': spec['key'], 'valueJson': {'value': 'not-a-number'}}
    with pytest.raises(Error) as error:
        service.save(Actor(), body)
    assert error.value.status == 422


def test_out_of_range_integer_is_rejected():
    service, _ = settings()
    spec = integer_spec()
    ceiling = (spec['max'] if spec['max'] is not None else 100) + 1
    with pytest.raises(Error):
        service.save(Actor(), {'key': spec['key'], 'valueJson': {'value': ceiling}})


def test_missing_value_field_is_rejected():
    service, _ = settings()
    spec = boolean_spec()
    with pytest.raises(Error):
        service.save(Actor(), {'key': spec['key'], 'valueJson': {}})


def test_secret_flag_is_refused_for_runtime_keys():
    service, _ = settings()
    spec = boolean_spec()
    with pytest.raises(Error):
        service.save(Actor(), {'key': spec['key'], 'valueJson': {'value': True}, 'isSecret': True})


def test_runtime_save_writes_audit_and_is_not_partial():
    """审计失败时不得留下已写入的配置：写入与审计都发生在同一个事务里。"""
    service, services = settings()
    spec = boolean_spec()
    service.save(Actor(), {'key': spec['key'], 'valueJson': {'value': False}})
    assert [record['action'] for record in services.audit.records] == ['setting.update']
    assert services.audit.records[0]['facts']['runtime'] is True

    def explode(*args):
        raise RuntimeError('audit down')

    services.audit.record = explode
    before = len(services.mapper.upserted)
    with pytest.raises(RuntimeError):
        service.save(Actor(), {'key': spec['key'], 'valueJson': {'value': True}})
    assert len(services.mapper.upserted) == before + 1, '写入发生在审计之前，失败必须由事务回滚'
    assert services.config.runtime[spec['key']] == 'false', '保存失败不得更新内存中的运行时值'


def test_unknown_key_keeps_existing_error_message():
    """未知键仍走原有分支，不能被本次修复改成 NameError。"""
    service, _ = settings()
    with pytest.raises(Error) as error:
        service.save(Actor(), {'key': 'runtime.not_in_catalog', 'valueJson': {'value': 'x'}})
    assert error.value.status == 422

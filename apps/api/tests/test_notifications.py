"""通知通道收敛：只保留 Telegram，且「卡密核销完成」事件按 event_key 幂等。

这些用例不需要数据库：`Notifications.enqueue` 的幂等契约由
notification_outbox.event_key 的唯一约束（ON DUPLICATE KEY UPDATE）提供，
这里用一个同样去重 event_key 的内存表复现那条契约。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from smsinvit.common.notifications import CARD_REDEEMED_KIND, Notifications, card_redeemed_body
from smsinvit.errors import Error


class Codec:
    def __init__(self): self.secrets = {}
    def encrypt(self, value): return 'enc:' + str(value)
    def decrypt(self, value): return str(value).removeprefix('enc:')


class Memory:
    """Shared outbox table with a UNIQUE(event_key) exactly like notification_outbox."""
    def __init__(self): self.rows = {}


class ChannelMappers:
    def findEncrypted(self, key): return None
    def findLegacyValueJson(self, key): return None
    def legacyExists(self, key): return False
    def insertIgnore(self, *args): return None
    def deleteLegacy(self, *args): return None
    def upsert(self, *args): return None


class Mapper:
    def __init__(self, db, name):
        self.db, self.name = db, name
    def enqueue(self, id, eventKey, channel, kind, referenceId, payloadEncrypted, expiresAt):
        # INSERT ... ON DUPLICATE KEY UPDATE `event_key`=`event_key`
        self.db.rows.setdefault(eventKey, (id, eventKey, channel, kind, referenceId, payloadEncrypted, expiresAt))
        return 1
    def findIdByEventKey(self, eventKey):
        row = self.db.rows.get(eventKey)
        return row[0] if row else None
    def page(self, *args): return []
    def count(self, *args): return 0
    def findById(self, id): return None


class Transaction:
    """Minimal transaction surface: notifications.enqueue requires an active one."""
    def __init__(self):
        self.in_transaction = False
        self.rows = Memory().rows
    def transaction(self, requires_new=False):
        db = self
        class Scope:
            def __enter__(self):
                db.in_transaction = True
                return db
            def __exit__(self, *exc):
                db.in_transaction = False
                return False
        return Scope()


def notifications(config=None):
    db = Transaction()
    services = SimpleNamespace(db=db, codec=Codec(), config=config or SimpleNamespace(get=lambda key, default=None: default))
    service = Notifications(services)
    service.channels = ChannelMappers()
    service.mapper = Mapper(db, 'NotificationOutboxMapper')
    service.db = db
    service.table = db
    return service


def test_notification_keys_are_only_telegram():
    service = notifications()
    assert service.supports('notify.telegram_bot_token') and service.supports('notify.telegram_ops_chat_id')
    # SMTP/邮件键随邮件通道一起删除，写入接口必须拒绝它们。
    for key in ('notify.email_enabled','notify.smtp_host','notify.smtp_port','notify.smtp_username',
                'notify.smtp_password','notify.smtp_from','notify.smtp_from_name','notify.smtp_secure'):
        assert service.supports(key) is False
        with pytest.raises(Error): service.save(key, {'value': 'x'}, SimpleNamespace(user_id='a'))


def test_card_redeemed_event_is_idempotent_on_its_event_key():
    service = notifications()
    payload = {'chat': '-100123', 'body': card_redeemed_body('接码', 'B-1', 'abcdef012345', 'kid-1', [('服务/国家', 'ot / 0')])}
    with service.db.transaction():
        first = service.enqueue('sms.card-redeemed:kid-1', 'TELEGRAM', CARD_REDEEMED_KIND, 'kid-1', payload, None)
        second = service.enqueue('sms.card-redeemed:kid-1', 'TELEGRAM', CARD_REDEEMED_KIND, 'kid-1', payload, None)
    assert first == second and len(service.table.rows) == 1
    row = service.table.rows['sms.card-redeemed:kid-1']
    assert row[2] == 'TELEGRAM' and row[3] == CARD_REDEEMED_KIND and row[6] is None


def test_different_products_keep_separate_event_keys():
    service = notifications()
    with service.db.transaction():
        service.enqueue('sms.card-redeemed:kid-1', 'TELEGRAM', CARD_REDEEMED_KIND, 'kid-1', {'chat': '-1', 'body': 'a'}, None)
        service.enqueue('invitation.card-redeemed:kid-1', 'TELEGRAM', CARD_REDEEMED_KIND, 'kid-1', {'chat': '-1', 'body': 'b'}, None)
    assert len(service.table.rows) == 2


def test_enqueue_outside_a_transaction_is_rejected():
    service = notifications()
    with pytest.raises(RuntimeError):
        service.enqueue('sms.card-redeemed:kid-2', 'TELEGRAM', CARD_REDEEMED_KIND, 'kid-2', {'chat': '-1', 'body': 'a'}, None)


def test_telegram_failure_turns_provider_reason_into_actionable_code():
    """Telegram 的 description 必须变成可辨识的错误码 + 中文处理建议。

    起因：群里 Chat ID 填错时 Telegram 返回 400「chat not found」，但代码一律抛
    TELEGRAM_REJECTED「请检查通道」，运营只能看到一句没有方向的话。
    """
    from smsinvit.common.notifications import telegram_failure

    code, message = telegram_failure({'description': 'Bad Request: chat not found'})
    assert code == 'TELEGRAM_CHAT_NOT_FOUND'
    assert '拉进' in message and 'chat not found' in message, '要保留 Telegram 原话'

    for description, expected in [('Forbidden: bot was blocked by the user', 'TELEGRAM_BOT_BLOCKED'),
                                  ('Forbidden: bot is not a member of the channel chat', 'TELEGRAM_BOT_NOT_MEMBER'),
                                  ('Forbidden: not enough rights to send text messages', 'TELEGRAM_NO_RIGHTS'),
                                  ('Unauthorized', 'TELEGRAM_TOKEN_INVALID'),
                                  ('Bad Request: message is too long', 'TELEGRAM_MESSAGE_TOO_LONG')]:
        assert telegram_failure({'description': description})[0] == expected, description

    # 认不出来的原因也要保留原话，但不能凭空造码
    code, message = telegram_failure({'description': 'Bad Request: something new'})
    assert code == 'TELEGRAM_REJECTED' and 'something new' in message
    assert telegram_failure({}) == ('TELEGRAM_REJECTED', 'Telegram 拒绝发送，请检查通道。')

"""Write-before-create receipts and capability-protected, read-only recovery."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from uuid import UUID, uuid4
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as PoolTimeout

from ..errors import Error, bad
from ..common.notifications import CARD_REDEEMED_KIND,card_redeemed_body
from . import mysql

log = logging.getLogger(__name__)

PUBLIC_REDEMPTION_ERRORS = {
    'INVITATION_CDK_NOT_FOUND': '卡密无效或不存在，请检查后重试。',
    'INVITATION_CDK_UNAVAILABLE': '该卡密暂不可用，请联系客服。',
    'INVITATION_CDK_DISABLED': '该卡密已停用，请联系客服。',
    'INVITATION_CDK_EXPIRED': '该卡密已过期，请联系客服。',
    'INVITATION_BINDING_UNAVAILABLE': '原领取记录暂时无法读取，请稍后重试。',
    'INVITATION_BINDING_CHANGED': '领取记录发生变化，请重新查询。',
    'INVITATION_REVIEW_REQUIRED': '结果待确认，请联系客服，勿重复领取。',
    'INVITATION_REDEMPTION_BUSY': '领取结果正在确认，请查询原请求，勿重复提交。',
    'INVITATION_REDEMPTION_REVIEW': '领取结果待核查，请联系客服，勿重复领取。',
    'INVITATION_POOL_INSUFFICIENT': '可用账号不足，请减少数量或稍后重试。',
    'INVITATION_MAIL_INSUFFICIENT': '可用账号不足，请减少数量或稍后重试。',
    'INVITATION_MAIL_CONFIGURATION': '服务暂时不可用，请稍后重试。',
    'INVITATION_MAIL_CONNECTION': '服务暂时不可用，请稍后重试。',
    'INVITATION_MAIL_UNAVAILABLE': '服务暂时不可用，请稍后重试。',
    'VALIDATION_ERROR': '请检查填写的信息后重试。',
}

PUBLIC_ACTION_ERRORS = {
    **PUBLIC_REDEMPTION_ERRORS,
    'SESSION_SECRET_REQUIRED': '缺少领取凭证，请使用原卡密恢复记录。',
    'SESSION_SECRET_INVALID': '领取凭证无效，请使用原卡密恢复记录。',
    'SESSION_SUPERSEDED': '领取记录已更新，请使用原卡密恢复记录。',
    'INVITATION_SESSION_NOT_FOUND': '领取记录不存在，请使用原卡密恢复记录。',
    'INVITATION_ATTEMPT_NOT_FOUND': '未找到可恢复的领取请求。',
    'INVITATION_ATTEMPT_SECRET_INVALID': '领取恢复凭证无效。',
    'INVITATION_ATTEMPT_CONFLICT': '该请求对应另一批卡密，请查询原请求。',
    'INVITATION_BUSY': '当前繁忙，请稍后重试。',
    'INVITATION_START_DENIED': '当前状态不能开始，请刷新记录后重试。',
    'INVITATION_INVALID_STATE': '当前状态无法进行此操作，请刷新记录后重试。',
}


def public_invitation_error(error):
    code = error.code
    if code not in PUBLIC_ACTION_ERRORS or code in {'INVITATION_MAIL_CONFIGURATION', 'INVITATION_MAIL_CONNECTION', 'INVITATION_MAIL_UNAVAILABLE'}:
        return Error('SERVICE_UNAVAILABLE', '服务暂时不可用，请稍后重试。', 503)
    return Error(code, PUBLIC_ACTION_ERRORS[code], error.status)


class RedemptionLockLost(RuntimeError):
    pass


class RedemptionLocks:
    """Named locks live on one connection, outside business transactions."""
    def __init__(self, connection):
        self.connection = connection
        self.names = []
        self.owner = connection.exec_driver_sql('SELECT CONNECTION_ID()').scalar()

    def acquire(self, name):
        if self.connection.exec_driver_sql('SELECT GET_LOCK(%s,0)', (name,)).scalar() != 1:
            return False
        self.names.append(name)
        return True

    def require_owned(self):
        try:
            if self.connection.exec_driver_sql('SELECT CONNECTION_ID()').scalar() != self.owner:
                raise RedemptionLockLost()
            for name in self.names:
                if self.connection.exec_driver_sql('SELECT IS_USED_LOCK(%s)', (name,)).scalar() != self.owner:
                    raise RedemptionLockLost()
        except Exception as exc:
            raise RedemptionLockLost('领取锁已失效，结果需要复核。') from exc

    def close(self):
        for name in reversed(self.names):
            try:
                self.connection.exec_driver_sql('SELECT RELEASE_LOCK(%s)', (name,))
            except Exception:
                log.warning('Redemption lock connection unavailable during release')


class RedemptionAttemptMixin:
    def _redemption_fingerprint(self, domain, value):
        return hmac.new(self.s.codec.fingerprint_key, (domain + ':' + value).encode(), hashlib.sha256).hexdigest()

    def _attempt_lock_name(self, attempt_id):
        return self._redemption_fingerprint('invitation:redemption:attempt:v1', attempt_id)

    def _card_lock_name(self, code):
        return self._redemption_fingerprint('invitation:redemption:card:v1', code)

    def _recovery_fingerprint(self, secret):
        return self._redemption_fingerprint('invitation:redemption:recovery:v1', secret)

    @contextmanager
    def _redemption_locks(self):
        # Holding a named-lock connection while waiting for a second business
        # connection must never exhaust the business pool under concurrency.
        with self._mutex:
            engine = getattr(self, '_redemption_lock_engine', None)
            if engine is None:
                # Reuse the actual configured DBAPI creator, including TLS.
                # Services may receive an explicit engine independent of Config.
                engine = create_engine(self.db.engine.url, creator=self.db.engine.pool._creator,
                                       pool_size=8, max_overflow=0, pool_timeout=2,
                                       isolation_level='AUTOCOMMIT', hide_parameters=True)
                self._redemption_lock_engine = engine
        try:
            connection = engine.connect()
        except PoolTimeout:
            raise Error('INVITATION_REDEMPTION_BUSY', '邮箱领取当前繁忙，请查询原请求结果或稍后重试。', 409) from None
        with connection:
            locks = RedemptionLocks(connection)
            try:
                yield locks
            finally:
                locks.close()

    def _attempt_identity(self, attempt_id, recovery_secret):
        if attempt_id is None and recovery_secret is None:
            return str(uuid4()), secrets.token_hex(32)
        try:
            canonical = str(UUID(str(attempt_id)))
        except (ValueError, TypeError, AttributeError):
            raise bad('领取请求标识不合法。')
        if not isinstance(recovery_secret, str) or not re.fullmatch('[0-9a-fA-F]{64}', recovery_secret):
            raise bad('领取恢复凭证不合法。')
        return canonical, recovery_secret.lower()

    def _submitted_codes(self, codes):
        self._codes(codes)  # Keep the legacy size/type/marker validation.
        unique = {}
        for line, code in enumerate(codes, 1):
            canonical = code.strip().upper()
            if canonical:
                unique.setdefault(canonical, line)
        if not unique:
            raise bad('请输入至少一个卡密。')
        return list(unique), list(unique.values())

    def _attempt_row(self, attempt_id, *, lock=False):
        return self.db.one('SELECT * FROM invitation_v2_redemption_attempts WHERE attempt_id=?'
                           + (' FOR UPDATE' if lock else ''), attempt_id)

    def _require_attempt_proof(self, row, recovery_fingerprint, codes_fingerprint=None):
        if not hmac.compare_digest(row['recovery_fingerprint'], recovery_fingerprint):
            raise Error('INVITATION_ATTEMPT_SECRET_INVALID', '领取恢复凭证无效。', 401)
        if codes_fingerprint is not None and not hmac.compare_digest(row['codes_fingerprint'], codes_fingerprint):
            raise Error('INVITATION_ATTEMPT_CONFLICT', '该领取请求对应另一批卡密，请使用原请求查询结果。', 409)

    def _save_attempt(self, attempt_id, recovery_fingerprint, codes_fingerprint, status, *, cards=(), owner=None,
                      result=None, error=None):
        now = time.time()
        with self.db.transaction():
            self.db.execute('INSERT INTO invitation_v2_redemption_attempts'
                            '(attempt_id,recovery_fingerprint,codes_fingerprint,status,owner_token,created_at,updated_at,result_json,error_code,error_message) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?)', attempt_id, recovery_fingerprint, codes_fingerprint, status,
                            owner, now, now, json.dumps(result, ensure_ascii=False) if result is not None else None,
                            error.code if error else None, error.message if error else None)
            for fingerprint in cards:
                self.db.execute('INSERT INTO invitation_v2_redemption_attempt_cards(attempt_id,code_fingerprint) VALUES (?,?)',
                                attempt_id, fingerprint)

    def _finish_attempt(self, attempt_id, status, *, result=None, error=None):
        self.db.execute('UPDATE invitation_v2_redemption_attempts SET status=?,owner_token=NULL,updated_at=?,result_json=?,error_code=?,error_message=? '
                        "WHERE attempt_id=? AND status='pending'", status, time.time(),
                        json.dumps(result, ensure_ascii=False) if result is not None else None,
                        error.code if error else None, error.message if error else None, attempt_id)

    def _attempt_response(self, row, *, derived_status=None):
        status = derived_status or row['status']
        stored = row.get('result_json') or {}
        if isinstance(stored, str):
            stored = json.loads(stored)
        items = []
        if status == 'completed':
            for entry in stored.get('sessions', []):
                session = self.db.one('SELECT code_id FROM invitation_v2_sessions WHERE id=?', entry['id'])
                if not session:
                    items.append({'line': entry['line'], 'id': entry['id'], 'error': {
                        'code': 'INVITATION_SESSION_NOT_FOUND', 'message': '记录无法读取，请重新输入卡密。'}})
                    continue
                items.append({'line': entry['line'], **self._view(entry['id'], mysql.revealed(session['code_id'])),
                              'sessionSecret': self.s.tokens.issue('invocation-session', entry['id'])})
        elif status == 'rejected':
            # Older receipts may contain service diagnostics. Rebuild the public
            # shape instead of replaying stored JSON directly to the browser.
            for entry in stored.get('items', []):
                if not isinstance(entry, dict):
                    continue
                error = entry.get('error') if isinstance(entry.get('error'), dict) else {}
                code = error.get('code')
                if code not in PUBLIC_REDEMPTION_ERRORS:
                    code = 'INVITATION_REDEMPTION_REVIEW'
                public = public_invitation_error(Error(code, '', 400))
                item = {'error': {'code': public.code, 'message': public.message}}
                if type(entry.get('line')) is int and entry['line'] > 0:
                    item['line'] = entry['line']
                items.append(item)
        messages = {'pending': '正在获取邮箱，请稍候。',
                    'review': '领取结果待核查，请稍后再试。',
                    'rejected': '本次领取未完成，卡密未被本次请求扣减。'}
        page = self.page_content()
        result = {'attemptId': row['attempt_id'], 'attemptStatus': status,
                  'attemptCreatedAt': int(row['created_at'] * 1000), 'items': items,
                  **{key: page[key] for key in ('canRedeem', 'canStart', 'unavailableMessage')}}
        # 可兑换账号数：用户页的"当前可用账号"标签，也是"池不足要不要继续"的判据。
        result['poolAvailable'] = page.get('poolAvailable', -1)
        requested = stored.get('poolRequested')
        if isinstance(requested, int) and requested > 0:
            result['poolRequested'] = requested
        if status in messages:
            result['message'] = messages[status]
        return result

    def redemption_attempt_status(self, attempt_id, recovery_secret):
        # Unknown identifiers and wrong proofs share one response. This method
        # only reads; a lost owner's pending receipt is derived as review.
        try:
            canonical, secret = self._attempt_identity(attempt_id, recovery_secret)
        except Error:
            raise Error('INVITATION_ATTEMPT_NOT_FOUND', '未找到可恢复的领取请求。', 404)
        row = self._attempt_row(canonical)
        if not row or not hmac.compare_digest(row['recovery_fingerprint'], self._recovery_fingerprint(secret)):
            raise Error('INVITATION_ATTEMPT_NOT_FOUND', '未找到可恢复的领取请求。', 404)
        status = row['status']
        if status == 'pending':
            owner = self.db.scalar('SELECT IS_USED_LOCK(?)', self._attempt_lock_name(canonical))
            if owner is None or not row.get('owner_token'):
                # The owner may have committed completion and released its lock
                # between our receipt read and lock probe. Read the live receipt
                # again before deriving an unresolved result.
                latest = self._attempt_row(canonical)
                if latest:
                    row = latest
                status = 'review' if row['status'] == 'pending' else row['status']
        return self._attempt_response(row, derived_status=status)

    def _mailbox_readiness(self, settings=None):
        """号池里有没有可兑换的号。

        拆分后号池可以混放多种来源（补号来源与兑换来源各自可配），每个号按自己
        登记时的来源取信，所以这里只问"池里有没有已注册 GPT、尚未 Codex 授权、
        也没被占用的号"；不再按当前配置的来源做池化/非池化判断。
        """
        if self.alloc.pool_available(settings) > 0:
            return True, ''
        return False, '可用账号暂时不足，请稍后重试。'

    def _public_redemption_message(self, message):
        if re.search('部署|执行器|会话|凭据|管理员|复核', str(message)):
            return '记录暂时无法读取，请稍后重试。'
        return message

    def notify_card_redeemed(self, code):
        """Only the repository's committed done transition may publish success.

        The caller owns the completion transaction. A rollback removes both the
        done write and its outbox row; the event key deduplicates repeated sync.
        """
        outbox = getattr(self.s, 'outbox', None)
        if not outbox:return
        if not self.db.in_transaction:
            raise RuntimeError('Invitation completion notification requires the done transaction')
        row = self.repo.get_cdk_code(code) or {}
        if row.get('status') != 'done':return
        chat = outbox.value('telegram_ops_chat_id')
        if not chat:return
        code_id = mysql.marker(code)
        body = card_redeemed_body('Codex 邀请', row.get('batch'), None, code_id,
                                  [('邮箱', row.get('email') or ''), ('邮箱来源', row.get('mail_kind'))])
        outbox.enqueue('invitation.card-redeemed:' + code_id, 'TELEGRAM', CARD_REDEEMED_KIND,
                       None, {'chat': chat, 'body': body}, None)

    def _redemption_cards(self, codes, lines):
        errors, validated = [], {}
        for code, line in zip(codes, lines):
            row = self.repo.get_cdk_code(code)
            try:
                if not row:
                    raise Error('INVITATION_CDK_NOT_FOUND', '未找到该卡密。', 404)
                if row['status'] not in {'unused','bound','queued','running','done','failed','review'}:
                    raise Error('INVITATION_CDK_UNAVAILABLE', '该卡密暂不可用。', 409)
                marker = mysql.marker(code)
                self._require_inventory_available(marker, row['status'] == 'unused')
                if row['status'] == 'unused':
                    self._require_no_holds(marker)
                else:
                    # The CDK is the recovery authority. An email or session ID
                    # alone can never establish this binding or mint a capability.
                    email = str(row.get('email') or '').strip()
                    binding = self.db.one('SELECT cdk_code FROM invitation_v2_outlook_accounts WHERE email=?', email) if email else None
                    if not binding or binding.get('cdk_code') != marker:
                        raise Error('INVITATION_BINDING_UNAVAILABLE', '原邮箱记录暂时无法读取，请稍后重试。', 409)
                validated[code] = row
            except Error as exc:
                errors.append({'line': line, 'error': {'code': exc.code, 'message': self._public_redemption_message(exc.message)}})
        return errors, validated

    def _redemption_failure(self, exc, *, remote_started=False, remote_created=False):
        if remote_created:
            return 'review', Error('INVITATION_REDEMPTION_REVIEW', '领取结果待核查，请稍后再试。', 409)
        current, seen = exc, set()
        from mail_providers.cf_temp import MailboxCreationUncertainError, MailboxConfigurationError, MailboxConnectionError
        while current is not None and id(current) not in seen:
            if isinstance(current, (MailboxCreationUncertainError, RedemptionLockLost)):
                return 'review', Error('INVITATION_REDEMPTION_REVIEW', '领取结果待核查，请稍后再试。', 409)
            if isinstance(current, MailboxConfigurationError):
                return 'rejected', Error('INVITATION_MAIL_CONFIGURATION', '邮箱暂不可用，请稍后重试。', 503)
            if isinstance(current, MailboxConnectionError):
                return 'rejected', Error('INVITATION_MAIL_CONNECTION', '邮箱服务暂时连接失败，本次未扣减卡密，请稍后重试。', 503)
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        if isinstance(exc, Error) and not remote_started:
            return 'rejected', exc
        return 'review', Error('INVITATION_REDEMPTION_REVIEW', '领取结果待核查，请稍后再试。', 409)

    def redeem_attempt(self, supplied_codes, attempt_id=None, recovery_secret=None):
        codes, lines = self._submitted_codes(supplied_codes)
        attempt_id, recovery_secret = self._attempt_identity(attempt_id, recovery_secret)
        proof = self._recovery_fingerprint(recovery_secret)
        digest = self._redemption_fingerprint('invitation:redemption:codes:v1', json.dumps(sorted(codes), separators=(',', ':')))
        cards = sorted(self._card_lock_name(code) for code in codes)
        with self._redemption_locks() as locks:
            if not locks.acquire(self._attempt_lock_name(attempt_id)):
                existing = self._attempt_row(attempt_id)
                if existing:
                    self._require_attempt_proof(existing, proof, digest)
                    return self._attempt_response(existing)
                raise Error('INVITATION_REDEMPTION_BUSY', '该领取请求正在处理，请查询原请求结果。', 409)
            existing = self._attempt_row(attempt_id)
            if existing:
                self._require_attempt_proof(existing, proof, digest)
                if existing['status'] == 'pending':
                    with self.db.transaction():
                        self._finish_attempt(attempt_id, 'review', error=Error('INVITATION_REDEMPTION_REVIEW', '领取结果待核查，请稍后再试。', 409))
                    existing = self._attempt_row(attempt_id)
                return self._attempt_response(existing)
            if not all(locks.acquire(name) for name in cards):
                error = Error('INVITATION_REDEMPTION_BUSY', '其中的卡密正在领取，请查询原请求结果，不要重复提交。', 409)
                self._save_attempt(attempt_id, proof, digest, 'rejected', result={'items': [
                    {'line': line, 'error': {'code': error.code, 'message': error.message}} for line in lines]}, error=error)
                return self._attempt_response(self._attempt_row(attempt_id))
            self._save_attempt(attempt_id, proof, digest, 'pending', cards=cards, owner=str(uuid4()))
            try:
                placeholders = ','.join('?' for _ in cards)
                blocked = self.db.one('SELECT a.attempt_id FROM invitation_v2_redemption_attempt_cards c '
                                      'JOIN invitation_v2_redemption_attempts a ON a.attempt_id=c.attempt_id '
                                      "WHERE c.code_fingerprint IN (" + placeholders + ") AND a.status IN ('pending','review') AND a.attempt_id<>? LIMIT 1", *cards, attempt_id)
                if blocked:
                    raise Error('INVITATION_REDEMPTION_REVIEW', '领取结果待核查，请稍后再试。', 409)
                errors, initial = self._redemption_cards(codes, lines)
                if errors:
                    with self.db.transaction():
                        self._finish_attempt(attempt_id, 'rejected', result={'items': errors})
                    return self._attempt_response(self._attempt_row(attempt_id))
                unused = [code for code in codes if initial[code]['status'] == 'unused']
                settings = None
                if unused:
                    # 一律从"已注册 GPT"的号池取号；不再现场造邮箱，因此这一段
                    # 没有任何外部副作用，只读判断池子够不够。
                    settings = self.repo.get_mail_settings()
                    ready, reason = self._mailbox_readiness(settings)
                    if not ready:
                        raise Error('INVITATION_MAIL_INSUFFICIENT', reason, 409)
                with self._mutex, self.db.transaction():
                    markers = [mysql.marker(code) for code in codes]
                    self._lock_codes(markers)
                    locks.require_owned()
                    errors, current = self._redemption_cards(codes, lines)
                    for code, line in zip(codes, lines):
                        if code in current and ((initial[code]['status']=='unused') != (current[code]['status']=='unused')
                                                or initial[code]['status']!='unused' and initial[code].get('email')!=current[code].get('email')):
                            errors.append({'line':line,'error':{'code':'INVITATION_BINDING_CHANGED','message':'邮箱记录发生变化，请重新查询。'}})
                    if errors:
                        self._finish_attempt(attempt_id, 'rejected', result={'items': errors})
                    else:
                        available = self.alloc.pool_available(settings)
                        if len(unused) > available:
                            if available <= 0:
                                # 池子空了：整批拒绝，用户侧统一提示"请稍候重试"。
                                raise Error('INVITATION_MAIL_INSUFFICIENT', '可用邮箱不足，本次未扣减卡密。', 409)
                            # 不足以覆盖整批：不绑定任何邮箱、不扣减任何卡密，只把
                            # "池里还剩几个"告诉前端 —— 由用户决定要不要只兑换一部分，
                            # 没被兑换的卡密保持 unused，随时可以再来。
                            self._finish_attempt(attempt_id, 'rejected', result={
                                'items': [{'line': line, 'error': {
                                    'code': 'INVITATION_POOL_INSUFFICIENT',
                                    'message': '可用账号不足，请稍后重试。'}} for line in lines],
                                'poolRequested': len(unused),
                            })
                            return self._attempt_response(self._attempt_row(attempt_id))
                        entries = []
                        for code, line in zip(codes, lines):
                            if current[code]['status']=='unused':
                                self.alloc.redeem_mailbox(code, settings=settings)
                            session = self.db.one('SELECT id FROM invitation_v2_sessions WHERE code_id=? FOR UPDATE', mysql.marker(code))
                            sid = session['id'] if session else str(uuid4())
                            if not session:
                                self.db.execute('INSERT INTO invitation_v2_sessions(id,code_id) VALUES (?,?)', sid, mysql.marker(code))
                            entries.append({'line': line, 'id': sid})
                        locks.require_owned()
                        self._finish_attempt(attempt_id, 'completed', result={'sessions': entries})
            except Exception as exc:  # noqa: F841 -- ruff 误报，理由见下方 except 内注释
                try:
                    locks.require_owned()
                except RedemptionLockLost as lost:
                    # 锁在进入这里之前就已失效（例如在邮箱绑定过程中连接被换掉）：此时
                    # 成交与否无法确定，必须按 RedemptionLockLost 归成 review，而不是拿
                    # 原始异常去分类——原始异常常常本身就是 Error（例如「可用邮箱不足」），
                    # 那会被判成 rejected，等于让用户直接重试。
                    #
                    # 上面的 noqa 是 ruff 的误报：只要在嵌套 except 里给异常变量重新赋值，
                    # F841 就会把它报成"赋值后未使用"，而下面 `self._redemption_failure(exc)`
                    # 正是它的读取点。已用最小样例在 ruff 0.17.0 上确认，与本处代码无关。
                    exc = lost
                # 取号只在一个事务里绑定号池行，没有任何外部副作用，
                # 因此不存在"上游可能已创建"这一类不确定来源。
                status, error = self._redemption_failure(exc)
                try:
                    with self.db.transaction():
                        self._finish_attempt(attempt_id, status, error=error)
                except Exception:
                    # The durable pending receipt remains a blocker after a DB
                    # outage; release of its owner derives review on recovery.
                    log.error('Redemption failure persistence unavailable')
                raise error from None
            return self._attempt_response(self._attempt_row(attempt_id))

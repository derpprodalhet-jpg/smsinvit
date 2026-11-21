"""Durable rental limits, one-use phone claims, and supplier settlement state.

Only the task gate may set the rental context. Querying providers never needs a
context; creating an order always does. Phone numbers are persisted as keyed
fingerprints, never as plaintext.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import math
import re
import time
from uuid import uuid4

from . import mysql

ACTIVE_SMS_RUN = ContextVar('invitation_sms_run', default=None)
MAX_RENTALS = 3
SETTLEMENT_ATTEMPTS = 3
SETTLEMENT_INTERVAL = 60


class SmsBudgetExhausted(RuntimeError):
    pass


class RentalUncertainError(RuntimeError):
    pass


@contextmanager
def task_run(run_id):
    token = ACTIVE_SMS_RUN.set(str(run_id or '').strip() or None)
    try:
        yield
    finally:
        ACTIVE_SMS_RUN.reset(token)


def normalize_phone(phone):
    raw = str(phone or '').strip()
    # Providers return full international numbers. Do not guess a local country.
    if raw.startswith('00'):
        raw = '+' + raw[2:]
    if not re.fullmatch(r'\+?[1-9][0-9 ()-]{6,24}', raw):
        raise ValueError('接码号码不是有效国际号码')
    digits = re.sub(r'[^0-9]', '', raw)
    if not 7 <= len(digits) <= 15:
        raise ValueError('接码号码不是有效国际号码')
    return '+' + digits


def phone_fingerprint(phone):
    normalized = normalize_phone(phone)
    return hmac.new(mysql.services().codec.fingerprint_key,
                    ('invitation:phone-use:v1:' + normalized).encode(),
                    hashlib.sha256).hexdigest()


def _cost_amount(value):
    """Finite, non-negative supplier amount or None; never guess a price."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _run_marker():
    run = ACTIVE_SMS_RUN.get()
    if not run:
        raise RuntimeError('接码租号必须在受保护的执行任务内进行')
    return mysql.protected(run)


def reserve_rental(platform, action):
    if platform not in ('smsbower', 'herosms', 'smspool') or action not in ('getNumber', 'getNumberV2'):
        raise ValueError('Unsupported rental operation')
    db = mysql.services().db
    now = time.time()
    with db.transaction():
        run = _run_marker()
        db.execute('INSERT IGNORE INTO invitation_v2_sms_budgets(run_id,reserved_count,created_at,updated_at) VALUES (?,0,?,?)', run, now, now)
        budget = db.one('SELECT reserved_count FROM invitation_v2_sms_budgets WHERE run_id=? FOR UPDATE', run)
        if budget['reserved_count'] >= MAX_RENTALS:
            raise SmsBudgetExhausted('本次任务三笔租号预算已用尽，停止继续租号')
        rental_id = str(uuid4())
        db.execute('UPDATE invitation_v2_sms_budgets SET reserved_count=reserved_count+1,updated_at=? WHERE run_id=?', now, run)
        db.execute('INSERT INTO invitation_v2_sms_orders(rental_id,run_id,platform,action,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)', rental_id, run, platform, action, 'reserved', now, now)
    return rental_id


def rental_no_order(rental_id):
    """Release a slot only after an explicit response proving no purchase."""
    db = mysql.services().db
    with db.transaction():
        row = db.one('SELECT run_id,status FROM invitation_v2_sms_orders WHERE rental_id=? FOR UPDATE', rental_id)
        if not row or row['status'] != 'reserved':
            return
        now = time.time()
        db.execute("UPDATE invitation_v2_sms_orders SET status='no_order',updated_at=? WHERE rental_id=?", now, rental_id)
        db.execute('UPDATE invitation_v2_sms_budgets SET reserved_count=GREATEST(0,reserved_count-1),updated_at=? WHERE run_id=?', now, row['run_id'])


def rental_unknown(rental_id, reason):
    db = mysql.services().db
    with db.transaction():
        db.execute("UPDATE invitation_v2_sms_orders SET status='rental_unknown',error=?,updated_at=? WHERE rental_id=? AND status='reserved'", mysql.protected(str(reason)[:1000]), time.time(), rental_id)


def rental_created(rental_id, activation_id, phone, *, activation_cost=None, currency=None):
    """Record a supplier rental, its confirmed cost and the cost currency.

    The amount is written together with the rental (same transaction as the
    supplier order), so an interrupted task cannot lose a price that the
    supplier already charged. An unknown amount stays NULL: cost statistics
    must never count a guessed zero.
    """
    cost = _cost_amount(activation_cost)
    unit = str(currency or '').strip().upper()[:8] or None
    db = mysql.services().db
    with db.transaction():
        row = db.one('SELECT status FROM invitation_v2_sms_orders WHERE rental_id=? FOR UPDATE', rental_id)
        if not row or row['status'] != 'reserved':
            raise RentalUncertainError('租号记录状态异常，停止继续下单')
        db.execute("UPDATE invitation_v2_sms_orders SET status='created',activation_id=?,phone_fingerprint=?,activation_cost=COALESCE(?,activation_cost),currency=COALESCE(?,currency),updated_at=? WHERE rental_id=?", mysql.protected(activation_id), phone_fingerprint(phone), cost, unit, time.time(), rental_id)


def claim_phone(phone):
    """Irrevocably claim before sending; ambiguity and failure never release it."""
    db = mysql.services().db
    with db.transaction():
        result = db.execute('INSERT IGNORE INTO invitation_v2_sms_phone_uses(phone_fingerprint,first_run_id,first_used_at,evidence_source,created_at) VALUES (?,?,?,?,?)', phone_fingerprint(phone), _run_marker(), time.time(), 'runtime-send', time.time())
        return result.rowcount == 1


def import_phone(phone, source, used_at=None):
    db = mysql.services().db
    with db.transaction():
        result = db.execute('INSERT IGNORE INTO invitation_v2_sms_phone_uses(phone_fingerprint,first_run_id,first_used_at,evidence_source,created_at) VALUES (?,NULL,?,?,?)', phone_fingerprint(phone), used_at or time.time(), str(source)[:160], time.time())
        return result.rowcount == 1


def order_state(platform, activation_id, status, error=''):
    if status not in ('completed', 'finish_pending', 'cancel_pending', 'cancelled', 'failed'):
        raise ValueError('Unsupported supplier order state')
    db = mysql.services().db
    with db.transaction():
        row = db.one('SELECT rental_id,status FROM invitation_v2_sms_orders WHERE platform=? AND activation_id=? FOR UPDATE', platform, mysql.marker(activation_id))
        if not row:
            raise RuntimeError('接码订单记录不存在，不能丢弃待处理订单')
        if row['status'] in ('completed', 'cancelled'):
            return
        db.execute('UPDATE invitation_v2_sms_orders SET status=?,error=?,next_at=?,updated_at=? WHERE rental_id=?', status, mysql.protected(str(error)[:1000]) if error else None, time.time() + SETTLEMENT_INTERVAL if status == 'finish_pending' else None, time.time(), row['rental_id'])


def settlement_due(limit=20):
    db = mysql.services().db
    # A crashed final lease must become visible for review, not remain stuck.
    db.execute("UPDATE invitation_v2_sms_orders SET status='finish_review',lease_token=NULL,updated_at=? WHERE status='finish_processing' AND next_at<=? AND attempts>=?", time.time(), time.time(), SETTLEMENT_ATTEMPTS)
    rows = db.all("SELECT * FROM invitation_v2_sms_orders WHERE (status='finish_pending' OR status='finish_processing') AND next_at<=? AND attempts<? ORDER BY next_at LIMIT ?", time.time(), SETTLEMENT_ATTEMPTS, max(1, min(100, int(limit))))
    return [{**row, 'id': row['rental_id'], 'activation_id': mysql.revealed(row['activation_id']), 'status': 'finish_pending'} for row in rows]


def settlement_claim(rental_id):
    db = mysql.services().db
    with db.transaction():
        now = time.time()
        lease = str(uuid4())
        result = db.execute("UPDATE invitation_v2_sms_orders SET status='finish_processing',attempts=attempts+1,lease_token=?,next_at=?,updated_at=? WHERE rental_id=? AND status IN ('finish_pending','finish_processing') AND next_at<=? AND attempts<?", lease, now + 120, now, rental_id, now, SETTLEMENT_ATTEMPTS)
        return lease if result.rowcount == 1 else None


def settlement_result(rental_id, ok, error='', *, claim=None):
    db = mysql.services().db
    with db.transaction():
        row = db.one('SELECT attempts,status,lease_token FROM invitation_v2_sms_orders WHERE rental_id=? FOR UPDATE', rental_id)
        if not row or row['status'] != 'finish_processing' or not claim or row['lease_token'] != claim:
            return row or {}
        status = 'completed' if ok else 'finish_review' if row['attempts'] >= SETTLEMENT_ATTEMPTS else 'finish_pending'
        db.execute('UPDATE invitation_v2_sms_orders SET status=?,error=?,lease_token=NULL,next_at=?,updated_at=? WHERE rental_id=?', status, mysql.protected(str(error)[:1000]) if error else None, None if status != 'finish_pending' else time.time() + SETTLEMENT_INTERVAL, time.time(), rental_id)
        return {'status': status, 'attempts': row['attempts']}

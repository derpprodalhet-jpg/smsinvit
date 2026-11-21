"""Public admission limits: IP floods, verified owners, and unknown proofs.

The supported baseline is 20 users behind one IP, with four visible tabs each.
At the fastest public cadence (1 second), this is 4,800 reads/minute per domain
and 240 reads/minute for one shared session. The read limits leave 25% headroom.
These are admission budgets, not a throughput guarantee or payment authority.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from uuid import UUID

from .errors import Error
from .security import client_ip, ctx

READ_IP_LIMIT = 6000
ENTRY_IP_LIMIT = 600
METADATA_IP_LIMIT = 1200
OWNER_READ_LIMIT = 300
OWNER_WRITE_LIMIT = 30
UNKNOWN_CARD_LIMIT = 20
UNKNOWN_CARD_WINDOW = 900


def flood_rate(request, domain, kind, services=None):
    """Run before proof/database checks; middleware and routes share one charge."""
    key = (domain, kind)
    charged = getattr(request.state, 'public_floods', set())
    if key in charged:
        return
    limit = READ_IP_LIMIT if kind == 'read' else ENTRY_IP_LIMIT
    (services or ctx(request)).limiter.check(f'public.v2.{domain}.{kind}.ip:{client_ip(request)}', limit, 60)
    request.state.public_floods = charged | {key}


def invalid_proof_rate(request, domain):
    ctx(request).limiter.check(f'public.v2.{domain}.invalid-proof.ip:{client_ip(request)}', 60, 60)


def owner_rate(request, domain, owner, operation='read', *, limit=None, window=60):
    allowance = limit or (OWNER_READ_LIMIT if operation == 'read' else OWNER_WRITE_LIMIT)
    ctx(request).limiter.check(f'public.v2.{domain}.{operation}.owner:{owner}', allowance, window)


def require_session(request, domain, scope, sid, presented, operation='read'):
    flood_rate(request, domain, 'read' if operation == 'read' else 'write')
    try:
        if presented and len(presented) > 128:
            raise Error('VALIDATION_ERROR', '会话凭证长度不合法。', 422)
        ctx(request).tokens.require(scope, str(sid), presented)
    except Error:
        invalid_proof_rate(request, domain)
        raise
    # An invalid proof must never consume a real owner's budget.
    owner_rate(request, domain, str(sid), operation)


CARD_TABLES = {'sms': ('sms_redeem_keys', 'code_fingerprint')}


def card_owner(services, domain, code):
    """Read-only possession check; the service still enforces all business guards.

    SMS uses the same text(), codec.digest() and decrypted comparison as its
    redeem method. Invitation uses its canonical uppercase marker HMAC.
    No status, expiry, inventory permission or payment check is bypassed here.
    """
    from .sms.support import text
    if not isinstance(code, str):
        return None
    value = text(code)
    if domain == 'invitation':
        if not value or len(value) > 160 or value.lower().startswith('iv2$'):
            return None
        value = value.upper()
        marker = 'iv2$' + hmac.new(services.codec.fingerprint_key, value.encode(), hashlib.sha256).hexdigest()
        row = services.db.one('SELECT code FROM invitation_v2_cdk_codes WHERE code=?', marker)
        return marker if row else None
    if not 4 <= len(value) <= 256:
        return None
    table, column = CARD_TABLES[domain]
    row = services.db.one(f'SELECT k.id,i.secret_ciphertext FROM {table} k '
                          f'JOIN inventory_items i ON i.id=k.inventory_item_id WHERE k.{column}=? LIMIT 1',
                          services.codec.digest(value))
    if not row:
        return None
    if not hmac.compare_digest(value.encode(), services.codec.decrypt(row['secret_ciphertext']).strip().encode()):
        return None
    return str(row['id'])


def cards_rate(request, domain, codes, operation='redeem', *, limit=OWNER_WRITE_LIMIT, window=60):
    flood_rate(request, domain, 'read' if operation == 'read' else 'entry')
    owners = [card_owner(ctx(request), domain, code) for code in codes]
    if not owners or any(owner is None for owner in owners):
        # Rotating random card strings cannot create independent generous buckets.
        ctx(request).limiter.check(f'public.v2.{domain}.unknown-card.ip:{client_ip(request)}',
                                   UNKNOWN_CARD_LIMIT, UNKNOWN_CARD_WINDOW)
    for owner in set(owners) - {None}:
        owner_rate(request, domain, owner, operation, limit=limit, window=window)


def invitation_action_rate(request, entries, action):
    operation = 'read' if action == 'status' else action
    flood_rate(request, 'invitation', 'read' if action == 'status' else 'write')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
        invalid_proof_rate(request, 'invitation')
        return  # The service retains its existing validation/error envelope.
    owners = set()
    invalid = False
    for entry in entries:
        try:
            sid = str(UUID(str(entry.get('id')))) if isinstance(entry, dict) else ''
            ctx(request).tokens.require('invocation-session', sid, entry.get('sessionSecret'))
            owners.add(sid)
        except (Error, ValueError, TypeError, AttributeError):
            invalid = True
    if invalid:
        invalid_proof_rate(request, 'invitation')
    for sid in owners:
        owner_rate(request, 'invitation', sid, operation)


def invitation_recovery_rate(request, attempt_id, presented):
    flood_rate(request, 'invitation', 'read')
    verified = False
    try:
        canonical = str(UUID(attempt_id))
        if isinstance(presented, str) and re.fullmatch('[0-9a-fA-F]{64}', presented):
            row = ctx(request).db.one('SELECT recovery_fingerprint FROM invitation_v2_redemption_attempts WHERE attempt_id=?', canonical)
            proof = hmac.new(ctx(request).codec.fingerprint_key,
                             ('invitation:redemption:recovery:v1:' + presented.lower()).encode(), hashlib.sha256).hexdigest()
            verified = bool(row and hmac.compare_digest(row['recovery_fingerprint'], proof))
    except (ValueError, TypeError, AttributeError):
        pass
    if verified:
        owner_rate(request, 'invitation', canonical, 'recover', limit=OWNER_READ_LIMIT)
    else:
        invalid_proof_rate(request, 'invitation')

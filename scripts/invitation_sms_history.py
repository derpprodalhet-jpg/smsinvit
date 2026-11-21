#!/usr/bin/env python3
"""Preview or import proven historical SMS phone use; never print raw numbers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

from database_tools import MaintenanceError, arguments, credentials, maintenance_lock
from private_archive import key_from_file, read_archive
from smsinvit.security import SecretCodec
from smsinvit.invitation import mysql, sms_state

# Only the original per-run logger's complete message templates are evidence.
# A prepare-send message counts even when the following response was lost.
# Quoted user content, examples, rented-but-unused numbers and OTPs do not.
_PHONE = r'(\+?[1-9][0-9]{6,14})'
PHONE_LOG = re.compile(
    r'[0-9]{2}:[0-9]{2}:[0-9]{2} \[(?:INFO|WARNING)\] auth_flow: \[sms\] (?:'
    r'📤 准备 POST add-phone/send \(phone=' + _PHONE + r'\) \.\.\.|'
    r'✅ POST add-phone/send 成功 \(phone=' + _PHONE + r'\)|'
    r'✅ phone-otp/validate 通过 \(phone=' + _PHONE + r' code=[0-9]{4,8}\) next=\S+|'
    r'validate 失败 \(phone=' + _PHONE + r' code=[0-9]{4,8}\): [^\r\n]+)'
)


def collect(members, caches=()):
    phones = {}
    scanned = evidence = invalid = 0
    for name, data in members.items():
        if not name.endswith('.log'):
            continue
        scanned += 1
        source = 'legacy-log:' + hashlib.sha256(name.encode()).hexdigest()
        for line in data.decode('utf-8', errors='replace').splitlines():
            match = PHONE_LOG.fullmatch(line)
            if not match:
                continue
            evidence += 1
            try:
                phone = next(value for value in match.groups() if value is not None)
                phones.setdefault(sms_state.normalize_phone(phone), source)
            except ValueError:
                invalid += 1
    for name, cache in caches:
        scanned += 1
        if not isinstance(cache, dict):
            raise ValueError('Unsupported SMS cache')
        if int(cache.get('use_count') or 0) <= 0 and not cache.get('used_codes'):
            continue
        evidence += 1
        try:
            phones.setdefault(sms_state.normalize_phone(cache.get('phone_number')), 'legacy-cache:' + hashlib.sha256(str(name).encode()).hexdigest())
        except ValueError:
            invalid += 1
    return phones, {'scanned_files': scanned, 'evidence_records': evidence, 'unique_phones': len(phones), 'invalid_records': invalid}


def import_history(db, codec, members, caches=(), apply=False):
    phones, report = collect(members, caches)
    mysql.configure(SimpleNamespace(db=db, codec=codec))
    existing = 0
    with db.transaction():
        for phone, source in phones.items():
            if db.one('SELECT phone_fingerprint FROM invitation_v2_sms_phone_uses WHERE phone_fingerprint=?', sms_state.phone_fingerprint(phone)):
                existing += 1
            elif apply:
                if not sms_state.import_phone(phone, source):
                    existing += 1
    return {**report, 'existing_phones': existing, 'new_phones': len(phones) - existing, 'applied': bool(apply), 'historical_coverage': 'available-evidence-only'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument('--keys-file', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--archive-keys-file', type=Path, required=True)
    parser.add_argument('--cache-file', type=Path, action='append', default=[])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    db = None
    try:
        if args.keys_file.stat().st_mode & 0o077:
            raise MaintenanceError('Retained keys must have owner-only permissions.')
        keys = json.loads(args.keys_file.read_text())
        codec = SecretCodec(keys['inventory_encryption_key'], keys['inventory_fingerprint_key'])
        members = read_archive(args.archive, key_from_file(args.archive_keys_file))
        caches = [(path.name, json.loads(path.read_text())) for path in args.cache_file]
        selected = credentials(args)
        db = selected.connect()
        with maintenance_lock(db, selected.database):
            result = import_history(db, codec, members, caches, args.apply)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print('Historical SMS import refused (' + type(exc).__name__ + '); no phone or credential data printed.', file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

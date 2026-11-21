"""Loopback-only preview with an isolated database and simulated suppliers."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'apps/api'), str(ROOT / 'vendor/codex_invit'), str(ROOT / 'scripts')]

import httpx
import uvicorn
from sqlalchemy import URL

from database_tools import Credentials
from migrate import run
from smsinvit.app import Services, create_app
from smsinvit.config import Config
from smsinvit.db import Database
from smsinvit.sms.client import NumberOrder, StatusResult
from smsinvit.sms.support import dumps, now

HOME = Path(os.environ.get('SMSINVIT_PREVIEW_HOME') or (Path(tempfile.gettempdir()) / 'smsinvit-local-preview'))
DATABASE = os.environ.get('SMSINVIT_PREVIEW_DATABASE', 'smsinvit_preview_20261018')
PORT = int(os.environ.get('SMSINVIT_PREVIEW_PORT') or 3010)
ADMIN_USER = os.environ.get('SMSINVIT_PREVIEW_ADMIN_USER') or 'admin'
ADMIN_EMAIL = os.environ.get('SMSINVIT_PREVIEW_ADMIN_EMAIL') or 'admin@example.test'
PASSWORD = os.environ.get('SMSINVIT_PREVIEW_ADMIN_PASSWORD') or '123456'
# NANPA toll-free codes, with country code 1. This is not a VoIP classifier.
EXCLUDED_PREFIXES = '1800,1833,1844,1855,1866,1877,1888'


def mysql_settings():
    """Loopback MySQL for the preview: env JSON wins, otherwise the private credentials file.

    Nothing is invented: without one of the two sources the preview refuses to start.
    """
    raw = os.environ.get('SMSINVIT_PREVIEW_MYSQL')
    if raw:
        return json.loads(raw)
    path = ROOT / 'data' / 'test-private' / 'mysql.json'
    if not path.is_file():
        raise SystemExit('Set SMSINVIT_PREVIEW_MYSQL={"host":...,"port":...,"user":...,"password":...} '
                         'or create data/test-private/mysql.json')
    return json.loads(path.read_text(encoding='utf-8-sig'))


class PreviewSms:
    """No sockets: code arrival is generated locally eight seconds after allocation."""
    def __init__(self):
        self.cancelled = set()
        self.next_requests = {}

    def configured(self):return True
    def get_balance(self):return 'ACCESS_BALANCE:100.00'
    def get_prices(self, *args):return {'0': {'ot': {'1': {'provider_id': 1, 'price': '0.10', 'count': 100}}}}
    def get_number(self, *args):
        return NumberOrder(f'preview-{time.time()}-{uuid4().hex}', '+12025550123', None)
    def get_status(self, aid):
        if aid in self.cancelled:return StatusResult('CANCEL')
        started = self.next_requests.get(aid, float(aid.split('-')[1]))
        if time.time() - started < 8:return StatusResult('WAIT')
        code = '654321' if aid in self.next_requests else '123456'
        return StatusResult('OK', code, 'STATUS_OK:' + code)
    def set_status(self, aid, status):
        if status == 8:self.cancelled.add(aid);return 'ACCESS_CANCEL'
        if status == 3:self.next_requests[aid] = time.time();return 'ACCESS_RETRY_GET'
        return 'ACCESS_ACTIVATION'
    def directory(self, kind):
        return [{'id': 0, 'eng': 'United States', 'chn': '美国（模拟）'}] if kind == 'countries' else [{'code': 'ot', 'name': '本地模拟短信'}]


def main():
    HOME.mkdir(mode=0o700, exist_ok=True)
    private = mysql_settings()
    host = str(private.get('host') or '127.0.0.1')
    port = int(private.get('port') or 13308)
    user = str(private.get('user') or 'root')
    run(Credentials(host, port, user, private['password'], DATABASE, 'disabled'))
    keyfile = HOME / 'keys.json'
    if not keyfile.exists():
        keyfile.write_text(json.dumps({key: secrets.token_hex(32) for key in ('encryption', 'fingerprint', 'jwt')}))
        keyfile.chmod(0o600)
    keys = json.loads(keyfile.read_text())
    static = HOME / 'static'
    # Static routing rejects symlinks that resolve outside its root.
    static.mkdir(exist_ok=True)
    for name, target in {'index.html': ROOT / 'apps/web/dist/index.html', 'assets': ROOT / 'apps/web/dist/assets',
                         'favicon.svg': ROOT / 'apps/web/dist/favicon.svg', 'brand-mark.svg': ROOT / 'apps/web/dist/brand-mark.svg',
                         'admin': ROOT / 'apps/admin/dist'}.items():
        link = static / name
        if link.is_symlink():link.unlink()
        if target.is_dir():shutil.copytree(target, link, dirs_exist_ok=True)
        else:shutil.copy2(target, link)
    config = Config({'smsinvit.inventory-encryption-key': keys['encryption'],
                     'smsinvit.inventory-fingerprint-key': keys['fingerprint'], 'smsinvit.jwt-secret': keys['jwt'],
                     'runtime.secure_cookies': False, 'runtime.totp_enabled': False,
                     'runtime.cors_origins': f'http://127.0.0.1:{PORT},http://localhost:{PORT}',
                     'runtime.public_api_base': f'http://127.0.0.1:{PORT}',
                     'smsinvit.worker-enabled': False, 'smsinvit.invitation-worker-enabled': False,
                     'smsinvit.invitation-pool-enabled': False, 'SMSINVIT_STATIC_DIR': str(static),
                     'SMSINVIT_INVITATION_DATA_DIR': str(HOME / 'invitation')}, environ={})
    db = Database(URL.create('mysql+pymysql', username=user, password=private['password'], host=host,
                             port=port, database=DATABASE, query={'charset': 'utf8mb4'}))
    services = Services(db, config)
    # Deny all supplier HTTP by construction, including background/admin probes.
    services.http.close()
    services.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503, json={'error': 'local preview: no external supplier'})))
    services.attach_domains()
    services.sms.client = PreviewSms()
    # The preview must not spend real lookup credits even if someone enters a key.
    services.sms.line_checker.http.close()
    services.sms.line_checker.http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(503, json={'error': 'local simulated classifier only'})))
    services.sms.catalog.client = services.sms.client
    services.sms.catalog.fx = lambda: {'usdCnyRate': 7.2, 'rateSource': 'local-preview', 'rateFetchedAt': now().isoformat() + 'Z'}
    preview_settings = services.sms.settings.load()
    preview_settings.update(pageIntro='本地模拟预览：输入未使用的 PREVIEW-SMS-001～007，取号后约 8 秒显示模拟验证码；不发送真实短信。',
                            importantNotices='当前是本地模拟接码，线路检测结果同样为模拟，不可用于真实网站验证。')
    services.sms.settings.save(preview_settings)
    with db.transaction():
        # The preview account is deterministic: every start restores this username,
        # email and password hash, so the documented credentials always work even
        # after someone changes them from the admin UI.
        admin = db.scalar('SELECT id FROM admin_users WHERE username=?', ADMIN_USER) \
            or db.scalar("SELECT id FROM admin_users WHERE username='preview'")
        if not admin:
            admin = str(uuid4())
            db.execute('INSERT INTO admin_users(id,username,email,password_hash,display_name) VALUES(?,?,?,?,?)',
                       admin, ADMIN_USER, ADMIN_EMAIL, services.codec.hash_password(PASSWORD), '本地预览管理员')
            role = db.scalar("SELECT id FROM roles WHERE code='SUPER_ADMIN'")
            db.execute('INSERT INTO admin_user_roles(id,admin_user_id,role_id) VALUES(?,?,?)', str(uuid4()), admin, role)
        else:
            db.execute('UPDATE admin_users SET username=?,email=?,password_hash=?,status=? WHERE id=?',
                       ADMIN_USER, ADMIN_EMAIL, services.codec.hash_password(PASSWORD), 'ACTIVE', admin)
        sku = db.scalar("SELECT id FROM skus WHERE code='LOCAL-PREVIEW-SMS'")
        attrs = {'productKind': 'SMS_REDEEM', 'smsService': 'ot', 'smsCountry': 0, 'smsMinPrice': 0,
                 'smsMaxPrice': 1, 'smsMinCount': 1, 'cdkValidDays': 15, 'smsPhoneException': EXCLUDED_PREFIXES}
        if not sku:
            sku = str(uuid4())
            db.execute('INSERT INTO skus(id,code,name,attributes_json) VALUES(?,?,?,?)', sku, 'LOCAL-PREVIEW-SMS', '本地模拟接码', dumps(attrs))
        else:
            db.execute('UPDATE skus SET attributes_json=? WHERE id=?', dumps(attrs), sku)
        db.execute("UPDATE sms_redeem_keys SET sms_phone_exception=? WHERE sku_id=? AND status='UNUSED' AND attempt_count=0",
                   EXCLUDED_PREFIXES, sku)
        for number in range(1, 8):
            code = f'PREVIEW-SMS-{number:03d}'
            if db.scalar('SELECT COUNT(*) FROM inventory_items WHERE secret_fingerprint=?', services.codec.digest(code)):continue
            item = str(uuid4())
            db.execute("INSERT INTO inventory_items(id,sku_id,status,secret_ciphertext,secret_fingerprint,issued_at,updated_at) VALUES(?,?,'ISSUED',?,?,NOW(3),NOW(3))",
                       item, sku, services.codec.encrypt(code), services.codec.digest(code))
            db.execute('INSERT INTO redeem_cdk_registry(business_kind,code_fingerprint,owner_inventory_id) VALUES(?,?,?)',
                       'SMS_REDEEM', services.codec.digest(code), item)
            services.sms.register_issued_key(item, sku, attrs, code)
    app = create_app(config, db, services=services, load_runtime=False)
    print(f'preview ready on http://127.0.0.1:{PORT}/ -- admin at /admin/ with {ADMIN_USER} / {PASSWORD}', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=PORT, access_log=False)


if __name__ == '__main__':main()

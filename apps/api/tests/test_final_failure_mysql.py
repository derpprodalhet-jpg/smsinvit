"""Actual MySQL deadlocks and SIGKILL at the paid-dispatch boundary.

Only the explicitly opted-in disposable database and a loopback fake supplier
are used. The child is terminated after the supplier receives its request but
before its response reaches the service; no in-memory state survives recovery.
"""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import URL
from sqlalchemy.exc import OperationalError

from smsinvit.config import Config
from smsinvit.db import Database
from smsinvit.errors import Error
from smsinvit.security import SecretCodec, TokenService
from smsinvit.sms.service import SmsService
from test_sms_gpt_mysql import SmsProvider, provision

pytestmark = pytest.mark.skipif(
    not os.environ.get('SMSINVIT_TEST_PRIVATE_CONFIG'),
    reason='Dedicated MySQL test configuration not enabled',
)


def test_real_mysql_deadlock_rolls_back_claim_before_paid_dispatch(provision):
    first = provision()
    second = provision()
    barrier = threading.Barrier(2)
    original = []
    for (service, _, _, _), other in ((first, second), (second, first)):
        claim = service.claim_attempt
        original.append(claim)

        def crossing_claim(key, service=service, claim=claim, other_id=other[3]):
            result = claim(key)  # Session and attempt changes remain uncommitted.
            barrier.wait(timeout=10)
            service.db.one('SELECT id FROM inventory_items WHERE id=? FOR UPDATE', other_id)
            return result

        service.claim_attempt = crossing_claim

    def redeem(fixture):
        service, _, code, _ = fixture
        try:
            return ('committed', service.redeem(code))
        except OperationalError as error:
            assert error.orig.args[0] == 1213  # An actual InnoDB deadlock victim.
            return ('rolled_back', None)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(redeem, (first, second)))
    finally:
        first[0].claim_attempt, second[0].claim_attempt = original
    assert sorted(result[0] for result in outcomes) == ['committed', 'rolled_back']
    assert first[1].calls + second[1].calls == 1
    for fixture, (outcome, _) in zip((first, second), outcomes):
        service, supplier, code, item = fixture
        key = service.db.one(
            'SELECT status,attempt_count FROM sms_redeem_keys WHERE inventory_item_id=?', item
        )
        count = service.db.scalar(
            'SELECT COUNT(*) FROM sms_sessions WHERE redeem_key_id='
            '(SELECT id FROM sms_redeem_keys WHERE inventory_item_id=?)', item
        )
        if outcome == 'rolled_back':
            assert key == {'status': 'UNUSED', 'attempt_count': 0}
            assert count == 0 and supplier.calls == 0
            # A fresh explicit submission is safe after the fully rolled-back claim.
            service.redeem(code)
            assert supplier.calls == 1
        else:
            assert key == {'status': 'LOCKED', 'attempt_count': 1}
            assert count == 1 and supplier.calls == 1
    assert first[1].calls + second[1].calls == 2  # One per distinct fixture card.


@pytest.fixture
def supplier_boundary():
    received = threading.Event()
    release = threading.Event()
    records = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            records.append((self.path, body))
            if self.path == '/paid':
                received.set()
                release.wait(timeout=30)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b'{}')
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', received, records
    release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def killed_dispatch(domain, identifier, boundary):
    endpoint, received, records = boundary
    import_paths=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
    import_paths.extend(value for value in os.environ.get('PYTHONPATH','').split(os.pathsep) if value)
    child_env = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        # Preserve the opted-in fixture's helper/vendor module paths. The child
        # imports the same test providers and production dependencies as pytest.
        'PYTHONPATH': os.pathsep.join(dict.fromkeys(import_paths)),
        'SMSINVIT_TEST_PRIVATE_CONFIG': str(Path(os.environ['SMSINVIT_TEST_PRIVATE_CONFIG']).resolve()),
    }
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), 'child', domain, identifier, endpoint],
        env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        if not received.wait(timeout=20):
            if process.poll() is None:process.kill()
            _,stderr=process.communicate(timeout=10)
            password=json.loads(Path(child_env['SMSINVIT_TEST_PRIVATE_CONFIG']).read_text())['password']
            detail=stderr.decode(errors='replace').replace(password,'<redacted>')[-4000:]
            raise AssertionError('Fixture child did not reach the paid-dispatch boundary\n'+detail)
        process.kill()
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    return records


def test_sms_sigkill_after_supplier_receive_restart_does_not_acquire_again(provision, supplier_boundary):
    service, supplier, code, item = provision()
    records = killed_dispatch('sms', code, supplier_boundary)
    assert [path for path, _ in records] == ['/paid']
    session = service.db.one(
        'SELECT s.id,s.status,s.acquisition_started_at FROM sms_sessions s '
        'JOIN sms_redeem_keys k ON k.id=s.redeem_key_id WHERE k.inventory_item_id=?', item
    )
    assert session['status'] == 'PENDING_NUMBER' and session['acquisition_started_at'] is not None
    # This object has never seen the child's memory. Reads of the persisted claim
    # cannot dispatch another supplier purchase, including after stale recovery.
    for _ in range(10):
        assert service.redeem(code)['id'] == session['id']
    service.db.execute(
        'UPDATE sms_sessions SET updated_at=DATE_SUB(NOW(3),INTERVAL 11 MINUTE) WHERE id=?', session['id']
    )
    service.sweep()
    assert service.redeem(code)['status'] == 'ACQUIRE_UNKNOWN'
    with pytest.raises(Error) as error:
        service.retry(session['id'])
    assert error.value.code == 'SMS_RESULT_PENDING'
    assert supplier.calls == 0 and len(records) == 1
    assert service.db.scalar('SELECT attempt_count FROM sms_redeem_keys WHERE inventory_item_id=?', item) == 1


def child_main(domain, identifier, endpoint):
    password = json.loads(Path(os.environ['SMSINVIT_TEST_PRIVATE_CONFIG']).read_text())['password']
    db = Database(URL.create('mysql+pymysql', username='root', password=password,
                            host='127.0.0.1', port=13308, database='smsinvit_test',
                            query={'charset': 'utf8mb4'}))
    assert db.scalar('SELECT DATABASE()') == 'smsinvit_test'
    config = Config({'smsinvit.jwt-secret': 't' * 64}, environ={})
    codec = SecretCodec('ab' * 32, 'test-only-fingerprint')
    tokens = TokenService('t' * 64)

    def marker(path, payload):
        with urlopen(Request(endpoint + path, data=json.dumps(payload).encode(),
                             headers={'Content-Type': 'application/json'}), timeout=30) as response:
            response.read()

    if domain == 'sms':
        class ChildSms(SmsProvider):
            def get_number(self, *args):
                marker('/paid', {})
                raise AssertionError('The test must terminate before supplier response')
        SmsService(db, codec, config, client=ChildSms(), tokens=tokens).redeem(identifier)
    else:
        raise ValueError('Unknown fixture domain')


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != 'child':
        raise SystemExit('Only the isolated fixture child entry point is supported')
    child_main(*sys.argv[2:])

"""Exercise HTTPS-proxy trust snapshots using two fresh, offline TLS fixtures.

No external service, deployment credential or database is used. The only DNS
seam supplies an approved public target; the production Policy connects to two
explicitly pinned loopback fixture proxies and never opens that target itself.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import socket
import ssl
import sys
import tempfile
import threading
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from sandbox_network import OFFICIAL_HOSTS, Policy


def certificates(name: str, directory: Path) -> tuple[bytes, Path, Path]:
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'fixture-proxy.test')]))
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('fixture-proxy.test')]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = directory / (name + '-leaf.pem')
    key_path = directory / (name + '-fixture-key.pem')
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    return ca.public_bytes(serialization.Encoding.PEM), cert_path, key_path


class FixtureProxy(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cert: Path, key: Path):
        self.connect_requests = 0
        self.authorization_headers = 0
        self.expected_authorization = 'Basic ' + base64.b64encode(b'fixture-user:fixture-password').decode()
        super().__init__(('127.0.0.1', 0), FixtureHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def handle_error(self, request, client_address):
        # Rejected TLS handshakes are expected and contain no application data.
        pass

    def finish(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        assert self.path == '8.8.8.8:443'
        self.server.connect_requests += 1
        authorization = self.headers.get('Proxy-Authorization')
        if authorization is not None:
            assert authorization == self.server.expected_authorization
            self.server.authorization_headers += 1
        self.send_response(200)
        self.end_headers()
        # Do not connect to the requested target; this is an offline fixture.


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='proxy-ca-snapshot-fixture-') as private:
        directory = Path(private)
        ca_a, cert_a, key_a = certificates('Fixture-CA-A', directory)
        ca_b, cert_b, key_b = certificates('Fixture-CA-B', directory)
        task_ca = directory / 'task-ca.pem'
        task_ca.write_bytes(ca_a)
        os.chmod(task_ca, 0o600)
        original_dns = socket.getaddrinfo

        def fixture_dns(host, port, *args, **kwargs):
            if host in OFFICIAL_HOSTS:
                assert port == 443
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
            assert host == '127.0.0.1', 'Unexpected fixture DNS request'
            return original_dns(host, port, *args, **kwargs)

        trusted = FixtureProxy(cert_a, key_a)
        attack = FixtureProxy(cert_b, key_b)
        try:
            with patch('socket.getaddrinfo', fixture_dns):
                def policy(server):
                    proxy = 'https://fixture-user:fixture-password@fixture-proxy.test:' + str(server.server_port)
                    return Policy({'HTTPS_PROXY': proxy}, proxy_pins=('127.0.0.1',), cafile=str(task_ca))

                trusted_policy = policy(trusted)
                attack_policy = policy(attack)
                for instance in (trusted_policy, attack_policy):
                    context = instance.proxy_tls_context
                    assert context is not None and context.check_hostname
                    assert context.verify_mode == ssl.CERT_REQUIRED
                    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2

                rejected = []

                def reject_attack(case):
                    try:
                        connection = attack_policy.connect('8.8.8.8', 443)
                    except ssl.SSLCertVerificationError:
                        rejected.append(case)
                    else:
                        connection.close()
                        raise AssertionError('Untrusted fixture proxy was accepted: ' + case)
                    assert attack.connect_requests == 0 and attack.authorization_headers == 0

                reject_attack('before_ca_replacement')
                replacement = directory / 'replacement.pem'
                replacement.write_bytes(ca_b)
                os.chmod(replacement, 0o600)
                replacement.replace(task_ca)
                # Same trusted parent policies; task changes cannot reload trust.
                trusted_policy.connect('8.8.8.8', 443).close()
                reject_attack('after_ca_replacement')
                assert trusted.connect_requests == 1 and trusted.authorization_headers == 1

                task_ca.unlink()
                trusted_policy.connect('8.8.8.8', 443).close()
                assert trusted.connect_requests == 2 and trusted.authorization_headers == 2

                # Missing context must fail before opening a proxy TCP socket.
                attack_policy.proxy_tls_context = None
                with patch('socket.create_connection', side_effect=AssertionError('Missing trust opened a TCP socket')) as create:
                    try:
                        attack_policy.connect('8.8.8.8', 443)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError('Missing HTTPS proxy trust was accepted')
                    assert create.call_count == 0
                assert attack.connect_requests == 0 and attack.authorization_headers == 0
        finally:
            trusted.finish()
            attack.finish()

    print(json.dumps({
        'status': 'PASS', 'architecture': platform.machine(),
        'policySha256': hashlib.sha256((SCRIPTS / 'sandbox_network.py').read_bytes()).hexdigest(),
        'rejectedAttackCases': rejected,
        'originalTrustedProxyStillAccepted': True,
        'deletedCaFileDoesNotBreakSnapshot': True,
        'postInitializationCaChangesIgnored': True,
        'missingContextRejectedBeforeTcp': True,
        'certificateAndHostnameVerificationRequired': True,
        'attackConnectRequests': attack.connect_requests,
        'attackProxyAuthorizationHeaders': attack.authorization_headers,
        'trustedConnectRequests': trusted.connect_requests,
        'trustedSyntheticProxyAuthorizationHeaders': trusted.authorization_headers,
        'realUpstreamBusinessRequests': 0,
        'allKeysAndCredentialsSynthetic': True,
    }, sort_keys=True))


if __name__ == '__main__':
    main()

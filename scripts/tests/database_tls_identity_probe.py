"""Real Config/PyMySQL certificate+hostname verification on isolated MySQL13308.

Only the specified fixture container's public CA/server certificate is copied.
The test DNS seam changes socket routing to loopback, never the original hostname
passed to TLS. No verification flag is disabled. Temporary schema/account are
removed. Negative handshakes must finish before any SQL command or write.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import socket
import ssl
import sys
import tempfile
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pymysql
from pymysql.constants import COMMAND
from sqlalchemy.engine import make_url

WORK = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORK / 'apps/api'))
from smsinvit.config import Config


class ObservedConnection(pymysql.connections.Connection):
    """Count real COM_QUERY calls; delegate every operation to unchanged PyMySQL."""
    def __init__(self, observations, **kwargs):
        self.observations = observations
        super().__init__(**kwargs)

    def _execute_command(self, command, sql):
        if command == COMMAND.COM_QUERY:
            self.observations['sqlCommands'] += 1
            text = sql.decode(errors='replace') if isinstance(sql, bytes) else str(sql)
            parts = text.lstrip().split(None, 1)
            if parts and parts[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'ALTER', 'DROP', 'TRUNCATE'):
                self.observations['sqlWriteCommands'] += 1
        return super()._execute_command(command, sql)


@contextmanager
def route_test_names(correct, wrong):
    original = socket.create_connection
    def routed(address, *args, **kwargs):
        host, port = address
        if host not in (correct, wrong) or port != 13308:
            raise AssertionError('A test connection tried to leave the isolated database endpoint')
        return original(('127.0.0.1', 13308), *args, **kwargs)
    with patch.object(socket, 'create_connection', routed):
        yield


def unrelated_ca(path):
    # The fresh test-only private key stays in memory. Only its public CA PEM is
    # written; no server private key is accessed or copied.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Unrelated offline fixture CA')])
    now = datetime.now(timezone.utc)
    cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(
        x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(
        now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256())
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def probe(credentials_file, ca_file, server_file):
    private = json.loads(Path(credentials_file).read_text())
    assert private.get('host', '127.0.0.1') == '127.0.0.1' and int(private.get('port', 13308)) == 13308
    server_cert = x509.load_pem_x509_certificate(Path(server_file).read_bytes())
    names = server_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert len(names) == 1
    correct_host = names[0].value
    wrong_host = 'mismatched-identity-fixture.invalid'
    assert correct_host != wrong_host
    suffix = secrets.token_hex(5); schema = 'smsinvit_tls_identity_' + suffix; user = 'tls_identity_' + suffix
    password = secrets.token_urlsafe(32)
    admin = pymysql.connect(host='127.0.0.1', port=13308, user='root', password=private['password'], autocommit=True)
    account_created = schema_created = False
    cases = {}
    try:
        with admin.cursor() as cursor:
            cursor.execute('CREATE DATABASE `' + schema + '`'); schema_created = True
            cursor.execute('CREATE TABLE `' + schema + '`.write_guard(id INT PRIMARY KEY) ENGINE=InnoDB')
            cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s REQUIRE SSL', (user, '%', password)); account_created = True
            cursor.execute('GRANT SELECT,INSERT ON `' + schema + '`.write_guard TO %s@%s', (user, '%'))

        with tempfile.TemporaryDirectory(prefix='database-tls-identity-') as temp:
            bad_ca = Path(temp) / 'unrelated-public-ca.pem'; unrelated_ca(bad_ca)
            with route_test_names(correct_host, wrong_host):
                for label, host, ca in [('valid', correct_host, Path(ca_file)), ('wrongHostname', wrong_host, Path(ca_file)), ('untrustedCa', correct_host, bad_ca)]:
                    config = Config({'MYSQL_HOST': host, 'MYSQL_PORT': 13308, 'MYSQL_DATABASE': schema,
                                     'MYSQL_USER': user, 'MYSQL_PASSWORD': password,
                                     'MYSQL_TLS_MODE': 'VERIFY_IDENTITY', 'MYSQL_SSL_CA': str(ca)}, environ={})
                    url = make_url(config.database_url()); options = config.database_connect_args()
                    context = options['ssl']
                    assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED
                    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
                    observations = {'sqlCommands': 0, 'sqlWriteCommands': 0}
                    try:
                        conn = ObservedConnection(observations, host=url.host, port=url.port, user=url.username,
                                                  password=url.password, database=url.database, charset='utf8mb4', **options)
                    except pymysql.err.OperationalError as exc:
                        cause = getattr(exc, 'original_exception', None)
                        assert label != 'valid' and isinstance(cause, ssl.SSLCertVerificationError)
                        assert observations == {'sqlCommands': 0, 'sqlWriteCommands': 0}
                        cases[label] = {'status': 'REJECTED', 'tlsVerificationError': type(cause).__name__,
                                        'verifyCode': cause.verify_code, **observations, 'upstreamRequests': 0}
                    else:
                        try:
                            assert label == 'valid', 'Invalid certificate identity was accepted'
                            peer = conn._sock.getpeercert(binary_form=True)
                            assert peer == server_cert.public_bytes(serialization.Encoding.DER)
                            with conn.cursor() as cursor:
                                cursor.execute('SELECT 1'); assert cursor.fetchone() == (1,)
                                cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'"); cipher = cursor.fetchone()[1]
                                cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_version'"); version = cursor.fetchone()[1]
                            assert cipher and version and observations['sqlWriteCommands'] == 0
                            cases[label] = {'status': 'CONNECTED', 'certificateAndHostnameVerified': True,
                                            'peerMatchesCopiedPublicServerCert': True, 'cipher': cipher, 'tlsVersion': version,
                                            **observations, 'upstreamRequests': 0}
                        finally:
                            conn.close()
        assert set(cases) == {'valid', 'wrongHostname', 'untrustedCa'}
        with admin.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM `' + schema + '`.write_guard'); assert cursor.fetchone() == (0,)
        return {'format': 'smsinvit-database-tls-identity-v1', 'status': 'PASS',
                'recordedAtUtc': datetime.now(timezone.utc).isoformat(), 'fixtureContainer': 'smsinvit-python-validation',
                'scope': 'New random account/schema in isolated MySQL8.4 localhost:13308; no original database/container used',
                'actualConfigDatabaseConnectArgsUsed': True, 'actualPymysqlHandshakeUsed': True,
                'tlsMode': 'VERIFY_IDENTITY', 'checkHostname': True, 'verifyMode': 'CERT_REQUIRED',
                'minimumTlsVersion': 'TLSv1.2', 'verifiedPublicCertificateCommonName': correct_host,
                'testDnsSeam': 'Only the two test hostname routes become 127.0.0.1:13308; TLS server_hostname retains the original configured name',
                'certificateCopies': 'Public ca.pem and server-cert.pem only; no server private key accessed/copied',
                'publicCaPemSha256': hashlib.sha256(Path(ca_file).read_bytes()).hexdigest(),
                'publicServerCertPemSha256': hashlib.sha256(Path(server_file).read_bytes()).hexdigest(),
                'probeCodeSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'configCodeSha256': hashlib.sha256((WORK/'apps/api/smsinvit/config.py').read_bytes()).hexdigest(),
                'cases': cases, 'guardTableRows': 0, 'realUpstreamRequests': 0, 'privateCredentialPrinted': False,
                'runtimeCodeChanged': False, 'temporaryAccountAndSchemaRemoved': True,
                'notVerified': ['Production deployment certificate/SAN, server DNS and actual production database availability are separate deployment checks.']}
    finally:
        with admin.cursor() as cursor:
            if account_created: cursor.execute('DROP USER %s@%s', (user, '%'))
            if schema_created: cursor.execute('DROP DATABASE `' + schema + '`')
        admin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file', required=True, type=Path)
    parser.add_argument('--ca-file', required=True, type=Path)
    parser.add_argument('--server-cert', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    report = probe(args.credentials_file, args.ca_file, args.server_cert)
    args.evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'cases': {k:v['status'] for k,v in report['cases'].items()},
                      'negativeSqlCommands': sum(v['sqlCommands'] for k,v in report['cases'].items() if k != 'valid'),
                      'sqlWriteCommands': sum(v['sqlWriteCommands'] for v in report['cases'].values()),
                      'realUpstreamRequests': report['realUpstreamRequests']}))


if __name__ == '__main__': main()

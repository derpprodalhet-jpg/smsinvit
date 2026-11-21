"""Private credential handling shared by explicit database maintenance commands."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from dataclasses import dataclass, field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))


class MaintenanceError(Exception):
    """Safe message, deliberately excluding driver error text and credentials."""


@dataclass(frozen=True)
class Credentials:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
    tls_mode: str = "verify-identity"
    ca_file: str | None = None

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_$-]{1,64}", self.database):
            raise MaintenanceError("Database name must be an explicit safe identifier.")
        if not self.user or any(c in self.user + self.host for c in "\r\n\0") or not 1 <= self.port <= 65535:
            raise MaintenanceError("Invalid connection metadata.")
        if self.tls_mode == "disabled":
            try:
                local = ipaddress.ip_address(self.host).is_loopback
            except ValueError:
                local = self.host == "localhost"
            if not local:
                raise MaintenanceError("TLS may only be disabled for an explicitly selected loopback test database.")
        elif self.tls_mode != "verify-identity":
            raise MaintenanceError("TLS certificate and hostname verification are required.")
        return self

    def connect(self):
        self.validate()
        from smsinvit.db import Database
        from sqlalchemy import create_engine, URL
        import ssl
        options = {"connect_timeout": 10, "read_timeout": 30, "write_timeout": 30}
        if self.tls_mode == "verify-identity":
            options["ssl"] = ssl.create_default_context(cafile=self.ca_file)
        url = URL.create("mysql+pymysql", username=self.user, password=self.password, host=self.host, port=self.port,
                         database=self.database, query={"charset": "utf8mb4"})
        return Database(engine=create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=0, connect_args=options))


def arguments(parser, default_user="smsinvit_migration"):
    parser.add_argument("--credentials-file", type=Path, help="Private JSON connection file; passwords never belong in command arguments.")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user")
    parser.add_argument("--database")
    parser.add_argument("--tls-mode", choices=["verify-identity", "disabled"])
    parser.add_argument("--ca-file")
    parser.set_defaults(default_user=default_user)


def credentials(args):
    values = {}
    if args.credentials_file:
        path = args.credentials_file.resolve()
        if not path.is_file():
            raise MaintenanceError("Private credential file was not found.")
        if path.stat().st_mode & 0o077:
            raise MaintenanceError("Credential file must have owner-only permissions (0600).")
        try:
            values = json.loads(path.read_text())
        except (ValueError, UnicodeError):
            raise MaintenanceError("Private credential file must contain a JSON object.") from None
        if not isinstance(values, dict):
            raise MaintenanceError("Private credential file must contain a JSON object.")
    password = values.get("password", os.environ.get("SMSINVIT_MIGRATION_PASSWORD"))
    if password is None:
        raise MaintenanceError("An explicit private credential file or SMSINVIT_MIGRATION_PASSWORD is required.")
    result = Credentials(host=args.host or values.get("host", os.environ.get("SMSINVIT_MIGRATION_HOST", "127.0.0.1")),
                         port=args.port or int(values.get("port", os.environ.get("SMSINVIT_MIGRATION_PORT", 3306))),
                         user=args.user or values.get("user", os.environ.get("SMSINVIT_MIGRATION_USER", args.default_user)),
                         password=str(password), database=args.database or values.get("database", os.environ.get("SMSINVIT_MIGRATION_DATABASE", "smsinvit")),
                         tls_mode=args.tls_mode or values.get("tls_mode", "verify-identity"), ca_file=args.ca_file or values.get("ca_file"))
    return result.validate()


def lock_name(database):
    return "smsinvit:schema:" + hashlib.sha256(database.encode()).hexdigest()[:32]


@contextlib.contextmanager
def maintenance_lock(db, database):
    # Advisory locks are connection-scoped and survive MySQL implicit DDL commits.
    with db.engine.connect() as connection:
        acquired = connection.exec_driver_sql("SELECT GET_LOCK(%s,0)", (lock_name(database),)).scalar()
        connection.commit()
        if acquired != 1:
            raise MaintenanceError("Another migration or backup holds the database maintenance lock.")
        try:
            yield connection
        finally:
            connection.rollback()
            connection.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (lock_name(database),))
            connection.commit()


def client_escape(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


@contextlib.contextmanager
def client_defaults(selected):
    fd, name = tempfile.mkstemp(prefix="smsinvit-mysql-", suffix=".cnf")
    try:
        os.fchmod(fd, 0o600)
        lines = ["[client]", "host=" + client_escape(selected.host), "port=" + str(selected.port), "user=" + client_escape(selected.user),
                 "password=" + client_escape(selected.password), "protocol=TCP", "default-character-set=utf8mb4",
                 "ssl-mode=" + ("DISABLED" if selected.tls_mode == "disabled" else "VERIFY_IDENTITY")]
        if selected.ca_file:
            lines.append("ssl-ca=" + client_escape(selected.ca_file))
        with os.fdopen(fd, "w") as output:
            output.write("\n".join(lines) + "\n")
        yield Path(name)
    finally:
        Path(name).unlink(missing_ok=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")

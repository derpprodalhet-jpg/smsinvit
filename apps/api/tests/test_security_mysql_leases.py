"""Opt-in checks create and remove only a unique database on the test port."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import sys
from uuid import uuid4

import pymysql
import pytest
from sqlalchemy import URL

from smsinvit.common.auth import Captcha
from smsinvit.db import Database
from smsinvit.errors import Error
from smsinvit.security import SecretCodec, TokenService
from test_sms_gpt_state import Config

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
from migrate import split_sql

pytestmark = pytest.mark.skipif(os.getenv('SMSINVIT_SECURITY_MYSQL_TEST') != '1', reason='isolated MySQL security checks explicitly opt-in')


@pytest.fixture
def db():
    # The port belongs to smsinvit-python-validation, never the business DB.
    credentials = Path(os.environ.get('SMSINVIT_TEST_MYSQL_CREDENTIALS_FILE', str(ROOT / 'data/test-private/mysql.json')))
    password = json.loads(credentials.read_text())['password']
    name = 'smsinvit_security_test_' + uuid4().hex[:12]
    admin = pymysql.connect(host='127.0.0.1', port=13308, user='root', password=password, autocommit=True)
    instance = None
    created = False
    try:
        with admin.cursor() as cursor:
            cursor.execute('CREATE DATABASE `' + name + '` CHARACTER SET utf8mb4')
            created = True
        instance = Database(URL.create('mysql+pymysql', username='root', password=password, host='127.0.0.1', port=13308, database=name))
        assert instance.scalar('SELECT DATABASE()') == name
        # 本夹具直接从 V1 摘表建 schema，而 V1 的表显式声明 utf8mb4_unicode_ci；
        # 这里的替身表必须用同一排序规则，否则外键会因为服务器默认排序规则
        # （MySQL 8.0+ 是 utf8mb4_0900_ai_ci）而建不起来。
        instance.execute('CREATE TABLE admin_users(id CHAR(36) PRIMARY KEY) ENGINE=InnoDB COLLATE=utf8mb4_unicode_ci')
        legacy = split_sql((ROOT / 'migrations/legacy/V1__mysql_schema.sql').read_text())
        statement = next(sql for sql in legacy if sql.startswith('CREATE TABLE auth_challenges ('))
        instance.execute(statement)
        yield instance
    finally:
        if instance is not None:
            instance.close()
        if created:
            with admin.cursor() as cursor:
                cursor.execute('DROP DATABASE `' + name + '`')
        admin.close()


def test_real_mysql_captcha_single_use_is_independent_of_caller_transaction(db):
    captcha = Captcha(SecretCodec('11' * 32, 'isolated-fixture-fingerprint'), TokenService('isolated-fixture-token-secret-32bytes'), Config(), db=db)
    issued = captcha.issue()
    a, op, b, *_ = issued['prompt'].split()
    answer = str(int(a) + int(b) if op == '+' else int(a) - int(b))
    def verify(_):
        try:
            captcha.verify_local(issued['captchaToken'], answer)
            return 1
        except Error:
            return 0
    with ThreadPoolExecutor(max_workers=20) as pool:
        assert sum(pool.map(verify, range(40))) == 1
    second = captcha.issue()
    with pytest.raises(Error):
        with db.transaction():
            captcha.verify_local(second['captchaToken'], '99')
    assert db.scalar("SELECT COUNT(*) FROM auth_challenges WHERE consumed_at IS NOT NULL") == 2

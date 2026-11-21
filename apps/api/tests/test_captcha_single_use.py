"""Challenge replay protection and optional-but-bound TOTP behavior."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smsinvit.common.auth import AdminAuth, Captcha
from smsinvit.errors import Error
from smsinvit.security import TokenService
from test_common_security import ChallengeDB, NoopLimiter, codec, config


def challenge(config, codec):
    captcha = Captcha(codec, TokenService(config), config, db=ChallengeDB())
    issued = captcha.issue()
    a, op, b, *_ = issued['prompt'].split()
    answer = str(int(a) + int(b) if op == '+' else int(a) - int(b))
    return captcha, issued['captchaToken'], answer


@pytest.mark.parametrize('wrong', ['99', '', None, 'not-a-number'])
def test_incorrect_answer_still_consumes_challenge(config, codec, wrong):
    captcha, token, answer = challenge(config, codec)
    with pytest.raises(Error):
        captcha.verify_local(token, wrong)
    with pytest.raises(Error):
        captcha.verify_local(token, answer)


def test_concurrent_replay_has_exactly_one_winner(config, codec):
    captcha, token, answer = challenge(config, codec)
    def verify(_):
        try:
            captcha.verify_local(token, answer)
            return True
        except Error:
            return False
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(verify, range(40))) == 1


def test_invalid_signature_does_not_consume_another_valid_challenge(config, codec):
    captcha, token, answer = challenge(config, codec)
    with pytest.raises(Error):
        captcha.verify_local(token + 'x', answer)
    captcha.verify_local(token, answer)


def test_expired_database_challenge_cannot_be_used(config, codec):
    captcha, token, answer = challenge(config, codec)
    with captcha.db.db:
        captcha.db.db.execute("UPDATE auth_challenges SET expires_at='2000-01-01'")
    with pytest.raises(Error):
        captcha.verify_local(token, answer)


def test_failed_password_does_not_rollback_captcha(config, codec):
    captcha, token, answer = challenge(config, codec)
    auth = AdminAuth(captcha.db, codec, TokenService(config), config, NoopLimiter(), captcha)
    auth.mapper = Mock()
    auth.mapper.findByUsernameOrEmailForUpdate.return_value = None
    body = {'identifier': 'no-user', 'password': 'wrong', 'captchaToken': token, 'captchaAnswer': answer}
    with pytest.raises(Error, match='账号或密码错误'):
        auth.login(body, '203.0.113.1', 'test')
    with pytest.raises(Error, match='验证码已使用'):
        auth.login(body, '203.0.113.1', 'test')
    assert auth.mapper.findByUsernameOrEmailForUpdate.call_count == 1


def test_turnstile_token_never_bypasses_arithmetic(config, codec):
    captcha, _, _ = challenge(config, codec)
    config.runtime.update({'runtime.turnstile_site_key': 'configured', 'runtime.turnstile_secret_key': 'configured'})
    assert not captcha.turnstile_enabled()
    with pytest.raises(Error):
        captcha.verify({'turnstileToken': 'client-chosen-mode'}, '203.0.113.1')


def test_bound_totp_is_required_when_mandatory_enrollment_is_off(config, codec):
    db = ChallengeDB()
    auth = AdminAuth(db, codec, TokenService(config), config, NoopLimiter(), Mock())
    auth.mapper = Mock()
    auth.mapper.findByUsernameOrEmailForUpdate.return_value = {'id': 'admin', 'status': 'ACTIVE', 'passwordHash': codec.hash_password('password'), 'totpEnabledAt': '2026-10-10'}
    auth.roles = lambda _: ['ADMIN']
    auth.profile = lambda _: {'totpSetupRequired': False}
    auth.issue = Mock()
    result = auth.login({'identifier': 'admin', 'password': 'password'}, '203.0.113.1', 'test')
    assert result['needTotp'] is True and result['challengeToken']
    auth.issue.assert_not_called()
    auth.mapper.insertTotpLoginChallenge.assert_called_once()

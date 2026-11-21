"""Exercise real rate checks with local counters; no upstream or production DB."""
from collections import Counter
from contextlib import contextmanager
import hashlib
import hmac
from types import SimpleNamespace
from uuid import UUID

import pytest

from smsinvit.app import common_rate
from smsinvit.common.auth import RateLimiter
from smsinvit.errors import Error
from smsinvit.public_limits import cards_rate,flood_rate,invitation_action_rate,invitation_recovery_rate,require_session
from smsinvit.security import TokenService
from test_common_security import config,codec


class ProofDB:
    def __init__(self, codec):
        self.codec=codec;self.cards={};self.attempts={};self.jobs={};self.reads=0
    @contextmanager
    def transaction(self,**kwargs):yield self
    def one(self,sql,*args):
        self.reads+=1
        if 'redemption_attempts' in sql:return self.attempts.get(args[0])
        for (domain,fingerprint),row in self.cards.items():
            table={'sms':'sms_redeem_keys','invitation':'invitation_v2_cdk_codes'}[domain]
            if table in sql and fingerprint==args[0]:return row
        return None
    def issue(self,domain,code,id):
        fingerprint=self.codec.digest(code)
        row={'id':id,'secret_ciphertext':self.codec.encrypt(code)}
        if domain=='invitation':
            code=code.upper()
            fingerprint='iv2$'+hmac.new(self.codec.fingerprint_key,code.encode(),hashlib.sha256).hexdigest()
            row={'code':fingerprint}
        self.cards[(domain,fingerprint)]=row
        return row


@pytest.fixture
def services(codec,config):
    db=ProofDB(codec);limiter=RateLimiter(db,codec);counts=Counter();buckets=[]
    limiter.mapper=SimpleNamespace(bump=lambda key,seconds:counts.update([key]),attempts=lambda key:counts[key],window=lambda key:{'remainingSeconds':59})
    check=limiter.check
    def recorded(bucket,limit,window):
        buckets.append((bucket,limit,window));check(bucket,limit,window)
    limiter.check=recorded
    return SimpleNamespace(db=db,codec=codec,tokens=TokenService(config),limiter=limiter,counts=counts,buckets=buckets)


def request(services,path='',method='GET'):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)),
                           state=SimpleNamespace(client_ip='203.0.113.10',principal=None),
                           client=None,url=SimpleNamespace(path=path),method=method)


def sid(index):return str(UUID(int=index+1))


@pytest.mark.parametrize('domain,scope',[('sms','sms-session')])
def test_twenty_users_four_tabs_can_poll_for_a_minute(services,domain,scope):
    # Every owner has four 1-second tabs: 240/owner, 4800/IP in one window.
    for _ in range(60):
        for user in range(20):
            for _ in range(4):
                require_session(request(services),domain,scope,sid(user),services.tokens.issue(scope,sid(user)))
    assert max(services.counts.values())==4800
    # Read traffic cannot exhaust a separate legitimate mutation budget.
    require_session(request(services),domain,scope,sid(0),services.tokens.issue(scope,sid(0)),'confirm')


def test_polling_owner_limit_is_independent_and_invalid_proofs_do_not_charge_it(services):
    token=services.tokens.issue('sms-session',sid(0))
    for _ in range(10):
        with pytest.raises(Error):require_session(request(services),'sms','sms-session',sid(0),'incorrect','retry')
    assert not any('.owner:' in bucket[0] for bucket in services.buckets)
    for _ in range(300):require_session(request(services),'sms','sms-session',sid(0),token)
    with pytest.raises(Error) as limited:require_session(request(services),'sms','sms-session',sid(0),token)
    assert limited.value.status==429 and limited.value.retry_after==59
    require_session(request(services),'sms','sms-session',sid(1),services.tokens.issue('sms-session',sid(1)))
    require_session(request(services),'sms','sms-session',sid(0),token,'retry')


@pytest.mark.parametrize('domain',['sms','invitation'])
def test_unknown_card_budget_does_not_block_twenty_valid_users_four_tabs(services,domain):
    for index in range(20):cards_rate(request(services),domain,[f'unknown-{index}'])
    with pytest.raises(Error) as limited:cards_rate(request(services),domain,['another-unknown'])
    assert limited.value.status==429
    for user in range(20):
        code=f'VALID-CARD-{user}';services.db.issue(domain,code,sid(user))
        for _ in range(4):cards_rate(request(services),domain,[code])
    unknown=[bucket for bucket in services.buckets if 'unknown-card' in bucket[0]]
    assert len(unknown)==21 and all(bucket[1:]==(20,900) for bucket in unknown)
    assert all('VALID-CARD' not in bucket[0] and 'unknown-0' not in bucket[0] for bucket in services.buckets)


@pytest.mark.parametrize('domain',['sms'])
def test_matching_fingerprint_without_matching_plaintext_is_still_unknown(services,domain):
    services.db.cards[(domain,services.codec.digest('presented-code'))]={'id':'not-owner','secret_ciphertext':services.codec.encrypt('different-code')}
    cards_rate(request(services),domain,['presented-code'])
    assert 'unknown-card' in services.buckets[-1][0]
    assert not any('.owner:' in bucket[0] for bucket in services.buckets)


def test_entry_flood_rejects_before_any_card_lookup(services):
    for _ in range(600):flood_rate(request(services),'sms','entry')
    before=services.db.reads
    with pytest.raises(Error):cards_rate(request(services),'sms',['VALID-CARD'])
    assert services.db.reads==before






def test_invitation_batch_status_has_verified_owner_budgets_and_independent_start(services):
    entries=[{'id':sid(user),'sessionSecret':services.tokens.issue('invocation-session',sid(user))} for user in range(10)]
    for _ in range(240):invitation_action_rate(request(services),entries,'status')
    invitation_action_rate(request(services),entries,'start')
    invitation_action_rate(request(services),[{'id':sid(0),'sessionSecret':'wrong'}],'start')
    owner=f'public.v2.invitation.start.owner:{sid(0)}'
    assert services.counts[services.codec.sha256(owner)]==1


def test_twenty_invitation_users_four_tabs_can_poll_for_a_minute(services):
    entries=[{'id':sid(user),'sessionSecret':services.tokens.issue('invocation-session',sid(user))} for user in range(20)]
    for _ in range(60):
        for entry in entries:
            for _ in range(4):invitation_action_rate(request(services),[entry],'status')
    assert services.counts[services.codec.sha256('public.v2.invitation.read.ip:203.0.113.10')]==4800
    for entry in entries:invitation_action_rate(request(services),[entry],'start')


def test_receipt_recovery_is_verified_and_does_not_share_metadata_or_invalid_proof_budget(services):
    secret='a'*64;attempt=sid(0)
    proof=hmac.new(services.codec.fingerprint_key,('invitation:redemption:recovery:v1:'+secret).encode(),hashlib.sha256).hexdigest()
    services.db.attempts[attempt]={'recovery_fingerprint':proof}
    for _ in range(60):invitation_recovery_rate(request(services),attempt,'b'*64)
    with pytest.raises(Error):invitation_recovery_rate(request(services),attempt,'b'*64)
    for _ in range(240):invitation_recovery_rate(request(services),attempt,secret)
    assert services.counts[services.codec.sha256('public.v2.invitation.recover.owner:'+attempt)]==240
    assert not any('metadata' in bucket[0] for bucket in services.buckets)


def test_metadata_domains_are_separate_and_middleware_does_not_double_charge(services):
    for domain in ('sms','invitation'):
        for _ in range(960):common_rate(request(services,f'/api/v1/{domain}/page-content'),services)
    r=request(services,f'/api/v1/sms/sessions/{sid(0)}')
    common_rate(r,services)
    require_session(r,'sms','sms-session',sid(0),services.tokens.issue('sms-session',sid(0)))
    bucket=services.codec.sha256('public.v2.sms.read.ip:203.0.113.10')
    assert services.counts[bucket]==1


@pytest.mark.parametrize('domain,scope',[('sms','sms-session')])
def test_public_routes_enforce_policy_without_replaying_business_calls(services,config,domain,scope):
    from fastapi.testclient import TestClient
    from smsinvit.app import create_app
    from smsinvit.sms.routes import router as sms_router
    calls=[]
    business=SimpleNamespace(poll=lambda id:calls.append(id) or {'id':id},redeem=lambda code:calls.append(code) or {'id':sid(0)})
    services.sms=business
    app=create_app(config,services.db,services=services,include_domains=False,load_runtime=False)
    app.include_router(sms_router)
    client=TestClient(app,client=('203.0.113.10',123))
    code='VALID-CARD';services.db.issue(domain,code,sid(0))
    result=client.post(f'/api/v1/{domain}/redeem',json={'code':code})
    assert result.status_code==200 and calls==[code]
    headers={'X-Session-Secret':services.tokens.issue(scope,sid(0))}
    for _ in range(240):
        assert client.get(f'/api/v1/{domain}/sessions/{sid(0)}',headers=headers).status_code==200
    assert calls==[code]+[sid(0)]*240
    owner=services.codec.sha256(f'public.v2.{domain}.read.owner:{sid(0)}')
    ip=services.codec.sha256(f'public.v2.{domain}.read.ip:203.0.113.10')
    assert services.counts[owner]==services.counts[ip]==240

"""Receipt recovery and failure semantics with synthetic mailboxes only."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from smsinvit.errors import Error
from smsinvit.invitation import mysql
from smsinvit.invitation.redeem_attempts import RedemptionAttemptMixin, RedemptionLockLost
from smsinvit.security import TokenService


class Database:
    def __init__(self):
        self.con = sqlite3.connect(':memory:', isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.depth = 0
        self.writes = 0
        self.lock_owner = None

    @contextmanager
    def transaction(self):
        outer = not self.depth
        if outer: self.con.execute('BEGIN')
        self.depth += 1
        try:
            yield self
        except BaseException:
            if outer: self.con.rollback()
            raise
        else:
            if outer: self.con.commit()
        finally:
            self.depth -= 1

    def execute(self, sql, *args):
        if not sql.lstrip().upper().startswith('SELECT'): self.writes += 1
        return self.con.execute(sql.replace(' FOR UPDATE', ''), args)

    def all(self, sql, *args): return [dict(row) for row in self.execute(sql, *args)]
    def one(self, sql, *args):
        rows = self.all(sql, *args)
        return rows[0] if rows else None
    def scalar(self, sql, *args):
        if sql == 'SELECT IS_USED_LOCK(?)': return self.lock_owner
        row = self.one(sql, *args)
        return next(iter(row.values())) if row else None


class FixtureService(RedemptionAttemptMixin):
    def __init__(self, db):
        self.db = db
        self.s = SimpleNamespace(codec=SimpleNamespace(fingerprint_key=b'isolated-recovery-key'),
                                 tokens=TokenService('isolated-token-key-over-32-characters'))
        self._mutex = threading.RLock()
        self.lost = False
        self.calls = []
        self.bind_failure = False
        self.repo = SimpleNamespace(get_cdk_code=lambda code:db.one('SELECT * FROM cards WHERE code=?', code),
            get_mail_settings=lambda:{'mail_source':'cf_temp'},
            claim_cdk_mailbox=self.claim_mail, set_cdk_status=self.set_card, add_cdk_event=lambda *a:None)
        # 拆分后取号 = 从「已注册 GPT」的号池里绑一个号，**不再现场造邮箱**。
        # 每次取号仍记一笔到 calls，语义等价于原来的"造了一个邮箱"，
        # 这样既有的"不重复取号/原子批"断言继续有效。
        self.alloc = SimpleNamespace(current_kind=lambda settings:'cf_temp',kind_is_pooled=lambda kind:True,
                                     pool_available=lambda settings=None:50,
                                     redeem_mailbox=self.redeem_mailbox)

    def redeem_mailbox(self, code, settings=None):
        self.calls.append(1)
        email = 'pool-' + str(code).lower() + '@example.test'
        if not self.claim_mail(code, 'outlook', pooled=True, email=email):
            raise RuntimeError('synthetic pool binding failure')
        self.set_card(code, 'bound', email=email)
        return {'email': email, 'kind': 'outlook', 'pooled': True}

    def _codes(self, codes):
        if not isinstance(codes, list) or not codes: raise Error('VALIDATION_ERROR','Invalid batch',422)
    def _require_inventory_available(self, *args): pass
    def _require_no_holds(self, *args): pass
    def _lock_codes(self, *args): pass
    def _mailbox_readiness(self, settings=None): return True, ''
    def page_content(self):
        return {'canRedeem':True,'canStart':False,
                'poolAvailable':self.alloc.pool_available(),
                'unavailableMessage':'服务暂时不可用，请稍后重试。'}
    def _view(self, sid, code):
        row = self.repo.get_cdk_code(code)
        return {'id':sid,'email':row['email'],'status':row['status'],'allowedActions':['POLL']}
    def set_card(self, code, status, **fields):
        self.db.execute('UPDATE cards SET status=?,email=? WHERE code=?',status,fields.get('email'),code)
    def claim_mail(self, code, kind, **kwargs):
        if self.bind_failure: raise RuntimeError('synthetic binding transaction failure')
        self.db.execute('INSERT INTO invitation_v2_outlook_accounts(email,cdk_code) VALUES (?,?)',kwargs['email'],mysql.marker(code))
        return True
    @contextmanager
    def _redemption_locks(self):
        service = self
        class Locks:
            def acquire(self, name): return True
            def require_owned(self):
                if service.lost: raise RedemptionLockLost('synthetic lock loss')
        self.db.lock_owner = 77
        try: yield Locks()
        finally: self.db.lock_owner = None


@pytest.fixture
def inv(monkeypatch):
    db = Database()
    db.con.executescript('''
      CREATE TABLE invitation_v2_redemption_attempts(attempt_id TEXT PRIMARY KEY,recovery_fingerprint TEXT,codes_fingerprint TEXT,status TEXT,owner_token TEXT,created_at REAL,updated_at REAL,result_json TEXT,error_code TEXT,error_message TEXT);
      CREATE TABLE invitation_v2_redemption_attempt_cards(attempt_id TEXT,code_fingerprint TEXT,PRIMARY KEY(attempt_id,code_fingerprint));
      CREATE TABLE invitation_v2_sessions(id TEXT PRIMARY KEY,code_id TEXT UNIQUE);
      CREATE TABLE invitation_v2_outlook_accounts(email TEXT PRIMARY KEY,cdk_code TEXT);
      CREATE TABLE cards(code TEXT PRIMARY KEY,status TEXT,email TEXT);
      INSERT INTO cards VALUES ('SYNTHETIC-CARD','unused',NULL);
    ''')
    service = FixtureService(db)
    values = {}
    def marker(value):
        result = 'iv2$' + hashlib.sha256(value.encode()).hexdigest()
        values[result] = value
        return result
    monkeypatch.setattr(mysql,'marker',marker)
    monkeypatch.setattr(mysql,'revealed',lambda value:values[value])
    import mail_providers
    def create(*args):
        def mailbox():
            service.calls.append(1)
            return 'synthetic-'+str(len(service.calls))+'@example.test'
        return SimpleNamespace(create_mailbox=mailbox)
    monkeypatch.setattr(mail_providers,'create_mail_provider',create)
    service.attempt = str(uuid4()); service.secret = 'a1'*32
    # 通知出口默认接一个记录型 outbox（与生产 Notifications.enqueue 同签名）。
    service.outbox = Outbox()
    service.s.outbox = service.outbox
    yield service
    db.con.close()


class Outbox:
    """Registers enqueue arguments exactly like the production notification outbox."""
    def __init__(self, chat='-100123'):
        self.chat = chat; self.calls = []
    def value(self, name):
        if name != 'telegram_ops_chat_id': raise ValueError('Unknown notification field')
        return self.chat
    def enqueue(self, event, channel, kind, reference, payload, expires):
        self.calls.append((event, channel, kind, reference, payload, expires))
        return 'notification-id'


def test_lost_response_recovers_original_capability_and_live_state_with_no_writes(inv):
    result=inv.redeem_attempt(['synthetic-card'],inv.attempt,inv.secret)
    original=result['items'][0]
    inv.db.execute("UPDATE cards SET status='done'")
    writes=inv.db.writes
    recovered=inv.redemption_attempt_status(inv.attempt,inv.secret)
    assert recovered['items'][0]['id']==original['id']
    assert recovered['items'][0]['sessionSecret']==original['sessionSecret']
    assert recovered['items'][0]['status']=='done' and inv.db.writes==writes and inv.calls==[1]
    assert recovered['canStart'] is False
    assert not {'capacity','mailboxReady','executorReady','executorUnavailableReason'} & recovered.keys()


def test_repeat_attempt_is_idempotent_and_receipt_never_stores_secrets_or_codes(inv):
    first=inv.redeem_attempt(['synthetic-card'],inv.attempt,inv.secret)
    assert inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)==first
    stored=json.dumps(inv.db.all('SELECT * FROM invitation_v2_redemption_attempts'))
    assert inv.secret not in stored and 'SYNTHETIC-CARD' not in stored and 'sessionSecret' not in stored
    assert inv.calls==[1]


@pytest.mark.parametrize('change,code',[('secret','INVITATION_ATTEMPT_SECRET_INVALID'),('codes','INVITATION_ATTEMPT_CONFLICT')])
def test_existing_attempt_rejects_changed_proof_before_returning_result(inv,change,code):
    inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    with pytest.raises(Error) as error:
        inv.redeem_attempt(['OTHER-CARD'] if change=='codes' else ['SYNTHETIC-CARD'],inv.attempt,'b2'*32 if change=='secret' else inv.secret)
    assert error.value.code==code and inv.calls==[1]


def test_recovery_wrong_secret_and_absent_id_are_indistinguishable(inv):
    inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    errors=[]
    for attempt,secret in [(inv.attempt,'ff'*32),(str(uuid4()),inv.secret)]:
        with pytest.raises(Error) as error:inv.redemption_attempt_status(attempt,secret)
        errors.append((error.value.code,error.value.message,error.value.status))
    assert errors[0]==errors[1] and errors[0][2]==404


def test_duplicate_and_blank_inputs_keep_first_original_error_line_and_atomic_batch(inv):
    result=inv.redeem_attempt(['SYNTHETIC-CARD','synthetic-card','','NO-SUCH-CARD'],inv.attempt,inv.secret)
    assert result['attemptStatus']=='rejected' and result['items'][0]['line']==4 and not inv.calls
    assert inv.repo.get_cdk_code('SYNTHETIC-CARD')['status']=='unused'


@pytest.mark.parametrize('kind,expected,code',[('configuration','rejected','INVITATION_MAIL_CONFIGURATION'),('connection','rejected','INVITATION_MAIL_CONNECTION'),('uncertain','review','INVITATION_REDEMPTION_REVIEW')])
def test_pool_binding_failure_classes_have_safe_persistent_outcomes(inv,monkeypatch,kind,expected,code):
    """取号失败的安全分类：明确失败→rejected（什么都没扣），结果不明→review。

    拆分后兑换不再有"现场造邮箱"这一步，上游创建类错误不会再从取号路径冒出来，
    但分类函数仍在（邀请链路、锁丢失等走同一条路），所以直接对注入点分类验证。
    """
    from mail_providers.cf_temp import MailboxConfigurationError,MailboxConnectionError,MailboxCreationUncertainError
    classes={'configuration':MailboxConfigurationError,'connection':MailboxConnectionError,'uncertain':MailboxCreationUncertainError}
    def fail(code,settings=None):raise classes[kind]('synthetic provider detail')
    monkeypatch.setattr(inv.alloc,'redeem_mailbox',fail)
    with pytest.raises(Error) as error:inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    assert error.value.code==code and 'synthetic provider detail' not in error.value.message
    assert inv.redemption_attempt_status(inv.attempt,inv.secret)['attemptStatus']==expected
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions')==0


@pytest.mark.parametrize('failure',['lock','binding'])
def test_lock_loss_or_pool_binding_failure_requires_review_and_blocks_new_attempt(inv,monkeypatch,failure):
    real=inv.alloc.redeem_mailbox
    def bind(code,settings=None):
        if failure=='lock':inv.lost=True
        else:inv.bind_failure=True
        return real(code,settings)
    monkeypatch.setattr(inv.alloc,'redeem_mailbox',bind)
    with pytest.raises(Error):inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    assert inv.redemption_attempt_status(inv.attempt,inv.secret)['attemptStatus']=='review'
    inv.lost=False;inv.bind_failure=False
    with pytest.raises(Error) as error:inv.redeem_attempt(['SYNTHETIC-CARD'],str(uuid4()),'cc'*32)
    assert error.value.code=='INVITATION_REDEMPTION_REVIEW' and inv.calls==[1]
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions')==0


def test_pending_owner_loss_is_derived_review_without_database_write(inv):
    proof=inv._recovery_fingerprint(inv.secret)
    inv._save_attempt(inv.attempt,proof,'0'*64,'pending',owner=str(uuid4()))
    writes=inv.db.writes
    assert inv.redemption_attempt_status(inv.attempt,inv.secret)['attemptStatus']=='review'
    assert inv.db.writes==writes and inv._attempt_row(inv.attempt)['status']=='pending'
    inv.db.lock_owner=77
    assert inv.redemption_attempt_status(inv.attempt,inv.secret)['attemptStatus']=='pending'


@pytest.mark.parametrize('failure',['configuration','connection'])
def test_partial_pool_binding_rolls_back_the_whole_batch(inv,monkeypatch,failure):
    """第二个号绑定失败时，第一个号也必须一起回滚 —— 整批原子，卡密不变。"""
    from mail_providers.cf_temp import MailboxConfigurationError,MailboxConnectionError
    inv.db.execute("INSERT INTO cards VALUES ('SECOND-CARD','unused',NULL)")
    real=inv.alloc.redeem_mailbox
    def bind(code,settings=None):
        if str(code).upper()=='SECOND-CARD':
            raise (MailboxConfigurationError if failure=='configuration' else MailboxConnectionError)('synthetic definite rejection')
        return real(code,settings)
    monkeypatch.setattr(inv.alloc,'redeem_mailbox',bind)
    with pytest.raises(Error):inv.redeem_attempt(['SYNTHETIC-CARD','SECOND-CARD'],inv.attempt,inv.secret)
    assert inv.redemption_attempt_status(inv.attempt,inv.secret)['attemptStatus']=='rejected'
    assert inv.db.scalar("SELECT COUNT(*) FROM cards WHERE status!='unused'")==0
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_outlook_accounts')==0
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions')==0


def test_card_changed_inside_the_locked_window_is_rejected_without_consuming(inv,monkeypatch):
    """首次读卡与锁内复核之间卡密被改动 → 整批拒绝，一张都没绑。

    _lock_codes 就在"锁内复核"之前执行，是那个窗口的准确注入点。
    """
    def lock_codes(markers):
        inv.db.execute("UPDATE cards SET status='bound', email='elsewhere@example.test'")
    monkeypatch.setattr(inv,'_lock_codes',lock_codes)
    result=inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    assert result['attemptStatus']=='rejected'
    # 卡密在锁内复核时已不是"未使用"，且号池里没有对应的绑定行 →
    # 归到绑定类错误（BINDING_UNAVAILABLE / BINDING_CHANGED），一样是整批拒绝。
    assert result['items'][0]['error']['code'] in {'INVITATION_BINDING_CHANGED','INVITATION_BINDING_UNAVAILABLE'}
    assert inv.calls==[] and inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_outlook_accounts')==0
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions')==0


def test_owner_completion_between_receipt_read_and_lock_probe_returns_completed(inv,monkeypatch):
    first=inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    completed=inv._attempt_row(inv.attempt)['result_json']
    inv.db.execute("UPDATE invitation_v2_redemption_attempts SET status='pending',owner_token=?,result_json=NULL",str(uuid4()))
    original=inv.db.scalar;commits=[]
    def owner_commit_at_probe(sql,*args):
        if sql=='SELECT IS_USED_LOCK(?)':
            commits.append(1)
            inv.db.execute("UPDATE invitation_v2_redemption_attempts SET status='completed',owner_token=NULL,result_json=?",completed)
            return None
        return original(sql,*args)
    monkeypatch.setattr(inv.db,'scalar',owner_commit_at_probe)
    writes=inv.db.writes
    recovered=inv.redemption_attempt_status(inv.attempt,inv.secret)
    assert recovered['attemptStatus']=='completed' and recovered['items']==first['items']
    assert commits==[1] and inv.db.writes==writes+1 and inv.calls==[1]


def test_lock_lost_before_binding_consumes_nothing(inv,monkeypatch):
    """抢号锁在绑定前就失效 → 结果按"待核查"处理，但一张都没绑、卡密不变。"""
    def lock_codes(markers):
        inv.lost=True
    monkeypatch.setattr(inv,'_lock_codes',lock_codes)
    with pytest.raises(Error) as error:inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    assert error.value.code=='INVITATION_REDEMPTION_REVIEW' and not inv.calls
    assert inv.redemption_attempt_status(inv.attempt,inv.secret)['attemptStatus']=='review'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions')==0
    assert inv.repo.get_cdk_code('SYNTHETIC-CARD')['status']=='unused'


def test_lock_lost_during_binding_is_classified_as_review_not_rejected(inv,monkeypatch):
    """锁在邮箱绑定阶段失效时，必须判成"待核查"，不能被原始异常降级成"已拒绝"。

    这是 `except` 里那次 `require_owned()` 真正要拦的窗口：它在事务内的两次锁检查之间
    （绑定前 384 行、写回前 418 行）出现，所以只有异常处理里的这次检查能看到。原始异常
    本身是个 `Error`，若用它分类会被归成 rejected——提示用户"直接重试"，而锁其实已经
    不在手上，成交与否无法确定。

    这里用号池绑定抛连接错误来制造"业务异常 + 锁同时失效"的交错。
    """
    from mail_providers.cf_temp import MailboxConnectionError

    real_require = inv._redemption_locks
    state = {'lost': False}

    @contextmanager
    def locks():
        with real_require() as inner:
            original = inner.require_owned
            def require_owned():
                if state['lost']:
                    raise RedemptionLockLost('synthetic lock loss during binding')
                return original()
            inner.require_owned = require_owned
            yield inner

    monkeypatch.setattr(inv, '_redemption_locks', locks)

    def redeem_mailbox(code, settings=None):
        # 绑定过程中连接被换掉：业务抛错，同时锁失效。
        state['lost'] = True
        raise MailboxConnectionError('synthetic mailbox connection failure')

    monkeypatch.setattr(inv.alloc, 'redeem_mailbox', redeem_mailbox)

    with pytest.raises(Error) as error:
        inv.redeem_attempt(['SYNTHETIC-CARD'], inv.attempt, inv.secret)

    assert error.value.code == 'INVITATION_REDEMPTION_REVIEW'
    assert inv.redemption_attempt_status(inv.attempt, inv.secret)['attemptStatus'] == 'review'
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions') == 0
    assert inv.repo.get_cdk_code('SYNTHETIC-CARD')['status'] == 'unused'


@pytest.mark.parametrize('status',['bound','queued','running','done','failed','review'])
def test_new_attempt_for_used_card_reuses_original_email_id_capability_and_state(inv,status):
    first=inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)['items'][0]
    inv.db.execute('UPDATE cards SET status=?',status)
    before=inv.repo.get_cdk_code('SYNTHETIC-CARD')
    restored=inv.redeem_attempt(['SYNTHETIC-CARD'],str(uuid4()),'bb'*32)['items'][0]
    assert restored['id']==first['id'] and restored['sessionSecret']==first['sessionSecret']
    assert restored['email']==first['email'] and restored['status']==status
    assert inv.repo.get_cdk_code('SYNTHETIC-CARD')==before and inv.calls==[1]
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions')==1


def test_historical_used_card_without_session_creates_one_capability_without_new_mail(inv):
    inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    inv.db.execute('DELETE FROM invitation_v2_sessions')
    inv.db.execute("UPDATE cards SET status='done'")
    before=inv.repo.get_cdk_code('SYNTHETIC-CARD')
    recovered=inv.redeem_attempt(['SYNTHETIC-CARD'],str(uuid4()),'bb'*32)
    assert recovered['items'][0]['status']=='done' and recovered['items'][0]['sessionSecret']
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions')==1
    assert inv.repo.get_cdk_code('SYNTHETIC-CARD')==before and inv.calls==[1]


@pytest.mark.parametrize('used_first',[False,True])
def test_mixed_used_and_new_only_creates_new_subset_and_preserves_result_order(inv,used_first):
    first=inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)['items'][0]
    inv.db.execute("INSERT INTO cards VALUES ('NEW-CARD','unused',NULL)")
    codes=['SYNTHETIC-CARD','NEW-CARD'] if used_first else ['NEW-CARD','SYNTHETIC-CARD']
    result=inv.redeem_attempt(codes,str(uuid4()),'bb'*32)
    old=result['items'][0 if used_first else 1]
    new=result['items'][1 if used_first else 0]
    assert old['id']==first['id'] and old['sessionSecret']==first['sessionSecret']
    assert new['email']=='pool-new-card@example.test' and old['email']=='pool-synthetic-card@example.test'
    assert [row['line'] for row in result['items']]==[1,2] and len(inv.calls)==2
    assert inv.db.scalar('SELECT COUNT(*) FROM invitation_v2_sessions')==2


def test_mixed_invalid_batch_returns_no_capabilities_and_allocates_nothing(inv):
    inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)
    inv.db.execute("INSERT INTO cards VALUES ('NEW-CARD','unused',NULL)")
    result=inv.redeem_attempt(['SYNTHETIC-CARD','NEW-CARD','NO-SUCH-CARD'],str(uuid4()),'bb'*32)
    assert result['attemptStatus']=='rejected' and result['items'][0]['line']==3
    assert all('sessionSecret' not in row and 'email' not in row for row in result['items'])
    assert inv.repo.get_cdk_code('NEW-CARD')['status']=='unused' and inv.calls==[1]


def test_used_card_is_queryable_when_new_mail_is_unavailable(inv,monkeypatch):
    first=inv.redeem_attempt(['SYNTHETIC-CARD'],inv.attempt,inv.secret)['items'][0]
    monkeypatch.setattr(inv,'_mailbox_readiness',lambda *a:(_ for _ in ()).throw(AssertionError('Used cards must not preflight creation')))
    result=inv.redeem_attempt(['SYNTHETIC-CARD'],str(uuid4()),'bb'*32)
    assert result['items'][0]['id']==first['id'] and inv.calls==[1]


# ──────────────────────── 账号池（池化来源） ────────────────────────


def add_cards(inv, *codes):
    for code in codes:
        inv.db.execute("INSERT INTO cards VALUES (?, 'unused', NULL)", code)


def as_pool(inv, available):
    """把 fixture 切成池化来源：可兑换账号数由 available 决定。"""
    inv.repo.get_mail_settings = lambda: {'mail_source': 'outlook'}

    def bind(code):
        email = 'pool-' + code.lower() + '@example.test'
        inv.db.execute('INSERT INTO invitation_v2_outlook_accounts(email,cdk_code) VALUES (?,?)',
                       email, mysql.marker(code))
        inv.set_card(code, 'bound', email=email)
        return {'email': email, 'kind': 'outlook', 'pooled': True}

    inv.alloc = SimpleNamespace(current_kind=lambda settings=None: 'outlook',
                                kind_is_pooled=lambda kind: True,
                                pool_available=lambda settings=None: available,
                                redeem_mailbox=lambda code, settings=None: bind(code))
    return inv


def bound_rows(inv):
    return inv.db.all('SELECT * FROM invitation_v2_outlook_accounts')


def test_pool_with_enough_accounts_binds_every_card_and_reports_the_count(inv):
    add_cards(inv, 'CARD-A', 'CARD-B')
    as_pool(inv, 2)
    result = inv.redeem_attempt(['CARD-A', 'CARD-B'], inv.attempt, inv.secret)
    assert result['attemptStatus'] == 'completed' and result['poolAvailable'] == 2
    assert {item['email'] for item in result['items']} == {
        'pool-card-a@example.test', 'pool-card-b@example.test'}
    assert len(bound_rows(inv)) == 2


def test_pool_shortage_rejects_the_whole_batch_and_consumes_nothing(inv):
    add_cards(inv, 'CARD-A', 'CARD-B')
    as_pool(inv, 1)
    result = inv.redeem_attempt(['CARD-A', 'CARD-B'], inv.attempt, inv.secret)
    assert result['attemptStatus'] == 'rejected'
    assert result['poolAvailable'] == 1 and result['poolRequested'] == 2
    assert {item['error']['code'] for item in result['items']} == {'INVITATION_POOL_INSUFFICIENT'}
    assert {item['line'] for item in result['items']} == {1, 2}
    # 整批原子：一张都没绑、两张卡密都还是未使用，用户可以只兑换一部分再来
    assert bound_rows(inv) == []
    assert [inv.repo.get_cdk_code(code)['status'] for code in ('CARD-A', 'CARD-B')] == ['unused', 'unused']


def test_pool_shortage_recovery_reports_the_same_rejection_without_binding(inv):
    add_cards(inv, 'CARD-A', 'CARD-B')
    as_pool(inv, 1)
    inv.redeem_attempt(['CARD-A', 'CARD-B'], inv.attempt, inv.secret)
    recovered = inv.redemption_attempt_status(inv.attempt, inv.secret)
    assert recovered['attemptStatus'] == 'rejected' and recovered['poolAvailable'] == 1
    assert bound_rows(inv) == []
    # 用同一批卡密重复提交拿到同样的收据，绝不再绑一次
    repeated = inv.redeem_attempt(['CARD-A', 'CARD-B'], inv.attempt, inv.secret)
    assert repeated['attemptStatus'] == 'rejected' and bound_rows(inv) == []


def test_empty_pool_keeps_the_existing_retryable_error_and_binds_nothing(inv):
    as_pool(inv, 0)
    with pytest.raises(Error) as error:
        inv.redeem_attempt(['synthetic-card'], inv.attempt, inv.secret)
    assert error.value.code == 'INVITATION_MAIL_INSUFFICIENT' and error.value.status == 409
    assert inv.repo.get_cdk_code('SYNTHETIC-CARD')['status'] == 'unused'
    assert bound_rows(inv) == []
    assert inv.redemption_attempt_status(inv.attempt, inv.secret)['attemptStatus'] == 'rejected'


def test_partial_retry_after_a_shortage_only_takes_what_the_pool_has(inv):
    """用户确认"继续"时前端只提交前 N 张 —— 服务端要能正常完成这一小批。"""
    add_cards(inv, 'CARD-A', 'CARD-B')
    as_pool(inv, 1)
    inv.attempt = str(uuid4())
    result = inv.redeem_attempt(['CARD-A'], inv.attempt, inv.secret)
    assert result['attemptStatus'] == 'completed'
    assert inv.repo.get_cdk_code('CARD-A')['status'] == 'bound'
    assert inv.repo.get_cdk_code('CARD-B')['status'] == 'unused'


def test_real_readiness_is_about_the_pool_not_the_source(inv):
    """号池可以混放多种来源（补号来源可单独配），所以 readiness 只问池里有没有号。"""
    from smsinvit.invitation.redeem_attempts import RedemptionAttemptMixin
    for kind in ('cf_temp', 'outlook'):
        service = SimpleNamespace(alloc=SimpleNamespace(pool_available=lambda settings=None: 5,
                                                        current_kind=lambda settings=None, k=kind: k,
                                                        kind_is_pooled=lambda value: False))
        assert RedemptionAttemptMixin._mailbox_readiness(service) == (True, '')
    empty = SimpleNamespace(alloc=SimpleNamespace(pool_available=lambda settings=None: 0))
    ready, reason = RedemptionAttemptMixin._mailbox_readiness(empty)
    assert ready is False and '不足' in reason


def test_real_readiness_reports_an_empty_pool(inv):
    from smsinvit.invitation.redeem_attempts import RedemptionAttemptMixin
    service = SimpleNamespace(
        repo=SimpleNamespace(get_mail_settings=lambda: {'mail_source': 'outlook'}),
        alloc=SimpleNamespace(current_kind=lambda settings=None: 'outlook',
                              kind_is_pooled=lambda kind: True,
                              pool_available=lambda settings=None: 0))
    ready, reason = RedemptionAttemptMixin._mailbox_readiness(service)
    assert ready is False and '不足' in reason


def test_mailbox_binding_does_not_emit_completion_notification(inv):
    """领取只绑定邮箱；自动或手动授权写 done 时才产生完成通知。"""
    inv.db.execute("UPDATE cards SET status='unused'")
    result = inv.redeem_attempt(['SYNTHETIC-CARD'], inv.attempt, inv.secret)
    assert result['attemptStatus'] == 'completed'
    assert inv.repo.get_cdk_code('SYNTHETIC-CARD')['status'] == 'bound'
    assert inv.outbox.calls == []


def test_replay_and_rejected_batches_never_resend_redeemed_notifications(inv):
    """重放领取请求、整批拒绝都不能提前推送核销完成。"""
    inv.redeem_attempt(['SYNTHETIC-CARD'], inv.attempt, inv.secret)
    assert inv.outbox.calls == []
    inv.redeem_attempt(['synthetic-card'], inv.attempt, inv.secret)
    assert inv.outbox.calls == [], 'same receipt replay must stay silent'
    inv.db.execute("INSERT INTO cards VALUES ('REJECT-CARD','bound','taken@example.test')")
    rejected = inv.redeem_attempt(['REJECT-CARD'], str(uuid4()), 'dd'*32)
    assert rejected['attemptStatus'] != 'completed'
    assert inv.outbox.calls == [], 'nothing completed, so nothing may be notified'


def test_unconfigured_telegram_chat_never_breaks_redemption(inv):
    """未保存运营群 Chat ID：不写 outbox，领取结果与卡密状态照常提交。"""
    inv.outbox.chat = ''
    result = inv.redeem_attempt(['SYNTHETIC-CARD'], inv.attempt, inv.secret)
    assert result['attemptStatus'] == 'completed' and inv.outbox.calls == []
    assert inv.repo.get_cdk_code('SYNTHETIC-CARD')['status'] == 'bound'

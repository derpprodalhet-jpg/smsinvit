"""Persistent invitation admission controls; no threads or external operations."""
from contextlib import contextmanager
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest
from smsinvit.errors import Error
from smsinvit.invitation.service import InvitationService


@pytest.fixture
def executor():
    settings={};audits=[]
    state=SimpleNamespace(settings=settings,audits=audits,fail_save=False,fail_audit=False,fail_commit=False)
    @contextmanager
    def transaction():
        old=settings.copy();before=len(audits)
        try:
            yield
            if state.fail_commit:raise RuntimeError('synthetic database commit failed')
        except Exception:
            settings.clear();settings.update(old);del audits[before:]
            raise
    def save(key,value):
        if state.fail_save:raise RuntimeError('synthetic secret database detail')
        settings[key]=value
    def audit(*args):
        if state.fail_audit:raise RuntimeError('synthetic secret audit detail')
        audits.append(args)
    inv=InvitationService.__new__(InvitationService)
    inv._executor_lock=threading.RLock();inv._executor_enabled=False;inv._started=False;inv._stopping=False
    # 准入要求"仍持执行器排他锁"；这里给一个恒为真的假连接（真实判定在
    # test_invitation_permits.py 里对真 MySQL 跑）。
    inv._process_lock=SimpleNamespace(exec_driver_sql=lambda sql:SimpleNamespace(scalar=lambda:7))
    inv._process_lock_owner=7
    inv.db=SimpleNamespace(transaction=transaction)
    inv.repo=SimpleNamespace(get_setting=lambda key,default=None:settings.get(key,default),set_setting=save,
                             get_mail_settings=lambda:{'mail_source':'outlook'})
    inv.s=SimpleNamespace(audit=SimpleNamespace(record=audit))
    inv._execution_self_check=Mock();inv.gate=SimpleNamespace(close=Mock());inv.stop=Mock()
    def start(*,background):
        assert background is False
        if not inv._started:inv._execution_self_check()
        inv._started=True
    inv.start=Mock(side_effect=start)
    inv.cdk=SimpleNamespace(CONTROLLER=SimpleNamespace(worker_alive=lambda:1,ensure_workers=lambda:1))
    inv.proxy=SimpleNamespace(has_available=lambda:True)
    state.inv=inv;state.actor=SimpleNamespace(id='synthetic-actor')
    return state


@pytest.mark.parametrize('stored,default,expected',[(None,False,False),(None,True,True),('false',True,False),('true',False,True)])
def test_persistent_setting_overrides_initial_private_flag(executor,stored,default,expected):
    x=executor
    if stored is not None:x.settings['invitation_executor_enabled']=stored
    assert x.inv.initialize_executor_enabled(default) is expected
    assert x.inv._executor_enabled is expected and not x.audits


def test_enable_is_persisted_and_audited_then_admits_explicit_requests(executor):
    x=executor;result=x.inv.set_executor_enabled(True,x.actor)
    assert result=={'enabled':True,'ready':True,'state':'ready','message':'已开启，可在前台开始执行。'}
    x.inv.start.assert_called_once_with(background=False);x.inv._execution_self_check.assert_called_once()
    assert x.settings['invitation_executor_enabled']=='true'
    assert x.audits==[('synthetic-actor','invitation.executor.update','invitation_executor',None,{'enabled':True})]
    x.inv.require_executor_accepting()
    assert x.inv.initialize_executor_enabled(False) is True


def test_disable_keeps_queued_and_running_workers_alive(executor):
    x=executor;x.inv._started=True;x.inv._executor_enabled=True
    queued_and_running=['synthetic-queued','synthetic-running']
    x.inv.cdk.CONTROLLER._q=queued_and_running
    result=x.inv.set_executor_enabled(False,x.actor)
    assert result['state']=='disabled' and not result['ready'] and x.inv._started
    assert x.inv.cdk.CONTROLLER._q==queued_and_running
    x.inv.start.assert_not_called();x.inv.stop.assert_not_called();x.inv.gate.close.assert_not_called()
    with pytest.raises(Error) as error:x.inv.require_executor_accepting()
    assert error.value.code=='INVITATION_EXECUTOR_UNAVAILABLE'
    assert x.settings['invitation_executor_enabled']=='false'


def test_enabling_existing_workers_reruns_local_preflight_without_other_background(executor):
    x=executor;x.inv._started=True
    x.inv.set_executor_enabled(True,x.actor)
    x.inv._execution_self_check.assert_called_once();x.inv.start.assert_called_once_with(background=False)


def test_start_failure_does_not_save_or_expose_details(executor):
    x=executor;x.inv.start.side_effect=RuntimeError('synthetic-private-proxy-password')
    with pytest.raises(Error) as error:x.inv.set_executor_enabled(True,x.actor)
    assert error.value.code=='INVITATION_EXECUTOR_START_FAILED' and error.value.status==503
    assert 'synthetic-private' not in error.value.message and not x.settings and not x.audits
    assert not x.inv._executor_enabled


@pytest.mark.parametrize('failure',['fail_save','fail_audit','fail_commit'])
def test_failed_save_or_audit_does_not_claim_to_enable(executor,failure):
    x=executor;setattr(x,failure,True)
    with pytest.raises(Error) as error:x.inv.set_executor_enabled(True,x.actor)
    assert error.value.code=='INVITATION_EXECUTOR_SAVE_FAILED'
    assert not x.inv._executor_enabled and not x.settings and not x.audits
    with pytest.raises(Error):x.inv.require_executor_accepting()


def test_failed_disable_preserves_previous_saved_and_live_admission(executor):
    x=executor;x.inv._started=True;x.inv._executor_enabled=True;x.settings['invitation_executor_enabled']='true'
    x.fail_commit=True
    with pytest.raises(Error):x.inv.set_executor_enabled(False,x.actor)
    assert x.inv._executor_enabled and x.settings['invitation_executor_enabled']=='true'
    x.inv.require_executor_accepting()


def test_admin_status_explains_empty_proxy_pool_without_addresses(executor):
    x=executor;x.inv._started=True;x.inv._executor_enabled=True;x.inv.proxy.has_available=lambda:False
    result=x.inv.executor_status()
    assert result=={'enabled':True,'ready':False,'state':'unavailable','message':'已开启，但代理池暂无可用代理，请添加有效代理。'}

def test_public_availability_reasons_hide_administrator_conditions(executor):
    x=executor;x.inv.cdk.CONTROLLER.stats=lambda:{'max_concurrency':3,'worker_alive':0}
    x.inv._mailbox_readiness=lambda *args:(False,'synthetic private configuration problem')
    x.inv.alloc=SimpleNamespace(pool_available=lambda *args:0,current_kind=lambda *args:'outlook',kind_is_pooled=lambda kind:True)
    result=x.inv.page_content()
    assert result['unavailableMessage']=='服务暂时不可用，请稍后重试。'
    assert not result['canStart'] and not result['canRedeem']
    assert not {'capacity','executorReady','mailboxReady','executorUnavailableReason','mailboxUnavailableReason'} & result.keys()
    assert result['poolAvailable']==0


def test_redeemable_count_is_the_pool_size_regardless_of_source(executor):
    """可兑换账号 = 已注册 GPT、尚未 Codex 授权、未被占用的号，数量永远是实数。

    号池可以混放多种来源（补号来源可与兑换来源不同），所以计数**不能**按
    "当前配置的来源是否池化"来决定 —— 那会让 CF 现造地址入池的号数不出来，
    出现"可兑但显示 0"的矛盾。负数（旧的"不限量"语义）一律按 0。
    """
    x=executor;x.inv.cdk.CONTROLLER.stats=lambda:{'max_concurrency':3,'worker_alive':1}
    x.inv._mailbox_readiness=lambda *args:(True,'')
    for kind,pooled,count in (('cf_temp',False,2),('outlook',True,2)):
        x.inv.alloc=SimpleNamespace(pool_available=lambda *args,c=count:c,
                                    current_kind=lambda *args,k=kind:k,
                                    kind_is_pooled=lambda value,p=pooled:p)
        result=x.inv.page_content()
        assert result['poolAvailable']==count and result['canRedeem'] is True
    x.inv.alloc=SimpleNamespace(pool_available=lambda *args:-1,current_kind=lambda *args:'cf_temp',
                                kind_is_pooled=lambda kind:False)
    assert x.inv.page_content()['poolAvailable']==0


@pytest.mark.parametrize('value',['true',1,None])
def test_set_requires_real_boolean(executor,value):
    x=executor
    with pytest.raises(Error):x.inv.set_executor_enabled(value,x.actor)
    assert not x.settings and not x.audits and not x.inv.start.called

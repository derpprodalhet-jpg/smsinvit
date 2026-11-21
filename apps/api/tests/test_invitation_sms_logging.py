"""接码日志必须写出真实号码与验证码（否则出问题只能靠猜）。"""
import logging

import pytest
from types import SimpleNamespace

from smsinvit.invitation.runtime import TASK_SETTINGS
from sms_provider import PhoneCallbackController, _otp_logging_enabled


def controller(code="123456", phone="+15550001234"):
    ctrl = PhoneCallbackController.__new__(PhoneCallbackController)
    ctrl.provider = SimpleNamespace(get_code=lambda aid, timeout=0: code)
    ctrl.activation = SimpleNamespace(activation_id="act-1", phone_number=phone)
    ctrl._code_delivered = False
    ctrl.logs = []
    ctrl.log = ctrl.logs.append
    return ctrl


def test_received_code_is_logged_with_the_real_phone_number():
    ctrl = controller()
    assert ctrl.get_code(timeout=1) == "123456"
    assert ctrl.logs == ["已收到验证码：123456（号码=+15550001234）"]


def test_code_is_only_taken_once_and_logged_once():
    ctrl = controller()
    ctrl.get_code(timeout=1)
    assert ctrl.get_code(timeout=1) == ""
    assert len(ctrl.logs) == 1


def test_empty_code_is_not_logged():
    ctrl = controller(code="")
    assert ctrl.get_code(timeout=1) == ""
    assert ctrl.logs == []


def test_otp_logging_can_be_switched_off_by_the_task_setting():
    ctrl = controller()
    token = TASK_SETTINGS.set({"sms_log_otp": "0"})
    try:
        assert _otp_logging_enabled() is False
        ctrl.get_code(timeout=1)
        assert ctrl.logs == []
    finally:
        TASK_SETTINGS.reset(token)


def test_otp_logging_defaults_to_on_without_task_settings():
    assert _otp_logging_enabled() is True
    for value in ("1", "true", "on", "yes"):
        token = TASK_SETTINGS.set({"sms_log_otp": value})
        try:
            assert _otp_logging_enabled() is True
        finally:
            TASK_SETTINGS.reset(token)


def test_rental_log_prints_the_real_number():
    """租号那行必须带真实号码（这是对不上号时唯一的线索）。"""
    import inspect

    source = inspect.getsource(PhoneCallbackController.get_phone)
    assert "已租到新号码" in source and "号码=" in source


# ──────────────────────── 失败原因必须写出来 + 失败即止损 ────────────────────────


class FakeSmsController:
    def __init__(self):
        self.rented = 0
        self.cleaned = 0
        self.completed = False
        self.failed_reason = ""
        self.activation = SimpleNamespace(phone_number="+12025550123")

    def get_phone(self):
        self.rented += 1
        return "+12025550123"

    def mark_send_succeeded(self):
        pass

    def get_code(self, timeout=80):
        return "123456"

    def report_success(self):
        self.completed = True

    def mark_code_failed(self, reason):
        self.failed_reason = reason

    def cleanup(self):
        self.cleaned += 1
        return True


def sms_flow(exc):
    from auth_flow import AuthFlow

    flow = AuthFlow.__new__(AuthFlow)
    flow._sms_callback = FakeSmsController()
    flow._emit_stage = lambda name: None

    def send(_phone):
        raise exc

    flow._add_phone_send = send
    return flow


def test_sms_failure_logs_the_reason_not_just_the_class(caplog):
    """主人看到的必须是原因，不是"RuntimeError"。"""
    flow = sms_flow(RuntimeError("Your sign-in session is no longer valid（code=invalid_session, HTTP 409）"))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError):
            flow._do_sms_loop(flow._sms_callback)
    text = caplog.text
    assert "Your sign-in session is no longer valid" in text
    assert "HTTP 409" in text and "号码=+12025550123" in text


def test_session_invalid_stops_instead_of_burning_more_numbers():
    """会话失效换号也没用：必须第一次就停（以前会连租 3 个号，白烧预算）。"""
    flow = sms_flow(RuntimeError("Your sign-in session is no longer valid"))
    with pytest.raises(RuntimeError):
        flow._do_sms_loop(flow._sms_callback)
    assert flow._sms_callback.rented == 1        # 只租了一次
    assert flow._sms_callback.cleaned == 1       # 但要先把号退掉


def test_ordinary_failure_still_retries_with_another_number():
    """普通失败（号码收不到码）仍要换号重试 —— 止损只针对"换号无用"的错误。"""
    flow = sms_flow(RuntimeError("不会走到这里"))   # _add_phone_send 正常返回
    flow._add_phone_send = lambda _phone: {"ok": True}
    flow._extract_page_type = lambda _step: "phone_otp_verification"
    flow._extract_continue_url_from_step = lambda _step: ""
    flow._normalize_continue_url = lambda value: value

    def no_code(timeout=80):
        raise TimeoutError("号码在 80 秒内未收到验证码")

    flow._sms_callback.get_code = no_code
    with pytest.raises(TimeoutError):
        flow._do_sms_loop(flow._sms_callback)
    # 真循环会换 3 个号；每个都退号清理
    assert flow._sms_callback.rented == 3
    assert flow._sms_callback.cleaned == 3


def successful_sms_flow():
    flow = sms_flow(RuntimeError('unused'))
    flow._add_phone_send = lambda phone: {'page': 'phone_otp_verification'}
    flow._extract_page_type = lambda step: step.get('page', '')
    flow._extract_continue_url_from_step = lambda step: step.get('continue_url', '')
    flow._normalize_continue_url = lambda value: value
    flow._phone_otp_validate = lambda code: {'continue_url': 'https://auth.openai.com/next'}
    return flow


def test_validated_phone_evidence_survives_controller_cleanup():
    flow = successful_sms_flow()
    assert flow._handle_add_phone_via_sms() == 'https://auth.openai.com/next'
    assert flow._phone_bound_ok is True
    assert (flow._phone_number, flow._phone_code) == ('+12025550123', '123456')
    assert flow._sms_callback.cleaned == 1


def test_rejected_code_never_becomes_successful_phone_evidence():
    flow = successful_sms_flow()
    flow._phone_otp_validate = lambda code: (_ for _ in ()).throw(RuntimeError('rejected'))
    with pytest.raises(RuntimeError, match='rejected'):
        flow._do_sms_loop(flow._sms_callback)
    assert getattr(flow, '_phone_bound_ok', False) is False
    assert not getattr(flow, '_phone_number', '') and not getattr(flow, '_phone_code', '')

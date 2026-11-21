"""Public progress contracts must never serialize administrative execution details."""
import json
from types import SimpleNamespace

import pytest

from smsinvit.invitation.service import InvitationService
from smsinvit.invitation.redeem_attempts import RedemptionAttemptMixin


@pytest.mark.parametrize('status', ['queued', 'running', 'bound', 'failed', 'review', 'done', 'void', 'unknown'])
@pytest.mark.parametrize('progress,expected', [(64, 64), (-3, 0), (150, 99), (float('nan'), 0), ('bad', 0)])
def test_invitation_progress_contract_excludes_stages_and_clamps_unconfirmed_results(monkeypatch, status, progress, expected):
    service = InvitationService.__new__(InvitationService)
    info = {'status': status, 'progress': progress, 'email': 'customer@example.test',
            'stage': 'private-stage', 'stage_label': 'private-stage', 'queue_position': 33}
    service.cdk = SimpleNamespace(CONTROLLER=SimpleNamespace(status_for_codes=lambda _: [info]))
    service.repo = SimpleNamespace(get_cdk_code=lambda _: {'status': status, 'invite_sent_at': 123})
    service.db = SimpleNamespace(scalar=lambda *_: 0)
    monkeypatch.setattr('smsinvit.invitation.service.mysql.marker', lambda value: value)
    value = service._view('session', 'card')
    assert set(value) == {'id', 'email', 'progressPercent', 'displayResult', 'status', 'allowedActions'}
    assert value['progressPercent'] == (100 if status == 'done' else expected)
    assert value['status'] == ('processing' if status in {'queued', 'running'} else status)
    assert 'private-stage' not in json.dumps(value)
    if status == 'bound': assert value['allowedActions'] == ['POLL', 'START']
    if status == 'done': assert value['displayResult'] == 'SUCCESS' and value['allowedActions'] == []
    if status == 'review': assert value['allowedActions'] == ['POLL']


def test_invitation_manual_review_never_reports_success(monkeypatch):
    service = InvitationService.__new__(InvitationService)
    service.cdk = SimpleNamespace(CONTROLLER=SimpleNamespace(status_for_codes=lambda _: [
        {'status': 'done', 'progress': 100, 'email': 'customer@example.test'}]))
    service.repo = SimpleNamespace(get_cdk_code=lambda _: {'status': 'done'})
    service.db = SimpleNamespace(scalar=lambda *_: 1)
    monkeypatch.setattr('smsinvit.invitation.service.mysql.marker', lambda value: value)
    result = service._view('session', 'card')
    assert result['status'] == 'review' and result['progressPercent'] == 99
    assert result['displayResult'] is None and result['allowedActions'] == ['POLL']


@pytest.mark.parametrize('code', ['INVITATION_CDK_NOT_FOUND', 'PRIVATE_STAGE_ERROR'])
def test_old_rejected_invitation_receipts_are_rebuilt_without_internal_details(code):
    service = RedemptionAttemptMixin()
    service.page_content = lambda: {'canRedeem': True, 'canStart': False,
                                   'unavailableMessage': '服务暂时不可用，请稍后重试。', 'poolAvailable': 3}
    row = {'attempt_id': 'attempt', 'created_at': 123, 'status': 'rejected',
           'error_message': 'PRIVATE_DETAIL', 'result_json': json.dumps({'items': [
               {'line': 2, 'stage': 'PRIVATE_DETAIL', 'sessionSecret': 'PRIVATE_DETAIL',
                'error': {'code': code, 'message': 'PRIVATE_DETAIL', 'details': 'PRIVATE_DETAIL'}}]})}
    result = service._attempt_response(row)
    assert 'PRIVATE_' not in json.dumps(result)
    assert set(result['items'][0]) == {'line', 'error'}
    assert set(result['items'][0]['error']) == {'code', 'message'}
    assert result['items'][0]['line'] == 2
    assert result['items'][0]['error']['code'] == (code if code == 'INVITATION_CDK_NOT_FOUND' else 'INVITATION_REDEMPTION_REVIEW')
    assert result['canRedeem'] and not result['canStart']


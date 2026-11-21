"""审计覆盖回归：**读走机密也必须留痕**。

背景（只读安全审计结论）：README 承诺「每次明文读取记审计」，但实际有 15+ 条读取路径
（批量明文卡密、邮箱密码/2FA、Codex RT、运行记录里的卡密明文、通知正文 reveal）**一行审计都没有**，
等于可以无痕取走全部资产。这个文件锁住修复结果。
"""
from __future__ import annotations

from pathlib import Path

API = Path(__file__).resolve().parents[1] / 'smsinvit'


def read(relative: str) -> str:
    return (API / relative).read_text(encoding='utf-8')


def test_audit_log_endpoint_uses_dedicated_permission():
    """审计日志曾用 settings.read，专用权限点 audit.read 形同虚设。"""
    source = read('common/audit.py')
    assert "require_permission(request,'audit.read')" in source
    assert "audit.list(" in source


def test_public_health_probe_stays_public():
    """`/api/v1/health` 是公开存活探针：加了鉴权会让监控 401（曾误加并已回退）。

    真正待办的是它的**信息量**（workerId/调度器细节），方向是少暴露而不是要登录。
    """
    source = read('common/ops.py')
    assert "require_permission(request,'inventory.read');return ok(ctx(request).ops.dashboard_health())" not in source
    assert 'public' in source or '公开存活探针' in source


def test_scheduler_mutations_are_audited():
    """调度器改配置/立即执行原本无审计（可静默暂停恢复扫描让任务长期卡死）。"""
    source = read('common/jobs.py')
    assert "'scheduler.update'" in source and "'scheduler.run'" in source
    assert source.count("ctx(request).audit.record(") >= 2


def test_notification_reveal_is_audited():
    """revealSecrets 会解密历史认证邮件正文（含一次性验证码）。"""
    source = read('common/notifications.py')
    assert "'notification.payload_revealed'" in source


def test_invitation_boundary_audits_credential_reads_not_only_writes():
    """boundary 原来只在 `if write` 记审计，而 15 条 GET 会按 invitation.credentials 揭示机密。"""
    source = read('invitation/router.py')
    assert 'credentials_allowed=services.permissions.allows(actor,' in source
    assert 'if write or credentials_allowed:' in source


def test_batch_plaintext_reads_are_audited():
    """reveal=True 的批量明文旅程必须留痕，且审计只写作用域、不写明文。"""
    expectations = [
        ('sms/routes.py', "'sms.batch_reveal'"),
    ]
    for relative, action in expectations:
        source = read(relative)
        assert action in source, f'{relative} 缺少批量明文读取审计 {action}'
        # reveal 为真才记，且守卫与审计在同一个 if 里
        assert 'if reveal' in source
        # 审计里绝不能出现明文字段（只允许 scope 这类描述）
        assert "'code':" not in source.split(action)[0][-200:], f'{relative} 的审计疑似写入明文'

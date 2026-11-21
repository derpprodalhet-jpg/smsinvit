import { LockOutlined, SafetyCertificateOutlined, UserOutlined } from '@ant-design/icons';
import { LoginForm, ProFormText } from '@ant-design/pro-components';
import { Button, Space, Typography } from 'antd';
import { useRouter } from '../../lib/navigation';
import { useState } from 'react';
import { AdminBotGuard, type AdminBotPayload } from '../../components/admin-bot-guard';
import { AdminErrorAlert } from '../../components/admin-feedback';
import { useAdminAuth } from '../../lib/admin-auth-context';
import { adminLogin, adminLoginTotp } from '../../lib/auth';

export default function AdminLoginPage() {
  const router = useRouter();
  const { setUser } = useAdminAuth();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [challengeToken, setChallengeToken] = useState<string | null>(null);
  const [pendingName, setPendingName] = useState<string>('');
  const [bot, setBot] = useState<AdminBotPayload>({
    captchaToken: '',
    captchaAnswer: '',
    ready: false,
  });
  const [botKey, setBotKey] = useState(0);

  async function finishWithUser(user: {
    id: string;
    username: string;
    email: string;
    displayName: string | null;
    roles: string[];
    totpEnabled?: boolean;
    totpSetupRequired?: boolean;
  }) {
    const roles = user.roles || [];
    const canAccessAdmin = roles.some((role) => ['ADMIN', 'SUPER_ADMIN'].includes(role));
    if (!canAccessAdmin) {
      setError('当前账号没有管理后台访问权限。');
      return;
    }
    setUser(user);
    if (user.totpSetupRequired) {
      router.replace('/security');
    } else {
      router.replace('/');
    }
  }

  async function onPasswordFinish(values: { identifier: string; password: string }) {
    if (!bot.ready) {
      setError('请填写计算验证码');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await adminLogin(values.identifier.trim(), values.password, {
        captchaToken: bot.captchaToken,
        captchaAnswer: bot.captchaAnswer,
      });
      if (result.needTotp) {
        setChallengeToken(result.challengeToken);
        setPendingName(result.user.displayName || result.user.username || result.user.email);
        return;
      }
      await finishWithUser(result.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
      setBotKey((k) => k + 1);
    } finally {
      setLoading(false);
    }
  }

  async function onTotpFinish(values: { code: string }) {
    if (!challengeToken) return;
    setLoading(true);
    setError(null);
    try {
      const user = await adminLoginTotp(challengeToken, values.code.trim());
      setChallengeToken(null);
      await finishWithUser(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : '验证失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'linear-gradient(160deg, #f0f2f5 0%, #e6f4ff 55%, #f5f5f5 100%)',
        padding: 24,
      }}
    >
      <div style={{ width: '100%', maxWidth: 400, minWidth: 0 }}>
        {challengeToken ? (
          <LoginForm
            containerStyle={{ paddingInline: 0 }}
            contentStyle={{ width: '100%', maxWidth: 328, minWidth: 0 }}
            title="二次验证"
            subTitle={`请输入 ${pendingName || '账号'} 的谷歌验证器验证码`}
            logo={<SafetyCertificateOutlined style={{ fontSize: 32, color: '#1677ff' }} />}
            onFinish={onTotpFinish}
            submitter={{
              searchConfig: { submitText: '验证并登录' },
              submitButtonProps: { loading, size: 'large', block: true },
            }}
          >
            {error ? <AdminErrorAlert message={error} style={{ marginBottom: 16 }} /> : null}
            <ProFormText
              name="code"
              fieldProps={{
                size: 'large',
                prefix: <SafetyCertificateOutlined />,
                maxLength: 16,
                autoComplete: 'one-time-code',
              }}
              placeholder="6 位验证码或备份码"
              rules={[{ required: true, message: '请输入验证码' }]}
            />
            <Space wrap style={{ width: '100%', justifyContent: 'space-between', marginBottom: 8 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                打开 Google Authenticator / 微软验证器
              </Typography.Text>
              <Button
                type="link"
                size="small"
                onClick={() => {
                  setChallengeToken(null);
                  setError(null);
                  setBotKey((k) => k + 1);
                }}
              >
                返回
              </Button>
            </Space>
          </LoginForm>
        ) : (
          <LoginForm
            containerStyle={{ paddingInline: 0 }}
            contentStyle={{ width: '100%', maxWidth: 328, minWidth: 0 }}
            title="SmsInvit Admin"
            subTitle="使用具备后台权限的账号登录"
            logo={<UserOutlined style={{ fontSize: 32, color: '#1677ff' }} />}
            onFinish={onPasswordFinish}
            submitter={{
              searchConfig: { submitText: '登录' },
              submitButtonProps: {
                loading,
                size: 'large',
                block: true,
                disabled: !bot.ready,
              },
            }}
          >
            {error ? <AdminErrorAlert message={error} style={{ marginBottom: 16 }} /> : null}
            <ProFormText
              name="identifier"
              // 只给 placeholder 时，字段在无障碍树里没有名字（审查 8.4）。
              // ProForm 的 label 会自动与输入框关联（label[for] + input[id]）。
              label="用户名或邮箱"
              fieldProps={{
                size: 'large',
                prefix: <UserOutlined />,
                autoComplete: 'username',
              }}
              placeholder="用户名或邮箱"
              rules={[{ required: true, message: '请输入用户名或邮箱' }]}
            />
            <ProFormText.Password
              name="password"
              label="密码"
              fieldProps={{
                size: 'large',
                prefix: <LockOutlined />,
                autoComplete: 'current-password',
              }}
              placeholder="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            />
            <AdminBotGuard resetKey={botKey} onChange={setBot} />
          </LoginForm>
        )}
      </div>
    </div>
  );
}

import { CopyOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { PageContainer, ProCard, ProForm, ProFormText } from '@ant-design/pro-components';
import { Alert, Button, List, QRCode, Space, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import { useRouter } from '../../lib/navigation';
import { adminApi } from '../../lib/admin-api';
import { useAdminAuth } from '../../lib/admin-auth-context';

type TotpStatus = {
  totpEnabled: boolean;
  totpSetupRequired: boolean;
  enabledAt: string | null;
  backupCodesRemaining: number;
};

type SetupPayload = {
  secret: string;
  otpauthUrl: string;
  expiresInSeconds: number;
};

export default function AdminSecurityPage() {
  const router = useRouter();
  const { refresh } = useAdminAuth();
  const [status, setStatus] = useState<TotpStatus | null>(null);
  const [setup, setSetup] = useState<SetupPayload | null>(null);
  const [backupCodes, setBackupCodes] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(false);

  async function loadStatus() {
    const { data } = await adminApi<TotpStatus>('/api/v1/admin/auth/totp');
    setStatus(data);
    return data;
  }

  useEffect(() => {
    void loadStatus().catch((err) => {
      message.error(err instanceof Error ? err.message : '加载失败');
    });
  }, []);

  async function startSetup() {
    setLoading(true);
    setBackupCodes(null);
    try {
      const { data } = await adminApi<SetupPayload>('/api/v1/admin/auth/totp/setup', {
        method: 'POST',
      });
      setSetup(data);
      message.success('请使用验证器扫描二维码');
    } catch (err) {
      message.error(err instanceof Error ? err.message : '无法开始绑定');
    } finally {
      setLoading(false);
    }
  }

  async function enableTotp(values: { code: string }) {
    setLoading(true);
    try {
      const { data } = await adminApi<{ totpEnabled: boolean; backupCodes: string[] }>(
        '/api/v1/admin/auth/totp/enable',
        {
          method: 'POST',
          body: JSON.stringify({ code: values.code.trim() }),
        },
      );
      setSetup(null);
      setBackupCodes(data.backupCodes || []);
      await loadStatus();
      await refresh();
      message.success('谷歌验证器已启用');
    } catch (err) {
      message.error(err instanceof Error ? err.message : '启用失败');
    } finally {
      setLoading(false);
    }
  }

  async function disableTotp(values: { password: string; code: string }) {
    setLoading(true);
    try {
      await adminApi('/api/v1/admin/auth/totp/disable', {
        method: 'POST',
        body: JSON.stringify({
          password: values.password,
          code: values.code.trim(),
        }),
      });
      setBackupCodes(null);
      setSetup(null);
      await loadStatus();
      await refresh();
      message.success('已关闭谷歌验证器');
      router.replace('/security');
    } catch (err) {
      message.error(err instanceof Error ? err.message : '关闭失败');
    } finally {
      setLoading(false);
    }
  }

  const required = Boolean(status?.totpSetupRequired);

  return (
    <PageContainer
      title="安全设置"
      subTitle="登录密码与谷歌验证器（TOTP）"
      extra={
        required ? (
          <Alert type="warning" showIcon message="管理员必须绑定验证器后才能使用其它后台功能" />
        ) : null
      }
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <ProCard title="修改登录密码" bordered headerBordered>
          <ProForm
            submitter={{ searchConfig: { submitText: '更新密码' } }}
            onFinish={async (values) => {
              if (values.newPassword !== values.confirmPassword) {
                message.error('两次输入的新密码不一致');
                return false;
              }
              try {
                await adminApi('/api/v1/admin/auth/change-password', {
                  method: 'POST',
                  body: JSON.stringify({
                    currentPassword: values.currentPassword,
                    newPassword: values.newPassword,
                  }),
                });
                message.success('密码已更新');
                return true;
              } catch (err) {
                message.error(err instanceof Error ? err.message : '修改失败');
                return false;
              }
            }}
          >
            <ProFormText.Password
              name="currentPassword"
              label="当前密码"
              rules={[{ required: true, message: '请输入当前密码' }]}
              fieldProps={{ autoComplete: 'current-password' }}
            />
            <ProFormText.Password
              name="newPassword"
              label="新密码"
              rules={[
                { required: true, message: '请输入新密码' },
                { min: 8, message: '至少 8 位' },
              ]}
              fieldProps={{ autoComplete: 'new-password' }}
            />
            <ProFormText.Password
              name="confirmPassword"
              label="确认新密码"
              rules={[{ required: true, message: '请再次输入新密码' }]}
              fieldProps={{ autoComplete: 'new-password' }}
            />
          </ProForm>
        </ProCard>

        <ProCard title="当前状态" bordered headerBordered>
          {status ? (
            <Space direction="vertical">
              <Typography.Text>
                状态：
                <Typography.Text strong type={status.totpEnabled ? 'success' : 'warning'}>
                  {status.totpEnabled ? '已启用' : '未启用'}
                </Typography.Text>
              </Typography.Text>
              {status.enabledAt ? (
                <Typography.Text type="secondary">
                  启用时间 {new Date(status.enabledAt).toLocaleString()}
                </Typography.Text>
              ) : null}
              {status.totpEnabled ? (
                <Typography.Text type="secondary">
                  剩余备份码 {status.backupCodesRemaining} 个
                </Typography.Text>
              ) : null}
            </Space>
          ) : (
            <Typography.Text type="secondary">加载中…</Typography.Text>
          )}
        </ProCard>

        {backupCodes ? (
          <ProCard title="备份码（仅显示一次）" bordered headerBordered>
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="请立即保存备份码。丢失验证器时可使用备份码登录，每个码只能用一次。"
            />
            <List
              size="small"
              bordered
              dataSource={backupCodes}
              renderItem={(item) => (
                <List.Item
                  actions={[
                    <Button
                      key="copy"
                      type="link"
                      icon={<CopyOutlined />}
                      onClick={() => {
                        void navigator.clipboard.writeText(item);
                        message.success('已复制');
                      }}
                    >
                      复制
                    </Button>,
                  ]}
                >
                  <Typography.Text code>{item}</Typography.Text>
                </List.Item>
              )}
            />
            <Button
              type="primary"
              style={{ marginTop: 16 }}
              onClick={() => {
                setBackupCodes(null);
                if (!required) router.replace('/');
              }}
            >
              我已保存，继续
            </Button>
          </ProCard>
        ) : null}

        {!status?.totpEnabled && !backupCodes ? (
          <ProCard
            title="绑定谷歌验证器"
            bordered
            headerBordered
            extra={
              <Button
                type="primary"
                icon={<SafetyCertificateOutlined />}
                loading={loading}
                onClick={() => void startSetup()}
              >
                {setup ? '重新生成' : '开始绑定'}
              </Button>
            }
          >
            {setup ? (
              <Space align="start" size={24} wrap>
                <QRCode value={setup.otpauthUrl} size={180} />
                <div style={{ maxWidth: 360 }}>
                  <Typography.Paragraph type="secondary">
                    使用 Google Authenticator / Microsoft Authenticator
                    扫描左侧二维码，或手动输入密钥：
                  </Typography.Paragraph>
                  <Typography.Paragraph copyable code>
                    {setup.secret}
                  </Typography.Paragraph>
                  <ProForm
                    onFinish={enableTotp}
                    submitter={{
                      searchConfig: { submitText: '确认启用' },
                      submitButtonProps: { loading },
                      resetButtonProps: false,
                    }}
                  >
                    <ProFormText
                      name="code"
                      label="验证码"
                      placeholder="输入 App 中的 6 位数字"
                      rules={[{ required: true, message: '请输入验证码' }]}
                      fieldProps={{ maxLength: 8, autoComplete: 'one-time-code' }}
                    />
                  </ProForm>
                </div>
              </Space>
            ) : (
              <Typography.Text type="secondary">
                点击「开始绑定」生成密钥与二维码。绑定后，登录后台需输入动态验证码。
              </Typography.Text>
            )}
          </ProCard>
        ) : null}

        {status?.totpEnabled && !backupCodes ? (
          <ProCard title="关闭验证器" bordered headerBordered>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="关闭后，拥有后台角色的账号将再次被要求重新绑定。"
            />
            <ProForm
              onFinish={disableTotp}
              submitter={{
                searchConfig: { submitText: '确认关闭' },
                submitButtonProps: { danger: true, loading },
                resetButtonProps: false,
              }}
            >
              <ProFormText.Password
                name="password"
                label="当前密码"
                rules={[{ required: true, message: '请输入密码' }]}
              />
              <ProFormText
                name="code"
                label="验证码或备份码"
                rules={[{ required: true, message: '请输入验证码' }]}
              />
            </ProForm>
          </ProCard>
        ) : null}
      </Space>
    </PageContainer>
  );
}

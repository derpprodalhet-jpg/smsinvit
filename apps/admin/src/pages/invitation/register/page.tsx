import { useEffect, useState } from 'react';
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Checkbox,
  Col,
  Empty,
  Form,
  InputNumber,
  Row,
  Space,
  Switch,
  Typography,
  message,
} from 'antd';
import { AdminPage } from '../../../components/admin-page';
import { InvitationRunPanel } from '../../../components/invitation-run-panel';
import {
  copyInvitationText,
  invitationAction,
  invitationApi,
  invitationErrorMessage,
} from '../../../lib/invitation-api';
import { useInvitationQuery } from '../../../lib/use-invitation-query';
import { useInvitationRuntime } from '../../../lib/invitation-runtime';
import { Link } from '../../../lib/navigation';
import { resolveLabel } from '../../../lib/zh-label';
import { registrationPayload, useRegistrationPreferences } from './preferences';

/** 代理池状态 → 中文：选择器里不能显示 ready / frozen 这类原始枚举。 */
const PROXY_STATUS_LABEL: Record<string, string> = {
  ready: '可用',
  frozen: '冻结中',
  dead: '永久不可用',
  disabled: '已禁用',
};

export default function RegisterPage() {
  const [preferences, update] = useRegistrationPreferences();
  const [email, setEmail] = useState(
    () =>
      new URLSearchParams(typeof window === 'undefined' ? '' : window.location.search).get(
        'email',
      ) || '',
  );
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState('');
  const [credentials, setCredentials] = useState<any>();
  const [credentialError, setCredentialError] = useState('');
  const runtime = useInvitationRuntime();
  const proxies = useInvitationQuery<{ items: { id: string; url: string; status: string }[] }>(
    '/proxy/pool',
  );
  const providers = useInvitationQuery<{
    providers: { kind: string; display_name: string; pooled: boolean }[];
    current: string;
  }>('/mail/providers');
  const provider = providers.data?.providers.find((item) => item.kind === providers.data?.current);
  const accounts = useInvitationQuery<{ items: { email: string }[] }>(
    `/accounts?status=available&limit=5000&kind=${encodeURIComponent(providers.data?.current || '')}`,
  );
  useEffect(() => {
    let alive = true;
    setCredentials(undefined);
    setCredentialError('');
    if (runtime.run?.status === 'done' && runtime.run?.email) {
      void invitationApi<{ data: any }>(`/registered/${encodeURIComponent(runtime.run.email)}`)
        .then((result) => {
          if (alive) setCredentials(result.data);
        })
        .catch((error) => {
          if (alive) setCredentialError(invitationErrorMessage(error));
        });
    }
    return () => {
      alive = false;
    };
  }, [runtime.runId, runtime.run?.status, runtime.run?.email]);

  async function start() {
    setStarting(true);
    setStartError('');
    setCredentials(undefined);
    try {
      const result = await invitationAction<{ run_id: string }>(
        '/register',
        registrationPayload(email, preferences),
      );
      runtime.watchRun(result.run_id);
      message.success('注册任务已启动');
    } catch (error) {
      setStartError(invitationErrorMessage(error));
    } finally {
      setStarting(false);
    }
  }
  async function copy(text: string) {
    try {
      await copyInvitationText(text);
      message.success('已复制');
    } catch (error) {
      message.error(invitationErrorMessage(error));
    }
  }
  return (
    <AdminPage title="单次注册" description="选择邮箱与代理，启动一次注册并查看执行结果。">
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={10}>
          <Card>
            <Form layout="vertical" onFinish={() => void start()}>
              <Form.Item
                label="邮箱"
                extra={
                  provider?.pooled === false
                    ? `当前来源 ${provider.display_name} 自动创建邮箱。`
                    : `留空自动选择可用邮箱${provider ? `；当前来源：${provider.display_name}` : ''}。`
                }
              >
                <AutoComplete
                  aria-label="注册邮箱"
                  value={email}
                  onChange={setEmail}
                  disabled={provider?.pooled === false}
                  allowClear
                  options={(accounts.data?.items || []).map((item) => ({ value: item.email }))}
                  filterOption={(input, option) =>
                    String(option?.value).toLowerCase().includes(input.toLowerCase())
                  }
                  placeholder="留空自动选号，或输入指定邮箱"
                />
              </Form.Item>
              <Form.Item
                label="本次代理"
                extra="可从代理池选择或手动输入；留空直连。注册结果页的 Plus 检查和授权复用此选择。"
              >
                <AutoComplete
                  aria-label="注册代理"
                  value={preferences.proxy}
                  onChange={(proxy) => update({ proxy })}
                  allowClear
                  options={(proxies.data?.items || [])
                    .filter((item) => item.url && item.url !== '***')
                    .map((item) => ({
                      value: item.url,
                      label: `${item.url}（${resolveLabel(PROXY_STATUS_LABEL, item.status)}）`,
                    }))}
                  filterOption={(input, option) =>
                    String(option?.value).toLowerCase().includes(input.toLowerCase())
                  }
                  placeholder="socks5h://user:pass@host:1080"
                />
              </Form.Item>
              <Form.Item label="等待邮箱验证码（秒）">
                <InputNumber
                  aria-label="OTP 等待秒数"
                  min={10}
                  max={600}
                  value={preferences.otpTimeout}
                  onChange={(value) => update({ otpTimeout: value || 10 })}
                />
              </Form.Item>
              <Form.Item
                label="加入待兑换账号池（注册后停在 2FA，不做 Codex 授权）"
                extra="开启后注册完成即进入待兑换账号池，等用户兑换 CDK 再执行 Codex 授权；账号池注册必须绑定 2FA，也还没有 Refresh Token。"
              >
                <Switch
                  aria-label="加入待兑换账号池"
                  checked={preferences.poolOnly}
                  onChange={(poolOnly) => update({ poolOnly })}
                />
              </Form.Item>
              <Form.Item label="获取凭证">
                <Space wrap>
                  <Checkbox
                    checked={preferences.wantAccessToken}
                    onChange={(event) => update({ wantAccessToken: event.target.checked })}
                  >
                    Access Token
                  </Checkbox>
                  <Checkbox
                    checked={preferences.wantSessionToken}
                    onChange={(event) => update({ wantSessionToken: event.target.checked })}
                  >
                    Session Token
                  </Checkbox>
                  <Checkbox
                    checked={preferences.wantRefreshToken}
                    disabled={preferences.poolOnly}
                    onChange={(event) => update({ wantRefreshToken: event.target.checked })}
                  >
                    Refresh Token
                  </Checkbox>
                </Space>
                {preferences.poolOnly && (
                  <Typography.Paragraph type="secondary" style={{ margin: '12px 0 0' }}>
                    账号池注册在 Codex 授权前停止，此时还没有 Refresh Token；兑换 CDK
                    启动授权后才会产生。
                  </Typography.Paragraph>
                )}
              </Form.Item>
              <Form.Item>
                <Checkbox
                  checked={preferences.allowExistingLogin}
                  onChange={(event) => update({ allowExistingLogin: event.target.checked })}
                >
                  允许已有账号登录
                </Checkbox>
              </Form.Item>
              <Form.Item label="注册成功后绑定 2FA（TOTP）">
                <Switch
                  aria-label="自动绑定 2FA"
                  checked={preferences.want2fa}
                  disabled={preferences.poolOnly}
                  onChange={(want2fa) => update({ want2fa })}
                />
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginTop: 12 }}
                  message="默认启用；绑定后登录需动态验证码。"
                  description="仅对有密码的账号生效。请在注册成功后立即复制并保存 2FA secret，服务端无法重新获取。"
                />
                {preferences.poolOnly && (
                  <Typography.Paragraph type="secondary" style={{ margin: '12px 0 0' }}>
                    账号池注册固定绑定 2FA：兑换后凭 2FA 才能继续 Codex 授权。
                  </Typography.Paragraph>
                )}
              </Form.Item>
              <Button
                type="primary"
                htmlType="submit"
                loading={starting}
                disabled={runtime.running}
              >
                开始注册
              </Button>
            </Form>
            {(accounts.error || providers.error || proxies.error) && (
              <Alert
                type="error"
                showIcon
                message={accounts.error || providers.error || proxies.error}
                style={{ marginTop: 16 }}
              />
            )}
            {startError && (
              <Alert type="error" showIcon message={startError} style={{ marginTop: 16 }} />
            )}
            {runtime.run?.status === 'failed' && (
              <Alert
                type="error"
                showIcon
                message="注册失败"
                description={runtime.run.error || '请查看执行日志。'}
                style={{ marginTop: 16 }}
              />
            )}
            {runtime.run?.status === 'done' && (
              <Alert
                type="success"
                showIcon
                message={`任务完成：${runtime.run.email || ''}`}
                description={<Link href="/invitation/registered">查看注册结果</Link>}
                style={{ marginTop: 16 }}
              />
            )}
            {credentialError && (
              <Alert
                type="error"
                showIcon
                message={`获取结果凭证失败：${credentialError}`}
                style={{ marginTop: 12 }}
              />
            )}
            {credentials && (
              <Space direction="vertical" size={12} style={{ width: '100%', marginTop: 16 }}>
                <Typography.Text strong>注册凭证</Typography.Text>
                <Typography.Text>邮箱：{credentials.email || runtime.run?.email}</Typography.Text>
                <Typography.Text>
                  Access Token 长度：{String(credentials.access_token || '').length}
                </Typography.Text>
                {credentials.password ? (
                  <Typography.Text copyable={{ text: credentials.password }}>
                    密码：<code>{credentials.password}</code>
                  </Typography.Text>
                ) : (
                  <Typography.Text type="secondary">此账号未设置密码</Typography.Text>
                )}
                {credentials.totp_secret && (
                  <Typography.Text copyable={{ text: credentials.totp_secret }}>
                    2FA：<code style={{ overflowWrap: 'anywhere' }}>{credentials.totp_secret}</code>
                  </Typography.Text>
                )}
                <Space wrap>
                  <Button onClick={() => void copy(credentials.email || runtime.run?.email)}>
                    复制邮箱
                  </Button>
                  {credentials.password && (
                    <Button
                      onClick={() =>
                        void copy(
                          `${credentials.email || runtime.run?.email}----${credentials.password}`,
                        )
                      }
                    >
                      复制邮箱----密码
                    </Button>
                  )}
                  {credentials.access_token && (
                    <Button onClick={() => void copy(credentials.access_token)}>
                      复制 Access Token
                    </Button>
                  )}
                  {credentials.totp_secret && (
                    <Button onClick={() => void copy(credentials.totp_secret)}>
                      复制 2FA Secret
                    </Button>
                  )}
                </Space>
              </Space>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={14}>
          {runtime.runId ? (
            <InvitationRunPanel />
          ) : (
            <Card>
              <Empty description="启动注册后，这里显示任务状态与日志" />
            </Card>
          )}
        </Col>
      </Row>
    </AdminPage>
  );
}

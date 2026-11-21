import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Input,
  InputNumber,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { AdminPage } from './admin-page';
import { adminApi } from '../lib/admin-api';
import { setBusinessTimeZone } from '../lib/format-datetime';
import {
  initialSettingDraft,
  isSecretSetting,
  preservesSecret,
  TURNSTILE_SECRET_KEY,
} from '../lib/settings-values';
type Setting = {
  key: string;
  category: string;
  valueJson: Record<string, unknown>;
  visibility?: string;
  isSecret?: boolean;
  source?: string;
  requiresRestart?: boolean;
  valueType?: string;
  warning?: string;
  label?: string;
};
const NOTIFY: Array<[string, string, boolean?, boolean?]> = [
  ['telegram_bot_token', 'Telegram Bot Token', true],
  ['telegram_ops_chat_id', '运维告警群 ID'],
];
export function SettingsWorkspace({ section = 'runtime' }: { section?: 'runtime' | 'notify' }) {
  const [rows, setRows] = useState<Setting[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string | boolean | number>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  async function load() {
    setLoading(true);
    try {
      const r = await adminApi<Setting[]>('/api/v1/admin/settings');
      let data = (r.data || []).filter((x) =>
        section === 'runtime'
          ? x.key.startsWith('runtime.')
          : NOTIFY.some(([k]) => x.key === `notify.${k}`),
      );
      if (section === 'notify') {
        data = NOTIFY.map(([key, label, secret, bool]) => ({
          ...data.find((x) => x.key === `notify.${key}`),
          key: `notify.${key}`,
          category: 'notify',
          label,
          isSecret: secret,
          valueType: bool ? 'BOOLEAN' : 'TEXT',
          valueJson:
            data.find((x) => x.key === `notify.${key}`)?.valueJson ||
            (bool ? { enabled: false } : { value: '' }),
        }));
      }
      setRows(data);
      setDrafts(
        Object.fromEntries(
          data.map((x) => [x.key, initialSettingDraft(x)]),
        ),
      );
      const tz = data.find((x) => x.key === 'runtime.business_time_zone');
      if (tz?.valueJson?.value) setBusinessTimeZone(String(tz.valueJson.value));
    } catch (e) {
      message.error(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, [section]);
  async function save(row: Setting) {
    if (preservesSecret(row, drafts[row.key])) {
      message.info('密钥未修改，原值保持不变');
      return;
    }
    setSaving(row.key);
    try {
      const bool = row.valueType === 'BOOLEAN';
      const runtime = row.key.startsWith('runtime.');
      const value = bool
        ? runtime
          ? String(Boolean(drafts[row.key]))
          : Boolean(drafts[row.key])
        : String(drafts[row.key] ?? '');
      const valueJson = bool && !runtime ? { enabled: value } : { value };
      const r = await adminApi<Setting>('/api/v1/admin/settings', {
        method: 'POST',
        body: JSON.stringify({
          key: row.key,
          category: row.category,
          valueJson,
          visibility: 'PRIVATE',
          ...(isSecretSetting(row) ? { isSecret: true } : {}),
        }),
      });
      setRows((prev) => prev.map((x) => (x.key === row.key ? { ...x, ...r.data } : x)));
      if (isSecretSetting(row))
        setDrafts((d) => ({ ...d, [row.key]: initialSettingDraft({ ...row, ...r.data }) }));
      if (row.key === 'runtime.business_time_zone') setBusinessTimeZone(String(value));
      message.success(row.requiresRestart ? '已保存，重启应用后生效' : '已保存');
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存失败');
    } finally {
      setSaving(null);
    }
  }
  return (
    <AdminPage
      title={section === 'runtime' ? '运行配置' : '通知配置'}
      description={
        section === 'runtime'
          ? '每项单独保存；当前来源和生效时机由服务器提供。'
          : '配置兑换告警与任务通知；密钥通过原有加密接口保存。'
      }
    >
      <Space style={{ marginBottom: 16 }}>
        <Button loading={loading} onClick={() => void load()}>
          刷新配置
        </Button>
      </Space>
      <Table<Setting>
        rowKey="key"
        loading={loading}
        dataSource={rows}
        pagination={false}
        columns={[
          {
            title: '配置',
            render: (_, r) => (
              <>
                {/* 主体只显示中文名称，原始英文 key 放到提示里，避免列表里出现英文。 */}
                <Tooltip title={r.key}>
                  <Typography.Text strong>{r.label || r.key}</Typography.Text>
                </Tooltip>
                {r.warning && <Alert type="warning" message={r.warning} style={{ marginTop: 8 }} />}
              </>
            ),
          },
          {
            title: '值',
            width: '40%',
            render: (_, r) =>
              r.valueType === 'BOOLEAN' ? (
                <Switch
                  checked={Boolean(drafts[r.key])}
                  onChange={(v) => setDrafts((d) => ({ ...d, [r.key]: v }))}
                />
              ) : isSecretSetting(r) ? (
                <Input.Password
                  value={String(drafts[r.key] ?? '')}
                  placeholder={
                    r.key === TURNSTILE_SECRET_KEY ? '输入新密钥；清空后保存可移除' : '留空保留已保存密钥'
                  }
                  autoComplete="new-password"
                  onChange={(e) => setDrafts((d) => ({ ...d, [r.key]: e.target.value }))}
                />
              ) : r.valueType === 'INTEGER' ? (
                <InputNumber
                  value={Number(drafts[r.key])}
                  onChange={(v) => setDrafts((d) => ({ ...d, [r.key]: v ?? '' }))}
                />
              ) : (
                <Input
                  value={String(drafts[r.key] ?? '')}
                  onChange={(e) => setDrafts((d) => ({ ...d, [r.key]: e.target.value }))}
                />
              ),
          },
          {
            title: '生效',
            render: (_, r) => (
              <Space direction="vertical">
                <Tag>{r.source || '后台配置'}</Tag>
                {r.requiresRestart && <Tag color="orange">需要重启</Tag>}
              </Space>
            ),
          },
          {
            title: '操作',
            render: (_, r) => (
              <Button loading={saving === r.key} onClick={() => void save(r)}>
                保存
              </Button>
            ),
          },
        ]}
      />
      {section === 'notify' && (
        <Card title="测试已保存的渠道" style={{ marginTop: 20 }}>
          <Space wrap>
            <Button
              onClick={async () => {
                try {
                  await adminApi('/api/v1/admin/settings/test-telegram', { method: 'POST' });
                  message.success('已提交，请在通知发送记录中确认送达状态');
                } catch (e) {
                  message.error(e instanceof Error ? e.message : '发送失败');
                }
              }}
            >
              发送 Telegram 测试通知
            </Button>
          </Space>
        </Card>
      )}
    </AdminPage>
  );
}

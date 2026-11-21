import { useEffect, useState, type Key } from 'react';
import { Alert, AutoComplete, Button, Card, Checkbox, Collapse, Drawer, Dropdown, Empty, Form, Input, Modal, Select, Space, Table, Tag, Tooltip, Typography, message } from 'antd';
import { AdminPage } from '../../../components/admin-page';
import { AdminDetailDrawer, type DetailGroup } from '../../../components/admin-detail-drawer';
import { InvitationStatus } from '../../../components/invitation-status';
import { InvitationPhoneStatus, type InvitationPhoneRecord } from '../../../components/invitation-phone-status';
import { proxyUsageLabel, proxyUsageTone } from '../../../lib/invitation';
import { resolveLabel } from '../../../lib/zh-label';
import { InvitationRunPanel } from '../../../components/invitation-run-panel';
import {
  copyInvitationText,
  downloadInvitationExport,
  invitationAction,
  invitationApi,
  invitationErrorMessage,
  invitationTime,
} from '../../../lib/invitation-api';
import { useInvitationQuery } from '../../../lib/use-invitation-query';
import { useInvitationRuntime } from '../../../lib/invitation-runtime';
import { useRegistrationPreferences } from '../register/preferences';
import {
  credentialEditPayload,
  credentialFieldLabel,
  credentialRows,
  plusCheckSummary,
  PLUS_CHECK_LABEL,
  REGISTERED_SOURCE_LABEL,
  registeredExportPayload,
  type Credentials,
} from './model';

type Row = InvitationPhoneRecord & {
  email: string;
  /** -1 未记录 / 1 有代理 / 0 直连 */
  used_proxy?: number;
  password?: string;
  totp_secret?: string;
  at_len: number;
  st_len: number;
  rt_len: number;
  codex_rt_len: number;
  source?: string;
  proxy?: string;
  created_at?: number;
  plus_check?: { status: string; label?: string; [key: string]: any };
};
type ExportResult = {
  text?: string;
  b64?: string;
  mode?: string;
  mime?: string;
  filename?: string;
  count?: number;
  label?: string;
};
const filters = [
  { value: 'all', label: '全部结果' },
  { value: 'has_rt', label: '有 Refresh Token' },
  { value: 'no_rt', label: '无 Refresh Token' },
  { value: 'unchecked', label: '未检测 Plus' },
  { value: 'free', label: 'Free' },
  { value: 'plus', label: '可领 Plus' },
  { value: 'banned', label: '已封号' },
  { value: 'token_invalid', label: '凭证失效' },
];

/** 代理只按 `used_proxy` 说明真实用法；空值不能像以前那样写成「直连」。 */
function proxyTag(row: Row) {
  return (
    <Tooltip title={row.proxy || '未记录代理地址'}>
      <Tag color={proxyUsageTone(row.used_proxy)}>{proxyUsageLabel(row.used_proxy)}</Tag>
    </Tooltip>
  );
}

export default function RegisteredPage() {
  const [page, setPage] = useState(1),
    [pageSize, setPageSize] = useState(20),
    [filter, setFilter] = useState('all');
  const [selected, setSelected] = useState<Key[]>([]),
    [busy, setBusy] = useState('');
  const [checkResult, setCheckResult] = useState('');
  const [checkDetails, setCheckDetails] = useState<
    { email: string; status: string; label?: string }[]
  >([]);
  const [exported, setExported] = useState<ExportResult>();
  const [credential, setCredential] = useState<Credentials>();
  const [rowDetail, setRowDetail] = useState<Row | null>(null);
  const [editing, setEditing] = useState<Credentials>();
  const [editPassword, setEditPassword] = useState(''),
    [editSecret, setEditSecret] = useState('');
  const [pushEmail, setPushEmail] = useState(''),
    [targets, setTargets] = useState<string[]>(['cpa', 'sub2api']);
  const [pushResult, setPushResult] = useState<any>();
  const [preferences, updatePreferences] = useRegistrationPreferences();
  const runtime = useInvitationRuntime();
  const list = useInvitationQuery<{ items: Row[]; total: number }>(
    `/registered?${new URLSearchParams({ filter, limit: String(pageSize), offset: String((page - 1) * pageSize) })}`,
  );
  const proxies = useInvitationQuery<{ items: { url: string; status: string }[] }>('/proxy/pool');
  const formats = useInvitationQuery<{ formats: { id: string; label: string; note?: string }[] }>(
    '/registered/export/formats',
    0,
  );
  useEffect(() => {
    setSelected([]);
    setCheckDetails([]);
    setCheckResult('');
  }, [page, pageSize, filter]);

  async function copy(text: string) {
    if (!text) {
      message.info('该凭证为空');
      return;
    }
    try {
      await copyInvitationText(text);
      message.success('已复制');
    } catch (error) {
      message.error(invitationErrorMessage(error));
    }
  }
  async function getCredentials(email: string): Promise<Credentials> {
    const result = await invitationApi<{ data: Credentials }>(
      `/registered/${encodeURIComponent(email)}`,
    );
    return { ...result.data, email };
  }
  function closeEdit() {
    setEditing(undefined);
    setEditPassword('');
    setEditSecret('');
  }
  async function showCredentials(email: string, edit = false) {
    setBusy(`credentials:${email}`);
    try {
      const data = await getCredentials(email);
      if (edit) {
        setEditing(data);
        setEditPassword(data.password || '');
        setEditSecret(data.totp_secret || '');
      } else setCredential(data);
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  async function copyField(email: string, field: string) {
    try {
      const data = await getCredentials(email);
      await copy(
        field.startsWith('codex.') ? data.codex?.[field.slice(6)] || '' : data[field] || '',
      );
    } catch (error) {
      message.error(invitationErrorMessage(error));
    }
  }
  async function saveEdit() {
    if (!editing?.email) return;
    const payload = credentialEditPayload(editing.email, editing, editPassword, editSecret);
    if (Object.keys(payload).length === 1) {
      message.info('没有改动');
      closeEdit();
      return;
    }
    const perform = async () => {
      setBusy('save');
      try {
        const result = await invitationAction<{ changed: string[] }>(
          '/registered/update_credentials',
          payload,
        );
        message.success(`已保存：${result.changed?.join(' + ') || '凭证'}`);
        closeEdit();
        await list.reload();
      } catch (error) {
        message.error(invitationErrorMessage(error));
      } finally {
        setBusy('');
      }
    };
    if (payload.totp_secret !== undefined && editing.totp_secret) {
      Modal.confirm({
        title: '确认覆盖已有 2FA Secret？',
        content: '覆盖后原 Secret 将永久丢失。若原值仍在账号上生效，错误的替换将使账号无法登录。',
        okText: '确认覆盖',
        cancelText: '取消',
        okButtonProps: { danger: true },
        onOk: perform,
      });
    } else await perform();
  }
  async function deleteRows(emails: string[], all = false) {
    setBusy('delete');
    try {
      const result = await invitationAction<{ deleted: number }>(
        '/registered/bulk_delete',
        all ? { all: true } : { emails },
      );
      message.success(`已删除 ${result.deleted} 条凭证`);
      setSelected([]);
      await list.reload();
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  function confirmDelete(emails: string[], all = false) {
    Modal.confirm({
      title: all ? '清空全部注册凭证？' : `永久删除 ${emails.length} 条注册凭证？`,
      content: '此操作无法恢复。邮箱池中的邮箱不会被删除。',
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => {
        if (all)
          Modal.confirm({
            title: '再次确认：永久清空全部凭证',
            content: '包括密码、令牌和 2FA 记录。',
            okText: '永久清空',
            cancelText: '取消',
            okButtonProps: { danger: true },
            onOk: () => deleteRows([], true),
          });
        else return deleteRows(emails);
      },
    });
  }
  async function check(mode: 'selected' | 'unchecked' | 'page') {
    const emails =
      mode === 'selected'
        ? selected.map(String)
        : (list.data?.items || [])
            .filter((row) => mode !== 'unchecked' || !row.plus_check)
            .map((row) => row.email);
    if (!emails.length) {
      message.info('当前页没有可检测的账号');
      return;
    }
    setBusy('check');
    setCheckResult(`检查中，${emails.length} 个账号…`);
    try {
      const result = await invitationAction<{
        results: Record<string, { status: string; label?: string }>;
        note?: string;
      }>('/registered/check_plus', { emails, proxy: preferences.proxy.trim() });
      const counts = plusCheckSummary(result.results || {}, emails.length);
      setCheckDetails(
        emails.map((email) => ({
          email,
          ...(result.results?.[email] || { status: 'error', label: '服务未返回该账号结果' }),
        })),
      );
      setCheckResult(
        `完成：${counts.plus} 可用 Plus，${counts.free} Free，${counts.banned} 已封号，${counts.invalid} 凭证失效，${counts.failed} 未检测成功${result.note ? `；${result.note}` : ''}`,
      );
      await list.reload();
    } catch (error) {
      setCheckResult(`检查失败：${invitationErrorMessage(error)}`);
    } finally {
      setBusy('');
    }
  }
  async function authorize(emails: string[]) {
    setBusy('auth');
    let started = 0;
    try {
      for (const email of emails.slice(0, 20)) {
        const result = await invitationAction<{ run_id: string }>('/registered/codex_auth', {
          email,
          proxy: preferences.proxy.trim(),
        });
        if (started === 0) runtime.watchRun(result.run_id);
        started += 1;
      }
      message.success(
        `已启动 ${started} 个授权任务，日志展示第一个；全部任务可在运行记录查看。${emails.length > 20 ? '本次最多启动 20 个。' : ''}`,
      );
    } catch (error) {
      message.error(`${started ? `已启动 ${started} 个；` : ''}${invitationErrorMessage(error)}`);
    } finally {
      setBusy('');
    }
  }
  async function doExport(format: string) {
    setBusy('export');
    try {
      const result = await invitationAction<ExportResult>(
        '/registered/export',
        registeredExportPayload(format, selected.map(String)),
      );
      if (result.mode === 'download') {
        downloadInvitationExport(result);
        message.success(`已下载 ${result.filename}（${result.count || 0} 个账号）`);
      } else setExported(result);
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  async function push() {
    if (!targets.length) {
      message.warning('至少选择一个推送目标');
      return;
    }
    setBusy('push');
    setPushResult(undefined);
    try {
      const result = await invitationAction<any>('/registered/export_to_panel', {
        email: pushEmail,
        targets,
      });
      setPushResult(result);
      if (targets.every((target) => result[target]?.ok !== false)) message.success('面板推送完成');
      else message.warning('部分目标推送失败，请查看下方结果');
    } catch (error) {
      setPushResult({ error: invitationErrorMessage(error) });
    } finally {
      setBusy('');
    }
  }
  /**
   * 列表里的凭证列：只显示「复制」入口与字符数，点一下才按需读取完整值。
   *
   * 注意：曾经把这四列（Access/Session/RT/Codex RT）移进详情以减少列表宽度，
   * 运营反馈必须在一屏里看到每行有哪些凭证，故按需求放回列表——但**不**在
   * 表格里铺明文，仍然是按需复制。
   */
  const credentialColumn = (title: string, lengthKey: keyof Row, field: string) => ({
    title,
    width: 104,
    render: (_: unknown, row: Row) =>
      Number(row[lengthKey] || 0) > 0 ? (
        <Button
          type="link"
          size="small"
          disabled={!!busy}
          onClick={() => void copyField(row.email, field)}
        >
          复制 ({String(row[lengthKey])})
        </Button>
      ) : (
        <Typography.Text type="secondary">—</Typography.Text>
      ),
  });
  const secretColumn = (title: string, field: 'password' | 'totp_secret') => ({
    title,
    width: 84,
    render: (_: unknown, row: Row) =>
      row[field] ? (
        <Button
          type="link"
          size="small"
          disabled={!!busy}
          onClick={() => void copyField(row.email, field)}
        >
          复制
        </Button>
      ) : (
        <Typography.Text type="secondary">未保存</Typography.Text>
      ),
  });
  const tokenItem = (label: string, lengthKey: keyof Row, field: string) => ({
    key: String(lengthKey),
    label,
    children:
      rowDetail && Number(rowDetail[lengthKey]) > 0 ? (
        <Button
          type="link"
          size="small"
          disabled={!!busy}
          onClick={() => void copyField(rowDetail.email, field)}
        >
          复制（{String(rowDetail[lengthKey])} 字符）
        </Button>
      ) : (
        '—'
      ),
  });
  const detailGroups: DetailGroup[] = rowDetail
    ? [
        {
          key: 'account',
          title: '账号',
          items: [
            { key: 'email', label: '邮箱', children: rowDetail.email },
            {
              key: 'password',
              label: '密码',
              children: rowDetail.password ? (
                <Button
                  type="link"
                  size="small"
                  disabled={!!busy}
                  onClick={() => void copyField(rowDetail.email, 'password')}
                >
                  复制密码
                </Button>
              ) : (
                '未保存'
              ),
            },
            {
              key: 'totp',
              label: '2FA 密钥',
              children: rowDetail.totp_secret ? (
                <Button
                  type="link"
                  size="small"
                  disabled={!!busy}
                  onClick={() => void copyField(rowDetail.email, 'totp_secret')}
                >
                  复制 2FA 密钥
                </Button>
              ) : (
                '未保存'
              ),
            },
            {
              key: 'source',
              label: '来源',
              children: resolveLabel(REGISTERED_SOURCE_LABEL, rowDetail.source),
            },
            {
              key: 'created',
              label: '创建时间',
              children: invitationTime(rowDetail.created_at),
            },
          ],
        },
        {
          key: 'tokens',
          title: '令牌',
          items: [
            tokenItem('访问令牌', 'at_len', 'access_token'),
            tokenItem('会话令牌', 'st_len', 'session_token'),
            tokenItem('刷新令牌', 'rt_len', 'refresh_token'),
            tokenItem('Codex 刷新令牌', 'codex_rt_len', 'codex.refresh_token'),
          ],
        },
        {
          key: 'checks',
          title: '检测',
          items: [
            {
              key: 'plus',
              label: 'Plus 状态',
              children: rowDetail.plus_check ? (
                <Space direction="vertical" size={0}>
                  <InvitationStatus status={rowDetail.plus_check.status} />
                  <Typography.Text type="secondary">
                    {rowDetail.plus_check.label ||
                      resolveLabel(PLUS_CHECK_LABEL, rowDetail.plus_check.status)}
                  </Typography.Text>
                </Space>
              ) : (
                '未检测'
              ),
            },
            {
              key: 'phone',
              label: '接码',
              children: <InvitationPhoneStatus record={rowDetail} />,
            },
            {
              key: 'proxy',
              label: '代理',
              children: proxyTag(rowDetail),
            },
            {
              key: 'result',
              label: '结果',
              children: rowDetail.plus_check
                ? rowDetail.plus_check.label ||
                  resolveLabel(PLUS_CHECK_LABEL, rowDetail.plus_check.status)
                : '—',
            },
          ],
        },
      ]
    : [];
  return (
    <AdminPage
      title="注册结果"
      description="查看注册凭证、检测 Plus、执行 Codex 授权并导出或推送账号。"
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {list.error && (
          <Alert
            type="error"
            showIcon
            message={list.error}
            action={<Button onClick={() => void list.reload()}>重试</Button>}
          />
        )}
        <Card>
          <Space wrap style={{ marginBottom: 16 }}>
            <Select
              aria-label="注册结果筛选"
              value={filter}
              style={{ width: 180 }}
              onChange={(value) => {
                setFilter(value);
                setPage(1);
              }}
              options={filters}
            />
            <AutoComplete
              aria-label="Plus 与授权代理"
              allowClear
              value={preferences.proxy}
              onChange={(proxy) => updatePreferences({ proxy })}
              style={{ width: 280 }}
              placeholder="检测与授权代理，留空直连"
              options={(proxies.data?.items || [])
                .filter((item) => item.url && item.url !== '***')
                .map((item) => ({ value: item.url }))}
              filterOption={(input, option) =>
                String(option?.value).toLowerCase().includes(input.toLowerCase())
              }
            />
            <Button loading={list.loading} onClick={() => void list.reload()}>
              刷新
            </Button>
            <Button
              disabled={!!busy}
              loading={busy === 'check'}
              onClick={() => void check('unchecked')}
            >
              检查当前页未检测
            </Button>
            <Button disabled={!!busy} onClick={() => void check('page')}>
              重新检查当前页
            </Button>
            <Button disabled={!selected.length || !!busy} onClick={() => void check('selected')}>
              检测选中 ({selected.length})
            </Button>
          </Space>
          <Space wrap style={{ marginBottom: 16 }}>
            <Dropdown
              disabled={!!busy}
              menu={{
                items: (formats.data?.formats || []).map((format) => ({
                  key: format.id,
                  label: (
                    <span>
                      {format.label}
                      {format.note && (
                        <Typography.Text type="secondary"> · {format.note}</Typography.Text>
                      )}
                    </span>
                  ),
                })),
                onClick: ({ key }) => void doExport(key),
              }}
            >
              <Button
                loading={busy === 'export'}
                disabled={!!formats.error || !formats.data?.formats.length}
              >
                {selected.length ? `导出选中 (${selected.length})` : '导出全部（跨页）'}
              </Button>
            </Dropdown>
            <Button
              disabled={!selected.length || !!busy}
              loading={busy === 'auth'}
              onClick={() => void authorize(selected.map(String))}
            >
              授权选中 ({selected.length})
            </Button>
            <Button
              danger
              disabled={!selected.length || !!busy}
              onClick={() => confirmDelete(selected.map(String))}
            >
              删除选中 ({selected.length})
            </Button>
            <Button danger disabled={!!busy} onClick={() => confirmDelete([], true)}>
              清空全部
            </Button>
          </Space>
          {formats.error && (
            <Alert
              type="warning"
              showIcon
              message={`导出格式不可用：${formats.error}`}
              style={{ marginBottom: 12 }}
            />
          )}
          {checkResult && (
            <Alert type="info" showIcon message={checkResult} style={{ marginBottom: 12 }} />
          )}
          {!!checkDetails.length && (
            <Collapse
              style={{ marginBottom: 16 }}
              items={[
                {
                  key: 'results',
                  label: `本次 Plus 检测详情（${checkDetails.length} 个账号）`,
                  children: (
                    <Table
                      rowKey="email"
                      size="small"
                      pagination={false}
                      dataSource={checkDetails}
                      columns={[
                        { title: '邮箱', dataIndex: 'email' },
                        {
                          title: '结果',
                          dataIndex: 'label',
                          // 后端有时只给 status：任何情况下都不能漏出裸英文枚举。
                          render: (label: string, row: { status: string }) =>
                            label || resolveLabel(PLUS_CHECK_LABEL, row.status),
                        },
                      ]}
                    />
                  ),
                },
              ]}
            />
          )}
          <Table<Row>
            rowKey="email"
            size="middle"
            loading={list.loading}
            dataSource={list.data?.items || []}
            scroll={{ x: 1048 }}
            rowSelection={{ selectedRowKeys: selected, onChange: setSelected }}
            locale={{ emptyText: <Empty description="暂无注册结果" /> }}
            pagination={{
              current: page,
              pageSize,
              total: list.data?.total || 0,
              showSizeChanger: true,
              showTotal: (total) => `共 ${total} 条结果`,
              onChange: (next, size) => {
                setPage(next);
                setPageSize(size);
              },
            }}
            columns={[
              { title: '邮箱', dataIndex: 'email', width: 200, ellipsis: true },
              secretColumn('密码', 'password'),
              secretColumn('2FA', 'totp_secret'),
              credentialColumn('Access', 'at_len', 'access_token'),
              credentialColumn('Session', 'st_len', 'session_token'),
              credentialColumn('RT', 'rt_len', 'refresh_token'),
              credentialColumn('Codex RT', 'codex_rt_len', 'codex.refresh_token'),
              {
                title: 'Plus 状态',
                width: 132,
                render: (_, row) =>
                  row.plus_check ? (
                    <InvitationStatus status={row.plus_check.status} />
                  ) : (
                    <Typography.Text type="secondary">未检测</Typography.Text>
                  ),
              },
              {
                title: '操作',
                fixed: 'right',
                width: 132,
                render: (_, row) => (
                  // 三个全称按钮会把这一列撑到 280px；运营明确要求收窄，所以用短标签 + Tooltip。
                  <Space size={0}>
                    <Tooltip title="查看该账号的详情">
                      <Button size="small" type="link" onClick={() => setRowDetail(row)}>
                        详情
                      </Button>
                    </Tooltip>
                    <Tooltip title="查看完整凭证">
                      <Button
                        size="small"
                        type="link"
                        loading={busy === `credentials:${row.email}`}
                        disabled={!!busy}
                        onClick={() => void showCredentials(row.email)}
                      >
                        凭证
                      </Button>
                    </Tooltip>
                    <Tooltip title="Codex 授权">
                      <Button
                        size="small"
                        type="link"
                        disabled={!!busy}
                        onClick={() => void authorize([row.email])}
                      >
                        授权
                      </Button>
                    </Tooltip>
                  </Space>
                ),
              },
            ]}
          />
        </Card>
        <InvitationRunPanel />
      </Space>
      <Drawer
        title={`凭证 · ${credential?.email || ''}`}
        open={!!credential}
        onClose={() => setCredential(undefined)}
        width={760}
        extra={
          <Space>
            <Button onClick={() => void copy(JSON.stringify(credential, null, 2))}>
              复制全部 JSON
            </Button>
            <Button
              onClick={() =>
                downloadInvitationExport({
                  text: JSON.stringify(credential, null, 2),
                  filename: `${credential?.email || 'credentials'}.json`,
                  mime: 'application/json',
                })
              }
            >
              下载 JSON
            </Button>
          </Space>
        }
      >
        {credential && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {credentialRows(credential).map((row) => (
              <div key={row.key}>
                <Space style={{ marginBottom: 8 }}>
                  <Typography.Text strong>{credentialFieldLabel(row.key)}</Typography.Text>
                  <Tag>长度 {row.value.length}</Tag>
                  <Button size="small" onClick={() => void copy(row.value)}>
                    复制
                  </Button>
                </Space>
                <Input.TextArea
                  readOnly
                  value={row.value}
                  rows={2}
                  style={{ fontFamily: 'monospace' }}
                />
              </div>
            ))}
            {!credentialRows(credential).length && <Empty description="没有凭证字段" />}
          </Space>
        )}
      </Drawer>
      <AdminDetailDrawer
        title={`账号详情 · ${rowDetail?.email || ''}`}
        subtitle="令牌只显示长度；点复制会按需读取完整凭证。"
        open={!!rowDetail}
        onClose={() => setRowDetail(null)}
        extra={
          <>
            <Button
              disabled={!rowDetail || !!busy}
              onClick={() => rowDetail && void showCredentials(rowDetail.email)}
            >
              查看完整凭证
            </Button>
            <Button
              disabled={!rowDetail || !!busy}
              onClick={() => rowDetail && void showCredentials(rowDetail.email, true)}
            >
              编辑
            </Button>
            <Button
              disabled={!rowDetail || !!busy}
              onClick={() => {
                if (!rowDetail) return;
                setPushEmail(rowDetail.email);
                setPushResult(undefined);
              }}
            >
              推送面板
            </Button>
            <Button
              danger
              disabled={!rowDetail || !!busy}
              onClick={() => rowDetail && confirmDelete([rowDetail.email])}
            >
              删除
            </Button>
          </>
        }
        groups={detailGroups}
      />
      <Modal
        title={`编辑凭证 · ${editing?.email || ''}`}
        open={!!editing}
        onCancel={closeEdit}
        onOk={() => void saveEdit()}
        confirmLoading={busy === 'save'}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          message="仅修改本地记录，不会同步到 OpenAI。"
          description="这里修改密码不会更改账号密码。登录流程将使用保存的值。清空字段表示主动删除该值。"
          style={{ marginBottom: 16 }}
        />
        <Form layout="vertical">
          <Form.Item label="密码">
            <Input
              aria-label="编辑密码"
              value={editPassword}
              onChange={(event) => setEditPassword(event.target.value)}
              autoComplete="off"
            />
          </Form.Item>
          <Form.Item
            label="2FA Secret"
            extra="支持 Base32、带空格、小写或 otpauth:// 链接。覆盖后原 Secret 无法恢复。"
          >
            <Input
              aria-label="编辑 2FA Secret"
              value={editSecret}
              onChange={(event) => setEditSecret(event.target.value)}
              autoComplete="off"
            />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={`推送面板 · ${pushEmail}`}
        open={!!pushEmail}
        onCancel={() => setPushEmail('')}
        onOk={() => void push()}
        confirmLoading={busy === 'push'}
        okText="推送"
        cancelText="关闭"
      >
        <Alert
          type="info"
          showIcon
          message="手动推送使用服务配置中的地址与密钥，即使自动导出开关关闭也会执行。"
          style={{ marginBottom: 16 }}
        />
        <Checkbox.Group
          options={[
            { label: 'CPA', value: 'cpa' },
            { label: 'Sub2API', value: 'sub2api' },
          ]}
          value={targets}
          onChange={(values) => setTargets(values.map(String))}
        />
        {pushResult && (
          <Space direction="vertical" style={{ width: '100%', marginTop: 16 }}>
            {pushResult.error ? (
              <Alert type="error" showIcon message={pushResult.error} />
            ) : (
              targets.map((target) => (
                <Alert
                  key={target}
                  type={pushResult[target]?.ok === false ? 'error' : 'success'}
                  showIcon
                  message={`${target.toUpperCase()}：${pushResult[target]?.ok === false ? '推送失败' : '推送完成'}`}
                  description={
                    <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0 }}>
                      {JSON.stringify(pushResult[target], null, 2)}
                    </pre>
                  }
                />
              ))
            )}
          </Space>
        )}
      </Modal>
      <Modal
        title={`导出 · ${exported?.label || ''}（${exported?.count || 0} 个账号）`}
        open={!!exported}
        onCancel={() => setExported(undefined)}
        width={760}
        footer={
          <Space>
            <Button onClick={() => setExported(undefined)}>关闭</Button>
            <Button onClick={() => void copy(exported?.text || '')}>复制全部</Button>
            <Button type="primary" onClick={() => exported && downloadInvitationExport(exported)}>
              下载 {exported?.filename}
            </Button>
          </Space>
        }
      >
        <Input.TextArea
          value={exported?.text || ''}
          readOnly
          rows={16}
          style={{ fontFamily: 'monospace' }}
        />
      </Modal>
    </AdminPage>
  );
}

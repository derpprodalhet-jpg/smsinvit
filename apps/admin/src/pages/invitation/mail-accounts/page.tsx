import { useEffect, useState, type Key } from 'react';
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { AdminPage } from '../../../components/admin-page';
import { AdminDetailDrawer, type DetailGroup } from '../../../components/admin-detail-drawer';
import { InvitationStatus } from '../../../components/invitation-status';
import { InvitationPhoneStatus, type InvitationPhoneRecord } from '../../../components/invitation-phone-status';
import { proxyUsageLabel, proxyUsageTone } from '../../../lib/invitation';
import { unknownLabel } from '../../../lib/zh-label';
import { mailSourceLabel } from '../../../lib/zh-maps';
import { localizedText } from '../../../lib/zh-text';
import {
  invitationAction,
  invitationApi,
  invitationErrorMessage,
  invitationTime,
} from '../../../lib/invitation-api';
import { useInvitationQuery } from '../../../lib/use-invitation-query';
import { useRouter } from '../../../lib/navigation';
import {
  importLineCount,
  importLineErrors,
  type ImportLineError,
  type MailProvider,
} from './model';

type Account = InvitationPhoneRecord & {
  email: string;
  kind: string;
  status: string;
  fail_reason?: string;
  /** -1 未记录 / 1 有代理 / 0 直连 */
  used_proxy?: number;
  imported_at?: number;
  proxy?: string;
};
type Accounts = { items: Account[]; total: number; by_kind?: Record<string, { total: number }> };
const statuses = ['available', 'in_use', 'registered', 'done', 'failed'];
/**
 * 筛选下拉与批量删除用的短标签。
 *
 * 列表的「账号类型」列只显示三个身份词（邮箱 / GPT / Codex，见 invitation-status），
 * 但**筛选必须能单独选中每一个状态**，否则「使用中」「上次失败」就没法筛了。
 * 所以这里保留五个选项、只压成短词；「使用中 / 上次失败」是任务状态而不是身份，
 * 在下拉里单列，不和三个身份词混在一起。
 */
const labels: Record<string, string> = {
  available: '邮箱',
  in_use: '使用中',
  registered: 'GPT',
  done: 'Codex',
  failed: '上次失败',
};
/** 使用中与待兑换都受保护：前者正在执行，后者已进账号池等待兑换。 */
const protectedStatuses = ['in_use', 'registered'];

/** 代理只按 `used_proxy` 说明真实用法；空值不能像以前那样写成「直连」。 */
function accountProxyTag(row: Account) {
  return (
    <Tooltip title={row.proxy || '未记录代理地址'}>
      <Tag color={proxyUsageTone(row.used_proxy)}>{proxyUsageLabel(row.used_proxy)}</Tag>
    </Tooltip>
  );
}

export default function MailAccountsPage() {
  const router = useRouter();
  const [page, setPage] = useState(1),
    [pageSize, setPageSize] = useState(20);
  const [status, setStatus] = useState(''),
    [kind, setKind] = useState('');
  const [selected, setSelected] = useState<Key[]>([]),
    [bulkStatus, setBulkStatus] = useState<string>();
  /**
   * 当前正在执行的动作标识（按「动作 + 对象」区分）：每个按钮在自己的 key
   * 与 busy 相等时才转圈。单个布尔会让整页一起转圈 —— 点一行的「重置」时
   * 所有行、所有批量按钮都会转，所以这里存的是具体动作的 key。
   * busy 不为 null 仍旧表示「有操作在进行」，用作重复点击的守卫。
   */
  const [busy, setBusy] = useState<string | null>(null),
    [importOpen, setImportOpen] = useState(false);
  const [detail, setDetail] = useState<Account | null>(null);
  const [importKind, setImportKind] = useState(''),
    [text, setText] = useState('');
  const [importResult, setImportResult] = useState(''),
    [lineErrors, setLineErrors] = useState<ImportLineError[]>([]);
  const providers = useInvitationQuery<{ providers: MailProvider[]; current: string }>(
    '/mail/providers',
  );
  const imports = (providers.data?.providers || []).filter((provider) => provider.pooled);
  const current = imports.find((provider) => provider.kind === importKind);
  const query = new URLSearchParams({
    status,
    kind,
    limit: String(pageSize),
    offset: String((page - 1) * pageSize),
  });
  const list = useInvitationQuery<Accounts>(`/accounts?${query}`);
  useEffect(() => {
    setSelected([]);
  }, [page, pageSize, status, kind]);
  useEffect(() => {
    if (imports.length && !imports.some((provider) => provider.kind === importKind)) {
      setImportKind(
        imports.some((provider) => provider.kind === providers.data?.current)
          ? providers.data!.current
          : imports[0].kind,
      );
    }
  }, [providers.data, importKind]);

  /**
   * 共用的请求函数。`key` 是本次动作的稳定标识（行内动作带该行邮箱），由调用方传入，
   * 不用「最后一次点击」之类的全局可变状态去猜 —— 否则并发/异步返回时会把 key 认错。
   */
  async function mutate(key: string, path: string, payload?: object, method = 'POST') {
    setBusy(key);
    try {
      const result =
        method === 'DELETE'
          ? await invitationApi<any>(path, { method })
          : await invitationAction<any>(path, payload);
      message.success(
        `操作完成${result.deleted !== undefined ? `，已删除 ${result.deleted} 个` : result.reset !== undefined ? `，已重置 ${result.reset} 个` : result.released !== undefined ? `，已释放 ${result.released} 个` : ''}`,
      );
      setSelected([]);
      await list.reload();
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }
  function confirm(title: string, action: () => Promise<void>, danger = false) {
    Modal.confirm({
      title,
      content: '执行中或结果不明的邮箱受服务端保护，不能修改。',
      okText: '确定',
      cancelText: '取消',
      okButtonProps: { danger },
      onOk: action,
    });
  }
  async function doImport() {
    if (!text.trim() || !importKind) {
      message.warning('请选择邮箱来源并填写要导入的邮箱');
      return;
    }
    setBusy('import');
    setLineErrors([]);
    setImportResult('');
    try {
      const result = await invitationAction<any>('/import', { text, kind: importKind });
      setImportResult(
        `解析 ${result.parsed} 行，新增 ${result.inserted}，更新 ${result.updated}，跳过 ${result.skipped}`,
      );
      setText('');
      message.success('导入完成');
      await list.reload();
    } catch (error) {
      const errors = importLineErrors(error);
      setLineErrors(errors);
      setImportResult(
        errors.length
          ? `有 ${errors.length} 行不合法，整批已拒绝，邮箱池未被改动。`
          : invitationErrorMessage(error),
      );
    } finally {
      setBusy(null);
    }
  }
  /** 来源一律显示中文名；未知来源不能再漏出 kind 原文。 */
  const sourceLabel = (value?: string) =>
    providers.data?.providers.find((provider) => provider.kind === value)?.display_name ||
    unknownLabel(value);
  const detailGroups: DetailGroup[] = detail
    ? [
        {
          key: 'account',
          title: '账号',
          items: [
            { key: 'email', label: '邮箱', children: detail.email },
            { key: 'source', label: '来源', children: sourceLabel(detail.kind) },
            {
              key: 'status',
              label: '账号类型',
              children: <InvitationStatus status={detail.status} context="mail-account" />,
            },
            {
              key: 'phone',
              label: '接码',
              children: <InvitationPhoneStatus record={detail} />,
            },
            {
              key: 'proxy',
              label: '代理',
              children: accountProxyTag(detail),
            },
            {
              key: 'fail',
              label: '失败原因',
              children: localizedText(detail.fail_reason),
            },
            {
              key: 'imported',
              label: '导入时间',
              children: invitationTime(detail.imported_at ?? null),
            },
          ],
        },
      ]
    : [];
  return (
    <AdminPage
      title="邮箱管理"
      description="管理邀请注册使用的邮箱池与来源。"
      extra={
        <Button type="primary" onClick={() => setImportOpen(true)}>
          导入邮箱
        </Button>
      }
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {(list.error || providers.error) && (
          <Alert
            type="error"
            showIcon
            message={list.error || providers.error}
            action={
              <Button
                onClick={() => {
                  void list.reload();
                  void providers.reload();
                }}
              >
                重试
              </Button>
            }
          />
        )}
        <Card>
          <Space wrap style={{ marginBottom: 16 }}>
            <Select
              aria-label="邮箱状态"
              value={status}
              style={{ width: 150 }}
              onChange={(value) => {
                setStatus(value);
                setPage(1);
              }}
              options={[
                { value: '', label: '全部账号类型' },
                ...statuses.map((value) => ({ value, label: labels[value] })),
              ]}
            />
            <Select
              aria-label="邮箱来源"
              value={kind}
              style={{ width: 220 }}
              onChange={(value) => {
                setKind(value);
                setPage(1);
              }}
              options={[
                { value: '', label: '全部来源' },
                ...imports.map((provider) => ({
                  value: provider.kind,
                  label: `${provider.display_name} (${list.data?.by_kind?.[provider.kind]?.total || 0})`,
                })),
              ]}
            />
            <Button loading={list.loading} onClick={() => void list.reload()}>
              刷新
            </Button>
            <Button
              loading={busy === 'reset-failed'}
              disabled={busy !== null}
              onClick={() =>
                confirm('将所有失败邮箱重置为可用？', () =>
                  mutate('reset-failed', '/accounts/reset_failed'),
                )
              }
            >
              重试失败邮箱
            </Button>
            <Button
              loading={busy === 'release-stale'}
              disabled={busy !== null}
              onClick={() =>
                confirm('释放超过 30 分钟的卡死邮箱？', () =>
                  mutate('release-stale', '/accounts/release_stale'),
                )
              }
            >
              释放卡死邮箱
            </Button>
          </Space>
          <Space wrap style={{ marginBottom: 16 }}>
            <Button
              loading={busy === 'bulk-reset'}
              disabled={!selected.length || busy !== null}
              onClick={() =>
                confirm(`重置选中的 ${selected.length} 个邮箱为可用？已保存凭证保持不变。`, () =>
                  mutate('bulk-reset', '/accounts/bulk_reset', { emails: selected }),
                )
              }
            >
              重置选中 ({selected.length})
            </Button>
            <Button
              danger
              loading={busy === 'bulk-delete:selected'}
              disabled={!selected.length || busy !== null}
              onClick={() =>
                confirm(
                  `永久删除选中的 ${selected.length} 个邮箱？`,
                  () =>
                    mutate('bulk-delete:selected', '/accounts/bulk_delete', { emails: selected }),
                  true,
                )
              }
            >
              删除选中 ({selected.length})
            </Button>
            <Select
              aria-label="按状态批量删除"
              value={bulkStatus}
              placeholder="按状态批量删除"
              style={{ width: 180 }}
              onChange={setBulkStatus}
              options={[
                ...statuses.map((value) => ({ value, label: `删除全部${labels[value]}` })),
                { value: 'all', label: '删除全部邮箱' },
              ]}
            />
            <Button
              danger
              loading={busy === 'bulk-delete:status'}
              disabled={!bulkStatus || busy !== null}
              onClick={() =>
                confirm(
                  bulkStatus === 'all'
                    ? '永久删除邮箱池的全部邮箱（含未注册邮箱）？'
                    : `永久删除全部${labels[bulkStatus!] || bulkStatus}邮箱？`,
                  () =>
                    mutate('bulk-delete:status', '/accounts/bulk_delete', { status: bulkStatus }),
                  true,
                )
              }
            >
              执行批量删除
            </Button>
          </Space>
          <Table<Account>
            rowKey="email"
            loading={list.loading || busy !== null}
            dataSource={list.data?.items || []}
            scroll={{ x: 940 }}
            rowSelection={{
              selectedRowKeys: selected,
              onChange: setSelected,
              getCheckboxProps: (row) => ({ disabled: protectedStatuses.includes(row.status) }),
            }}
            locale={{ emptyText: <Empty description="暂无邮箱，点击导入邮箱添加" /> }}
            pagination={{
              current: page,
              pageSize,
              total: list.data?.total || 0,
              showSizeChanger: true,
              showTotal: (total) => `共 ${total} 个邮箱`,
              onChange: (next, size) => {
                setPage(next);
                setPageSize(size);
              },
            }}
            columns={[
              { title: '邮箱', dataIndex: 'email', width: 260, ellipsis: true },
              {
                title: '来源',
                dataIndex: 'kind',
                width: 104,
                // 列表只放短名（CF临时 / Outlook / iCloud），全称（「CF Worker 临时邮箱」）
                // 放悬浮说明 —— 否则这一列会折成两行，把整张表撑高。
                render: (value: string) => (
                  <Tooltip title={sourceLabel(value)}>
                    <Tag style={{ marginInlineEnd: 0, whiteSpace: 'nowrap' }}>
                      {mailSourceLabel(value)}
                    </Tag>
                  </Tooltip>
                ),
              },
              {
                title: '账号类型',
                dataIndex: 'status',
                width: 108,
                render: (value) => <InvitationStatus status={value} context="mail-account" />,
              },
              {
                title: '接码',
                dataIndex: 'phone_bound_ok',
                width: 88,
                // 只留两个词：已接 / 未接。原因（接码失败 / 兑换时到 add-phone）放悬浮。
                render: (_, row) => <InvitationPhoneStatus record={row} compact />,
              },
              {
                title: '导入',
                dataIndex: 'imported_at',
                width: 152,
                render: (value: number) => invitationTime(value ?? null),
              },
              {
                title: '操作',
                fixed: 'right',
                width: 230,
                render: (_, row) => (
                  <Space>
                    <Button size="small" type="link" onClick={() => setDetail(row)}>
                      详情
                    </Button>
                    <Button
                      size="small"
                      type="link"
                      disabled={row.status !== 'available'}
                      onClick={() =>
                        router.push(`/invitation/register?email=${encodeURIComponent(row.email)}`)
                      }
                    >
                      使用
                    </Button>
                    <Button
                      size="small"
                      type="link"
                      loading={busy === `reset:${row.email}`}
                      disabled={!['done', 'failed'].includes(row.status) || busy !== null}
                      onClick={() =>
                        confirm(`将 ${row.email} 重置为可用？`, () =>
                          mutate(
                            `reset:${row.email}`,
                            `/accounts/reset/${encodeURIComponent(row.email)}`,
                          ),
                        )
                      }
                    >
                      重置
                    </Button>
                    <Button
                      size="small"
                      type="link"
                      danger
                      loading={busy === `delete:${row.email}`}
                      disabled={protectedStatuses.includes(row.status) || busy !== null}
                      onClick={() =>
                        confirm(
                          `永久删除 ${row.email}？`,
                          () =>
                            mutate(
                              `delete:${row.email}`,
                              `/accounts/${encodeURIComponent(row.email)}`,
                              undefined,
                              'DELETE',
                            ),
                          true,
                        )
                      }
                    >
                      删除
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      </Space>
      <AdminDetailDrawer
        title={`邮箱详情 · ${detail?.email || ''}`}
        subtitle="列表只保留关键字段；来源、接码、代理与失败原因都在这里。"
        open={!!detail}
        onClose={() => setDetail(null)}
        groups={detailGroups}
      />
      <Drawer
        title="导入邮箱"
        open={importOpen}
        onClose={() => setImportOpen(false)}
        width={640}
        extra={
          <Button
            type="primary"
            loading={busy === 'import'}
            disabled={!imports.length}
            onClick={() => void doImport()}
          >
            导入
          </Button>
        }
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Typography.Text>邮箱来源</Typography.Text>
          <Select
            aria-label="导入邮箱来源"
            value={importKind || undefined}
            style={{ width: '100%' }}
            onChange={setImportKind}
            placeholder="选择来源"
            options={imports.map((provider) => ({
              value: provider.kind,
              label: provider.display_name,
            }))}
          />
          {current && (
            <Alert
              type="info"
              showIcon
              message={`每行一个邮箱，以 ---- 分隔，共 ${current.line_segments || 4} 段。`}
              description={current.import_hint}
            />
          )}
          {!providers.loading && !imports.length && (
            <Empty description="当前没有支持导入的邮箱来源" />
          )}
          <Input.TextArea
            aria-label="邮箱导入内容"
            rows={12}
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={current?.import_placeholder || '选择邮箱来源后粘贴邮箱内容'}
            style={{ fontFamily: 'monospace' }}
          />
          <Typography.Text type="secondary">
            待导入 {importLineCount(text)} 行；任一行不合法时，整批拒绝。
          </Typography.Text>
          {importResult && (
            <Alert type={lineErrors.length ? 'error' : 'info'} showIcon message={importResult} />
          )}
          {!!lineErrors.length && (
            <Table<ImportLineError>
              size="small"
              rowKey="line"
              dataSource={lineErrors}
              pagination={false}
              scroll={{ y: 260 }}
              columns={[
                { title: '行号', dataIndex: 'line', width: 90, render: (line) => `第 ${line} 行` },
                {
                  title: '原因',
                  dataIndex: 'error',
                  render: (error: string) => localizedText(error),
                },
              ]}
            />
          )}
        </Space>
      </Drawer>
    </AdminPage>
  );
}

import { useEffect, useState, type Key } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Typography,
  message,
} from 'antd';
import { AdminPage } from '../../../components/admin-page';
import { InvitationStatus } from '../../../components/invitation-status';
import {
  copyInvitationText,
  downloadInvitationExport,
  invitationAction,
  invitationApi,
  invitationErrorMessage,
  invitationTime,
} from '../../../lib/invitation-api';
import { useInvitationQuery } from '../../../lib/use-invitation-query';
import { cdkStatusLabel } from '../../../lib/invitation';
import { runEventLabel } from '../../../lib/zh-maps';
import { localizedText } from '../../../lib/zh-text';
import { cdkCanMutate, cdkExportPayload, collectFilteredCodes } from './model';

type Cdk = {
  code: string;
  status: string;
  email?: string;
  inviter_email?: string;
  inviter_subject?: string;
  invite_received_at?: number;
  proxy_used?: string;
  attempts?: number;
  fail_count?: number;
  batch?: string;
  created_at?: number;
  note?: string;
  error?: string;
};
type CdkList = { items: Cdk[]; total: number; batches: string[]; stats: Record<string, number> };
type Job = {
  job_id: string;
  email?: string;
  status: string;
  stage?: string;
  stage_label?: string;
  progress?: number;
  attempt?: number;
  elapsed?: number;
  proxy?: string;
  error?: string;
  started_at?: number;
};
type Event = { id?: string; event: string; detail?: string; code?: string; created_at?: number };
type Records = { code: Cdk; jobs: Job[]; events: Event[] };
type ExportResult = {
  text?: string;
  b64?: string;
  mode?: string;
  mime?: string;
  filename?: string;
  count?: number;
  label?: string;
};
/**
 * 状态筛选与统计卡片的文案：全部取自 `lib/invitation.ts` 的卡密口径
 * （终态「已核销」、未使用橘黄），避免筛选写「已完成」而列表写别的。
 */
const CDK_FILTER_STATUSES = ['unused', 'bound', 'queued', 'running', 'done', 'failed', 'void'];
const statusOptions = [
  { value: '', label: '全部状态' },
  ...CDK_FILTER_STATUSES.map((value) => ({ value, label: cdkStatusLabel(value) })),
];
const statLabels: [string, string][] = [
  ['total', '总数'],
  ...CDK_FILTER_STATUSES.map((value) => [value, cdkStatusLabel(value)] as [string, string]),
  ['mail_pool', '可兑换账号'],
];

export default function CdkPage() {
  const [page, setPage] = useState(1),
    [pageSize, setPageSize] = useState(20);
  const [status, setStatus] = useState(''),
    [batch, setBatch] = useState('');
  const [searchInput, setSearchInput] = useState(''),
    [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Key[]>([]),
    [busy, setBusy] = useState('');
  const [generateOpen, setGenerateOpen] = useState(false),
    [generated, setGenerated] = useState<string[]>([]);
  const [exported, setExported] = useState<ExportResult>();
  const [recordCode, setRecordCode] = useState('');
  const [editing, setEditing] = useState<Cdk>(),
    [editNote, setEditNote] = useState(''),
    [editBatch, setEditBatch] = useState('');
  const [form] = Form.useForm();
  const filters = { status, batch, search };
  const list = useInvitationQuery<CdkList>(
    `/cdk/list?${new URLSearchParams({ ...filters, limit: String(pageSize), offset: String((page - 1) * pageSize) })}`,
  );
  const statsQuery = useInvitationQuery<{ stats: Record<string, number> }>('/cdk/stats');
  const stats = statsQuery.data?.stats || list.data?.stats || {};
  const formats = useInvitationQuery<{ formats: { id: string; label: string; note?: string }[] }>(
    '/cdk/export/formats',
    0,
  );
  const skus = useInvitationQuery<{ items: { id: string; name: string }[] }>(
    generateOpen ? '/cdk/skus' : null,
    0,
  );
  const records = useInvitationQuery<Records>(
    recordCode ? `/cdk/records?code=${encodeURIComponent(recordCode)}` : null,
    2000,
  );
  /**
   * 这里曾经有一张「实时执行监控」卡：作业表、事件流水、最近完成/失败表全部
   * 来自 `/cdk/monitor`，和「运行记录」页逐项重复（那边的作业表带 `itemsLimit`
   * 截断提示、事件流水和全局计数）。按计划整卡删除，本页只保留服务端的全局计数
   * （统计卡）与卡密台账；执行明细请到「运行记录」按状态与类型筛选。
   */
  const selectedBlocked = selected.some((code) => {
    const row = list.data?.items.find((item) => item.code === code);
    return !row || !cdkCanMutate(row);
  });
  useEffect(() => {
    setSelected([]);
  }, [page, pageSize, status, batch, search]);
  useEffect(() => {
    if (generateOpen && skus.data?.items.length === 1 && !form.getFieldValue('skuId'))
      form.setFieldValue('skuId', skus.data.items[0].id);
  }, [generateOpen, skus.data, form]);

  async function copy(text: string) {
    if (!text) {
      message.info('没有可复制的卡密');
      return;
    }
    try {
      await copyInvitationText(text);
      message.success('已复制');
    } catch (error) {
      message.error(invitationErrorMessage(error));
    }
  }
  async function copyFiltered() {
    setBusy('copy');
    try {
      const codes = await collectFilteredCodes(filters, invitationApi);
      await copy(codes.join('\n'));
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  async function reloadAll() {
    await Promise.all([list.reload(), statsQuery.reload()]);
  }
  async function mutate(path: string, payload: object, success: string) {
    setBusy('mutation');
    try {
      await invitationAction(path, payload);
      message.success(success);
      setSelected([]);
      await reloadAll();
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  function confirmMutation(row: Cdk, action: 'reset' | 'unbind' | 'void') {
    if (!cdkCanMutate(row)) return;
    const descriptions = {
      reset: '清除失败计数并恢复可再次开始，保留绑定邮箱。',
      unbind: '释放绑定邮箱回到邮箱池，卡密恢复未使用。',
      void: '作废卡密并释放绑定邮箱，卡密不再可兑换。',
    };
    const labels = { reset: '重置', unbind: '解绑', void: '作废' };
    Modal.confirm({
      title: `${labels[action]} ${row.code}？`,
      content: descriptions[action],
      okText: labels[action],
      cancelText: '取消',
      okButtonProps: { danger: action === 'void' },
      onOk: () => mutate(`/cdk/${action}`, { code: row.code }, `已${labels[action]}`),
    });
  }
  function deleteSelected() {
    Modal.confirm({
      title: `永久删除 ${selected.length} 张卡密？`,
      content: '占用邮箱会先被释放回池。此操作无法恢复；执行中或结果不明的卡密不能修改。',
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => mutate('/cdk/delete', { codes: selected.map(String) }, '已删除选中卡密'),
    });
  }
  async function generate() {
    let values;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setBusy('generate');
    try {
      const result = await invitationAction<{
        codes: string[];
        inserted: number;
        batch: string;
        skipped?: number;
      }>('/cdk/generate', values);
      setGenerated(result.codes || []);
      message.success(
        `已生成 ${result.inserted} 张，批次 ${result.batch}${result.skipped ? `，重复跳过 ${result.skipped}` : ''}`,
      );
      setPage(1);
      await reloadAll();
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  async function doExport(format: string) {
    setBusy('export');
    try {
      const result = await invitationAction<ExportResult>(
        '/cdk/export',
        cdkExportPayload(format, selected.map(String), filters),
      );
      if (result.mode === 'download') {
        downloadInvitationExport(result);
        message.success(`已下载 ${result.filename}（${result.count || 0} 张）`);
      } else setExported(result);
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  async function saveNote() {
    if (!editing) return;
    const payload: { code: string; note?: string; batch?: string } = { code: editing.code };
    if (editNote !== (editing.note || '')) payload.note = editNote;
    if (editBatch !== (editing.batch || '')) payload.batch = editBatch;
    if (Object.keys(payload).length === 1) {
      setEditing(undefined);
      return;
    }
    setBusy('note');
    try {
      await invitationAction('/cdk/update', payload);
      message.success('已保存');
      setEditing(undefined);
      await list.reload();
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  const jobsColumns = [
    { title: '邮箱', dataIndex: 'email', width: 220, ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value: string) => <InvitationStatus status={value} />,
    },
    {
      title: '阶段',
      width: 180,
      render: (_: unknown, job: Job) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{job.stage_label || localizedText(job.stage, '等待执行')}</Typography.Text>
          {job.elapsed !== undefined && (
            <Typography.Text type="secondary">已用时 {job.elapsed} 秒</Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 160,
      render: (value: number) => <Progress percent={value || 0} size="small" />,
    },
    { title: '尝试', dataIndex: 'attempt', width: 70 },
    { title: '代理', dataIndex: 'proxy', width: 200, ellipsis: true },
    {
      title: '错误',
      dataIndex: 'error',
      width: 220,
      ellipsis: true,
      render: (value: string) => localizedText(value),
    },
  ];
  const eventColumns = [
    { title: '时间', dataIndex: 'created_at', width: 180, render: invitationTime },
    {
      title: '事件',
      dataIndex: 'event',
      width: 180,
      render: (value: string) => runEventLabel(value),
    },
    {
      title: '详情',
      dataIndex: 'detail',
      width: 400,
      render: (value: string) => (
        <Typography.Paragraph
          style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}
        >
          {value || '—'}
        </Typography.Paragraph>
      ),
    },
  ];
  return (
    <AdminPage
      title="卡密管理"
      description="生成关联邀请商品与库存台账的卡密，查看卡密状态与使用记录；执行明细见「运行记录」。统计里的「总数」是本模块全部卡密（含历史导入与人工创建），等于「卡密批次」页「已登记批次 + 未登记批次」的合计；没有批次归属的历史卡密在批次页单列为「未登记批次」，只有本站生成的卡密带批次号，且该批次号与批次页显示一致。"
      extra={
        <Button
          type="primary"
          onClick={() => {
            setGenerateOpen(true);
            setGenerated([]);
            form.resetFields();
          }}
        >
          生成卡密
        </Button>
      }
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Row gutter={[12, 12]}>
          {statLabels.map(([key, label]) => (
            <Col key={key} xs={12} sm={8} lg={4}>
              <Card size="small">
                <Statistic title={label} value={stats[key] || 0} />
              </Card>
            </Col>
          ))}
        </Row>
        <Typography.Text type="secondary">
          以上是全局计数（服务端整表统计，与分页无关）。「总数」是本模块全部卡密（含没有批次归属的历史导入 /
          手工创建卡密），与「卡密批次」页的「全部卡密」是同一个口径；「可兑换账号」统计的是邮箱池里当前可用的账号，
          不是卡密数。执行中/排队的作业、事件流水与作业列表已统一到「运行记录」页，可按状态与类型筛选查看。
        </Typography.Text>
        {(list.error || statsQuery.error) && (
          <Alert
            type="error"
            showIcon
            message={list.error || statsQuery.error}
            action={<Button onClick={() => void reloadAll()}>重试</Button>}
          />
        )}
        <Card>
          <Space wrap style={{ marginBottom: 16 }}>
            <Select
              aria-label="卡密状态"
              value={status}
              onChange={(value) => {
                setStatus(value);
                setPage(1);
              }}
              style={{ width: 150 }}
              options={statusOptions}
            />
            <Select
              aria-label="卡密批次"
              allowClear
              placeholder="全部批次"
              value={batch || undefined}
              onChange={(value) => {
                setBatch(value || '');
                setPage(1);
              }}
              style={{ width: 220 }}
              options={(list.data?.batches || []).map((value) => ({ value, label: value }))}
            />
            <Input.Search
              aria-label="搜索卡密"
              allowClear
              placeholder="卡密 / 邮箱 / 邀请方"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              onSearch={(value) => {
                setSearch(value);
                setPage(1);
              }}
              style={{ width: 260 }}
              enterButton="查询"
            />
            <Button loading={list.loading} onClick={() => void reloadAll()}>
              刷新
            </Button>
          </Space>
          <Space wrap style={{ marginBottom: 16 }}>
            <Button
              disabled={!selected.length || !!busy}
              onClick={() => void copy(selected.map(String).join('\n'))}
            >
              复制选中 ({selected.length})
            </Button>
            <Button loading={busy === 'copy'} disabled={!!busy} onClick={() => void copyFiltered()}>
              复制当前筛选全部
            </Button>
            <Dropdown
              menu={{
                items: (formats.data?.formats || []).map((format) => ({
                  key: format.id,
                  label: `${format.label}${format.note ? ` · ${format.note}` : ''}`,
                })),
                onClick: ({ key }) => void doExport(key),
              }}
              disabled={!!busy}
            >
              <Button
                loading={busy === 'export'}
                disabled={!formats.data?.formats.length || !!formats.error}
              >
                {selected.length ? '导出选中' : '导出当前筛选全部'}
              </Button>
            </Dropdown>
            <Button
              danger
              disabled={!selected.length || selectedBlocked || !!busy}
              onClick={deleteSelected}
            >
              删除选中
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
          {list.data?.items.some((row) => row.code === '***') && (
            <Alert
              type="info"
              showIcon
              message="卡密内容已脱敏；查看、复制和管理卡密内容需要凭证权限。"
              style={{ marginBottom: 12 }}
            />
          )}
          <Table<Cdk>
            rowKey="viewKey"
            loading={list.loading}
            dataSource={(list.data?.items || []).map((row, index) => ({
              ...row,
              viewKey: row.code === '***' ? `masked-${index}` : row.code,
            }))}
            scroll={{ x: 1150 }}
            rowSelection={{
              selectedRowKeys: selected,
              onChange: setSelected,
              getCheckboxProps: (row) => ({ disabled: row.code === '***' }),
            }}
            locale={{ emptyText: <Empty description="暂无卡密，点击生成卡密创建" /> }}
            pagination={{
              current: page,
              pageSize,
              total: list.data?.total || 0,
              showSizeChanger: true,
              showTotal: (total) => `共 ${total} 张卡密`,
              onChange: (next, size) => {
                setPage(next);
                setPageSize(size);
              },
            }}
            columns={[
              {
                title: '卡密',
                dataIndex: 'code',
                width: 240,
                render: (code) =>
                  code === '***' ? (
                    '已脱敏'
                  ) : (
                    <Typography.Text
                      copyable={{ text: code }}
                      code
                      style={{ overflowWrap: 'anywhere' }}
                    >
                      {code}
                    </Typography.Text>
                  ),
              },
              {
                title: '状态',
                dataIndex: 'status',
                width: 110,
                // 卡密口径：终态显示「已核销」，未使用橘黄。
                render: (value) => <InvitationStatus status={value} context="cdk" />,
              },
              { title: '邮箱', dataIndex: 'email', width: 240, ellipsis: true },
              { title: '批次', dataIndex: 'batch', width: 190, ellipsis: true },
              { title: '创建时间', dataIndex: 'created_at', width: 180, render: invitationTime },
              {
                title: '操作',
                fixed: 'right',
                width: 280,
                render: (_, row) => (
                  <Space wrap size={0}>
                    <Button
                      type="link"
                      size="small"
                      disabled={row.code === '***'}
                      onClick={() => setRecordCode(row.code)}
                    >
                      使用记录
                    </Button>
                    <Button
                      type="link"
                      size="small"
                      disabled={!cdkCanMutate(row) || !!busy}
                      onClick={() => confirmMutation(row, 'reset')}
                    >
                      重置
                    </Button>
                    <Button
                      type="link"
                      size="small"
                      disabled={row.code === '***' || !!busy}
                      onClick={() => {
                        setEditing(row);
                        setEditNote(row.note || '');
                        setEditBatch(row.batch || '');
                      }}
                    >
                      备注
                    </Button>
                    <Button
                      type="link"
                      size="small"
                      disabled={!cdkCanMutate(row) || !!busy}
                      onClick={() => confirmMutation(row, 'unbind')}
                    >
                      解绑
                    </Button>
                    <Button
                      type="link"
                      size="small"
                      danger
                      disabled={!cdkCanMutate(row) || !!busy}
                      onClick={() => confirmMutation(row, 'void')}
                    >
                      作废
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      </Space>
      <Modal
        title="生成卡密"
        open={generateOpen}
        onCancel={() => {
          setGenerateOpen(false);
          setGenerated([]);
        }}
        width={620}
        footer={
          <Space>
            <Button
              onClick={() => {
                setGenerateOpen(false);
                setGenerated([]);
              }}
            >
              关闭
            </Button>
            {!!generated.length && (
              <Button onClick={() => void copy(generated.join('\n'))}>复制本次生成</Button>
            )}
            <Button
              type="primary"
              loading={busy === 'generate'}
              disabled={!skus.data?.items.length}
              onClick={() => void generate()}
            >
              生成
            </Button>
          </Space>
        }
      >
        {skus.error && (
          <Alert type="error" showIcon message={skus.error} style={{ marginBottom: 16 }} />
        )}
        {!skus.loading && skus.data && !skus.data.items.length && (
          <Alert
            type="warning"
            message="请先在套餐管理创建启用的 Codex 邀请商品。"
            style={{ marginBottom: 16 }}
          />
        )}
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            count: 10,
            batch: '',
            prefix: 'CDK-',
            length: 20,
            note: '',
            skuId: undefined,
          }}
        >
          <Form.Item
            label="邀请商品"
            name="skuId"
            rules={[{ required: true, message: '请选择邀请商品' }]}
          >
            <Select
              aria-label="邀请商品"
              placeholder="选择所属商品"
              loading={skus.loading}
              options={(skus.data?.items || []).map((sku) => ({ value: sku.id, label: sku.name }))}
            />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                label="数量（1–5000）"
                name="count"
                rules={[
                  {
                    required: true,
                    type: 'number',
                    min: 1,
                    max: 5000,
                    message: '数量必须为 1–5000',
                  },
                ]}
              >
                <InputNumber
                  aria-label="生成数量"
                  min={1}
                  max={5000}
                  precision={0}
                  style={{ width: '100%' }}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="批次（留空为当前时间）" name="batch">
                <Input aria-label="生成批次" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label="前缀" name="prefix">
                <Input aria-label="卡密前缀" maxLength={32} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                label="随机部分长度（8–64）"
                name="length"
                rules={[
                  {
                    required: true,
                    type: 'number',
                    min: 8,
                    max: 64,
                    message: '随机长度必须为 8–64',
                  },
                ]}
              >
                <InputNumber
                  aria-label="卡密随机长度"
                  min={8}
                  max={64}
                  precision={0}
                  style={{ width: '100%' }}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item label="备注" name="note">
            <Input aria-label="生成备注" />
          </Form.Item>
        </Form>
        {!!generated.length && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Alert type="success" message={`本次生成 ${generated.length} 张卡密`} />
            <Input.TextArea
              value={generated.join('\n')}
              readOnly
              rows={6}
              style={{ fontFamily: 'monospace' }}
            />
          </Space>
        )}
      </Modal>
      <Modal
        title="编辑备注与批次"
        open={!!editing}
        onCancel={() => setEditing(undefined)}
        onOk={() => void saveNote()}
        confirmLoading={busy === 'note'}
        okText="保存"
        cancelText="取消"
      >
        <Form layout="vertical">
          <Form.Item label="备注">
            <Input value={editNote} onChange={(event) => setEditNote(event.target.value)} />
          </Form.Item>
          <Form.Item label="批次">
            <Input value={editBatch} onChange={(event) => setEditBatch(event.target.value)} />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={`导出 · ${exported?.label || ''}（${exported?.count || 0} 张）`}
        open={!!exported}
        onCancel={() => setExported(undefined)}
        width={760}
        footer={
          <Space>
            <Button onClick={() => setExported(undefined)}>关闭</Button>
            <Button onClick={() => void copy(exported?.text || '')}>复制</Button>
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
      <Drawer
        title={`使用记录 · ${recordCode}`}
        open={!!recordCode}
        onClose={() => setRecordCode('')}
        width={1000}
        extra={<Button onClick={() => void records.reload()}>刷新</Button>}
      >
        {records.error && (
          <Alert type="error" showIcon message={records.error} style={{ marginBottom: 16 }} />
        )}
        {records.data && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              bordered
              size="small"
              column={2}
              items={[
                {
                  key: 'status',
                  label: '状态',
                  children: (
                    <InvitationStatus status={records.data.code.status} context="cdk" />
                  ),
                },
                { key: 'email', label: '邮箱', children: records.data.code.email || '—' },
                { key: 'batch', label: '批次', children: records.data.code.batch || '—' },
                {
                  key: 'created',
                  label: '创建时间',
                  children: invitationTime(records.data.code.created_at || null),
                },
                {
                  key: 'inviter',
                  label: '邀请方邮箱',
                  children: records.data.code.inviter_email || '—',
                },
                {
                  key: 'subject',
                  label: '邀请信主题',
                  children: records.data.code.inviter_subject || '—',
                },
                {
                  key: 'received',
                  label: '收到邀请信',
                  children: invitationTime(records.data.code.invite_received_at || null),
                },
                { key: 'proxy', label: '实际代理', children: records.data.code.proxy_used || '—' },
                {
                  key: 'attempts',
                  label: '尝试 / 失败',
                  children: `${records.data.code.attempts || 0} / ${records.data.code.fail_count || 0}`,
                },
                { key: 'note', label: '备注', children: records.data.code.note || '—' },
                {
                  key: 'error',
                  label: '最近错误',
                  children: localizedText(records.data.code.error),
                  span: 2,
                },
              ]}
            />
            <Typography.Title level={5} style={{ margin: 0 }}>
              作业历史
            </Typography.Title>
            <Table<Job>
              rowKey="job_id"
              size="small"
              dataSource={records.data.jobs || []}
              pagination={false}
              scroll={{ x: 1200, y: 280 }}
              columns={[
                { title: '任务编号', dataIndex: 'job_id', width: 220, ellipsis: true },
                ...jobsColumns.filter((column) => column.dataIndex !== 'email'),
                { title: '开始时间', dataIndex: 'started_at', width: 180, render: invitationTime },
              ]}
            />
            <Typography.Title level={5} style={{ margin: 0 }}>
              事件流水
            </Typography.Title>
            <Table<Event>
              rowKey="viewKey"
              size="small"
              dataSource={(records.data.events || []).map((event, index) => ({
                ...event,
                viewKey: event.id || `event-${index}`,
              }))}
              pagination={false}
              scroll={{ x: 800, y: 320 }}
              columns={eventColumns}
            />
          </Space>
        )}
        {records.loading && !records.data && (
          <Typography.Text type="secondary">正在加载记录…</Typography.Text>
        )}
      </Drawer>
    </AdminPage>
  );
}

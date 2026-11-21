import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { AdminPage } from '../../../components/admin-page';
import { InvitationRunPanel } from '../../../components/invitation-run-panel';
import { InvitationStatus } from '../../../components/invitation-status';
import { adminApi } from '../../../lib/admin-api';
import {
  copyInvitationText,
  invitationErrorMessage,
  invitationTime,
} from '../../../lib/invitation-api';
import { useInvitationRuntime } from '../../../lib/invitation-runtime';
import { useInvitationQuery } from '../../../lib/use-invitation-query';
import { invitationDuration, useInvitationAdminQuery } from '../maintenance';
import { permitReleaseBody } from './permit-release';
import { runKindLabel, runKindOptions, runKindTone, runStatusLabel } from '../../../lib/invitation';
import { holdReasonLabel, runEventLabel } from '../../../lib/zh-maps';
import { resolveLabel } from '../../../lib/zh-label';
import { localizedText } from '../../../lib/zh-text';
import { CdkSyncDetails, CdkSyncTag, type CdkSyncStatus } from './cdk-sync';

type Row = {
  run_id?: string;
  job_id?: string;
  email?: string;
  status?: string;
  kind?: string;
  stage?: string;
  stage_label?: string;
  progress?: number;
  attempt?: number;
  proxy?: string;
  elapsed?: number;
  started_at?: number;
  finished_at?: number;
  error?: string;
  cdk_sync_status?: CdkSyncStatus;
  cdk_sync_error?: string;
  /** 卡密明细（无 `invitation.credentials` 权限时 cdk_code 为 `***`）。 */
  cdk_code?: string;
  cdk_code_masked?: string;
  cdk_batch?: string;
  cdk_status?: string;
  cdk_fail_count?: number;
  cdk_valid_until?: number | string | null;
};
type Hold = {
  jobId: string;
  originalStatus: string;
  reason: string;
  createdAt?: string;
  email?: string;
  jobStatus?: string;
  stage?: string;
  progress?: number;
};
type Permit = {
  slotId: number;
  ownerToken: string;
  instanceId: string;
  email?: string;
  taskId?: string;
  acquiredAt?: string;
};
type RunDetail = {
  run: Row;
  text?: string;
  truncated?: boolean;
  size?: number;
  log_path?: string;
  exists?: boolean;
  events?: any[];
};
type QueueGap = {
  jobId: string;
  email?: string;
  status?: string;
  stage?: string;
  createdAt?: number;
  ageSeconds?: number;
  detail?: string;
};
type QueueGaps = { items?: QueueGap[]; graceSeconds?: number; workerAlive?: boolean };
const PERMITS_PATH = '/api/v1/admin/invitation/execution-permits';
const HOLDS_PATH = '/api/v1/admin/invitation/task-holds';
const QUEUE_GAPS_PATH = '/api/v1/admin/invitation/queue/gaps';
const QUEUE_REDISPATCH_PATH = '/api/v1/admin/invitation/queue/redispatch';
const HOLD_RESOLUTIONS = [
  { value: 'not_started_confirmed', label: '已确认从未真正启动' },
  { value: 'interrupted_confirmed', label: '已确认外部执行结束、本次未完成' },
  { value: 'external_evidence_confirmed', label: '已按外部证据判定并在既有工具里处理' },
];
/** `/cdk/monitor` 的 summary 是**全局**计数；这里只做中文标签，不拿原始 key 当文案。 */
const SUMMARY_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  done: '已完成',
  failed: '失败',
  proxy_available: '可用代理',
  mail_pool: '可兑换账号',
  workers: '工作线程',
  max_concurrency: '并发上限',
};
const SUMMARY_ORDER = [
  'queued',
  'running',
  'done',
  'failed',
  'proxy_available',
  'mail_pool',
  'workers',
  'max_concurrency',
];
/** 全局计数条的配色：绿灯只在真的可用时亮，失败要有红色。 */
const SUMMARY_TONE: Record<string, string | undefined> = {
  queued: undefined,
  running: 'processing',
  workers: undefined,
  max_concurrency: undefined,
  mail_pool: undefined,
};
/** 只有排队中/运行中的作业才会显示「停止」。 */
function isStoppable(row: Row) {
  return row.status === 'queued' || row.status === 'running';
}
const eventColumns = [
  { title: '时间', dataIndex: 'created_at', width: 170, render: invitationTime },
  {
    title: '事件',
    dataIndex: 'event',
    width: 150,
    render: (value: string) => runEventLabel(value),
  },
  {
    title: '详情',
    dataIndex: 'detail',
    render: (value: string) => (
      <Typography.Text style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        {value || '—'}
      </Typography.Text>
    ),
  },
];
function stage(row: Row) {
  return (
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      <Space wrap>
        <Typography.Text>
          {row.stage_label || localizedText(row.stage, row.status === 'queued' ? '正在排队' : '—')}
        </Typography.Text>
        {!!row.attempt && <Tag>第 {row.attempt} 次</Tag>}
      </Space>
      {(row.status === 'running' || row.status === 'queued') && (
        <Progress percent={Math.min(100, Math.max(0, row.progress || 0))} size="small" />
      )}
      {row.proxy && (
        <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>
          代理：{row.proxy}
        </Typography.Text>
      )}
    </Space>
  );
}
/**
 * 卡密列：优先明文；`***` 表示当前账号没有卡密权限。
 *
 * 「卡密同步」不再单独占一列：它只对**人工授权**的运行有意义（本机 50 条
 * codex_auth 里只有 8 条有该状态），其余永远是「—」。真正要看的只有
 * 「待同步 / 同步失败」，所以把这个信号挂在它影响的卡密旁边；
 * 完整状态与原因仍在运行详情里（`CdkSyncDetails`）。
 */
function cdkCell(row: Row) {
  const masked = row.cdk_code === '***';
  const code = masked ? '已脱敏' : row.cdk_code || row.cdk_code_masked || '';
  const syncPending = row.cdk_sync_status === 'pending';
  const syncFailed = row.cdk_sync_status === 'failed';
  if (!code && !syncPending && !syncFailed) return '—';
  return (
    <Space direction="vertical" size={2} style={{ maxWidth: '100%' }}>
      {code ? (
        masked ? (
          <Typography.Text type="secondary">{code}</Typography.Text>
        ) : (
          <Typography.Text code style={{ overflowWrap: 'anywhere' }}>
            {code}
          </Typography.Text>
        )
      ) : (
        <Typography.Text type="secondary">未绑定卡密</Typography.Text>
      )}
      {row.cdk_batch && <Typography.Text type="secondary">{row.cdk_batch}</Typography.Text>}
      {(syncPending || syncFailed) && <CdkSyncTag status={row.cdk_sync_status} />}
    </Space>
  );
}
export default function InvitationRunsPage() {
  const [limit, setLimit] = useState(100);
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [kind, setKind] = useState('');
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<Row | null>(null);
  const [release, setRelease] = useState<Permit | null>(null);
  const [hold, setHold] = useState<Hold | null>(null);
  const [saving, setSaving] = useState(false);
  const [stopping, setStopping] = useState('');
  const [form] = Form.useForm();
  const [holdForm] = Form.useForm();
  const [gaps, setGaps] = useState<QueueGaps | null>(null);
  const [gapsLoading, setGapsLoading] = useState(false);
  const [gapsError, setGapsError] = useState('');
  const [redispatchOpen, setRedispatchOpen] = useState(false);
  const [redispatchJobId, setRedispatchJobId] = useState('');
  const [redispatchConfirmed, setRedispatchConfirmed] = useState(false);
  const [redispatching, setRedispatching] = useState(false);
  // 筛选与分页都在**服务端**做：以前是"先截取最近 N 条再在浏览器里筛"，
  // 于是更早的记录永远查不到。
  const runs = useInvitationQuery<{ items: Row[]; total?: number }>(
    `/runs?limit=${limit}&offset=${(page - 1) * limit}` +
      `&status=${encodeURIComponent(status)}&kind=${encodeURIComponent(kind)}`,
  );
  const monitor = useInvitationQuery<{
    items: Row[];
    events: any[];
    summary?: Record<string, any>;
  }>(`/cdk/monitor?limit=${limit}`, 2000);
  const permits = useInvitationAdminQuery<{ items: Permit[]; automaticReclaim: boolean }>(
    PERMITS_PATH,
  );
  const holds = useInvitationAdminQuery<{ items: Hold[]; reviewRequiresExecutorsStopped: boolean }>(
    HOLDS_PATH,
  );
  const details = useInvitationQuery<RunDetail>(
    detail?.run_id ? `/runs/${encodeURIComponent(detail.run_id)}/log?tail_kb=1024` : null,
    detail?.status === 'running' || detail?.cdk_sync_status === 'pending' ? 2000 : 0,
  );
  const { watchRun } = useInvitationRuntime();
  useEffect(() => {
    const run = details.data?.run;
    if (
      run &&
      (run.status !== detail?.status ||
        run.cdk_sync_status !== detail?.cdk_sync_status ||
        run.cdk_sync_error !== detail?.cdk_sync_error)
    )
      setDetail((previous) => (previous ? { ...previous, ...run } : previous));
  }, [details.data, detail?.status, detail?.cdk_sync_status, detail?.cdk_sync_error]);
  /**
   * 状态与类型已交给服务端；这里只做**本页内**的文本匹配。
   *
   * 邮箱/错误/run_id 在库里是加密标记，服务端没法对明文做子串匹配，所以文本搜索
   * 只能在已加载的这一页里生效 —— 界面上必须把这一点说清楚，不能让人以为搜的是全库。
   */
  const filter = (row: Row) =>
    !search ||
    `${row.email || ''} ${row.run_id || ''} ${row.job_id || ''}`
      .toLowerCase()
      .includes(search.toLowerCase());
  const runsColumns = [
    {
      title: '任务',
      key: 'task',
      width: 150,
      ellipsis: true,
      render: (_: unknown, row: Row) => row.run_id || row.job_id,
    },
    { title: '邮箱', dataIndex: 'email', width: 200, ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value: string) => <InvitationStatus status={value} />,
    },
    {
      title: '类型',
      dataIndex: 'kind',
      width: 110,
      render: (value: string) => <Tag color={runKindTone(value)}>{runKindLabel(value)}</Tag>,
    },
    {
      title: '卡密',
      key: 'cdk',
      width: 220,
      render: (_: unknown, row: Row) => cdkCell(row),
    },
    {
      title: '当前阶段 / 进度',
      key: 'stage',
      width: 240,
      render: (_: unknown, row: Row) => stage(row),
    },
    { title: '耗时', dataIndex: 'elapsed', width: 110, render: invitationDuration },
    { title: '开始时间', dataIndex: 'started_at', width: 170, render: invitationTime },
    {
      title: '错误 / 结果',
      dataIndex: 'error',
      ellipsis: true,
      width: 220,
      render: (value: string) => localizedText(value),
    },
  ];
  async function resolveHold() {
    if (!hold) return;
    try {
      const values = await holdForm.validateFields();
      const note = String(values.note || '').trim();
      if (
        values.confirmAllExecutorsStopped !== true ||
        note.length < 3 ||
        note.length > 250 ||
        /[\0\r\n]/.test(note)
      ) {
        throw new Error('必须确认所有执行实例已停止，并填写 3 至 250 字的单行复核说明。');
      }
      setSaving(true);
      await adminApi(`${HOLDS_PATH}/${encodeURIComponent(hold.jobId)}/resolve`, {
        method: 'POST',
        body: JSON.stringify({
          resolution: values.resolution,
          note,
          confirmAllExecutorsStopped: true,
          closeInterruptedTask: values.closeInterruptedTask !== false,
        }),
      });
      message.success('复核已完成；卡密与作业状态没有被改写成成功，结果请用卡密工具决定');
      setHold(null);
      holdForm.resetFields();
      await Promise.all([holds.reload(), runs.reload()]);
    } catch (error: any) {
      if (!error?.errorFields) message.error(invitationErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function releasePermit() {
    if (!release) return;
    try {
      const values = await form.validateFields();
      const body = permitReleaseBody(
        release.ownerToken,
        values.note,
        values.confirmAllExecutorsStopped,
      );
      setSaving(true);
      await adminApi(`${PERMITS_PATH}/${release.slotId}/release`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      message.success('执行占位已释放');
      setRelease(null);
      form.resetFields();
      await permits.reload();
    } catch (error: any) {
      if (!error?.errorFields) message.error(invitationErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  /** 停止单个作业：只结束本次执行并释放占位，不会把结果判成成功。 */
  async function stopJob(row: Row) {
    const jobId = String(row.job_id || '').trim();
    if (!jobId) return;
    setStopping(jobId);
    try {
      const response = await adminApi<{
        stopped?: boolean;
        released?: { email?: string };
        reviewRequired?: boolean;
        message?: string;
      }>(`/api/v1/admin/invitation/jobs/${encodeURIComponent(jobId)}/stop`, { method: 'POST' });
      const released = response.data?.released;
      const releasedText = released?.email ? `，已释放 ${released.email}` : '';
      // 后端对 queued 与 running 的处理不同：queued 确定没有外部动作，会把作业与卡密
      // 写成 failed；running 可能已经产生外部动作，复核会被保留。提示必须反映真实结果，
      // 不能统一说"卡密状态未被改写"。
      if (response.data?.reviewRequired) {
        message.warning(
          response.data?.message ||
            `已请求停止作业${releasedText}；该作业可能已产生外部动作，复核未解除，请先在「任务复核」中确认后再复用卡密`,
        );
      } else {
        message.success(`已停止作业${releasedText}；该作业尚未开始执行，卡密可按复核结果复用`);
      }
      await Promise.all([runs.reload(), holds.reload(), permits.reload()]);
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setStopping('');
    }
  }

  async function loadGaps() {
    setGapsLoading(true);
    setGapsError('');
    try {
      const response = await adminApi<QueueGaps>(QUEUE_GAPS_PATH);
      const next = response.data || {};
      setGaps(next);
      if (!(next.items || []).length) message.info('没有滞留作业');
    } catch (error) {
      setGaps(null);
      setGapsError(invitationErrorMessage(error));
    } finally {
      setGapsLoading(false);
    }
  }

  async function redispatch() {
    const jobId = redispatchJobId.trim();
    if (!jobId || !redispatchConfirmed) return;
    setRedispatching(true);
    try {
      await adminApi(QUEUE_REDISPATCH_PATH, {
        method: 'POST',
        body: JSON.stringify({ jobId, confirmNoExternalAction: true }),
      });
      message.success('已重新投递该作业');
      setRedispatchOpen(false);
      setRedispatchConfirmed(false);
      await Promise.all([runs.reload(), loadGaps()]);
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setRedispatching(false);
    }
  }

  const summary = monitor.data?.summary || {};
  const gapItems = gaps?.items || [];

  return (
    <AdminPage
      title="运行记录"
      description="查看排队、执行阶段和日志。运行列表每 5 秒刷新，执行状态和活动任务日志每 2 秒刷新。"
      extra={
        <Button
          onClick={() => {
            void runs.reload();
            void monitor.reload();
            void permits.reload();
            void holds.reload();
          }}
        >
          刷新
        </Button>
      }
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {[runs.error, monitor.error].filter(Boolean).map((error, index) => (
          <Alert
            key={index}
            type="warning"
            showIcon
            message={error}
            description={
              runs.lastLoadedAt
                ? `以下是 ${invitationTime(runs.lastLoadedAt / 1000)} 读取到的数据，可能已经过期。`
                : '尚未成功读取过数据。'
            }
          />
        ))}
        <InvitationRunPanel />
        <Card>
          <Space wrap style={{ marginBottom: 16 }}>
            <Input.Search
              aria-label="在本页搜索邮箱或任务"
              placeholder="在本页内搜索邮箱或任务"
              allowClear
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              style={{ width: 250 }}
            />
            <Select
              aria-label="状态筛选"
              value={status}
              onChange={(value) => {
                setStatus(value);
                setPage(1);
              }}
              style={{ width: 130 }}
              options={[
                { value: '', label: '全部状态' },
                ...['queued', 'running', 'done', 'failed'].map((value) => ({
                  value,
                  label: runStatusLabel(value),
                })),
              ]}
            />
            <Select
              aria-label="任务类型筛选"
              value={kind}
              onChange={(value) => {
                setKind(value);
                setPage(1);
              }}
              style={{ width: 140 }}
              options={runKindOptions()}
            />
            <Select
              aria-label="每页记录数量"
              value={limit}
              onChange={(value) => {
                setLimit(value);
                setPage(1);
              }}
              options={[20, 50, 100, 200].map((value) => ({ value, label: `每页 ${value} 条` }))}
            />
            <Space wrap>
              {/* 全局计数（服务端整表统计），不受下面列表条数限制。 */}
              {SUMMARY_ORDER.filter((key) => summary[key] !== undefined).map((key) => (
                <Tag
                  key={key}
                  color={
                    key === 'failed'
                      ? summary[key]
                        ? 'error'
                        : 'default'
                      : key === 'done'
                        ? 'success'
                        : key === 'proxy_available'
                          ? summary[key]
                            ? 'success'
                            : 'error'
                          : SUMMARY_TONE[key]
                  }
                >
                  {resolveLabel(SUMMARY_LABEL, key)} {String(summary[key] ?? '—')}
                </Tag>
              ))}
            </Space>
          </Space>
          <Tabs
            items={[
              {
                key: 'runs',
                label: '运行记录',
                children: (
                  <Table<Row>
                    rowKey={(row) => row.run_id || ''}
                    size="small"
                    loading={runs.loading && !runs.data}
                    dataSource={(runs.data?.items || []).filter(filter)}
                    scroll={{ x: 1900 }}
                    pagination={{
                      current: page,
                      pageSize: limit,
                      total: runs.data?.total ?? runs.data?.items?.length ?? 0,
                      showSizeChanger: false,
                      onChange: setPage,
                      showTotal: (total) =>
                        `共 ${total} 条（状态/类型筛选在服务端生效，文本框只过滤本页）`,
                    }}
                    columns={[
                      ...runsColumns,
                      {
                        title: '操作',
                        width: 210,
                        fixed: 'right',
                        render: (_, row) => (
                          <Space>
                            <Button type="link" size="small" onClick={() => setDetail(row)}>
                              日志详情
                            </Button>
                            <Button
                              type="link"
                              size="small"
                              onClick={() => row.run_id && watchRun(row.run_id)}
                            >
                              继续查看
                            </Button>
                            {isStoppable(row) &&
                              (row.job_id ? (
                                <Popconfirm
                                  title="停止该作业？"
                                  description="只结束本次执行并释放执行占位；不会把结果判成成功，卡密状态也不会被改写。"
                                  onConfirm={() => void stopJob(row)}
                                >
                                  <Button type="link" size="small" danger loading={stopping === row.job_id}>
                                    停止
                                  </Button>
                                </Popconfirm>
                              ) : (
                                <Tooltip title="该行没有可停止的作业">
                                  <Button type="link" size="small" danger disabled>
                                    停止
                                  </Button>
                                </Tooltip>
                              ))}
                          </Space>
                        ),
                      },
                    ]}
                  />
                ),
              },
              {
                key: 'jobs',
                label: '邀请作业 / 排队',
                children: (
                  <Space direction="vertical" size={12} style={{ width: '100%' }}>
                    <Space wrap>
                      <Button loading={gapsLoading} onClick={() => void loadGaps()}>
                        查询滞留队列
                      </Button>
                      <Button
                        type="primary"
                        danger
                        disabled={!gapItems.length}
                        onClick={() => {
                          setRedispatchJobId(String(gapItems[0]?.jobId || ''));
                          setRedispatchConfirmed(false);
                          setRedispatchOpen(true);
                        }}
                      >
                        重新投递
                      </Button>
                      {gaps && (
                        <Typography.Text type="secondary">
                          宽限期 {gaps.graceSeconds ?? '—'} 秒 ·{' '}
                          执行进程 {gaps.workerAlive ? '在线' : '不在线'}
                        </Typography.Text>
                      )}
                    </Space>
                    {gapsError && <Alert type="warning" showIcon message={gapsError} />}
                    {gaps && !gapItems.length && (
                      <Alert type="success" showIcon message="没有滞留作业" />
                    )}
                    {!!gapItems.length && (
                      <Table<QueueGap>
                        rowKey="jobId"
                        size="small"
                        dataSource={gapItems}
                        pagination={false}
                        scroll={{ x: 900 }}
                        columns={[
                          { title: '作业', dataIndex: 'jobId', width: 200, ellipsis: true },
                          { title: '邮箱', dataIndex: 'email', ellipsis: true },
                          {
                            title: '状态',
                            dataIndex: 'status',
                            width: 110,
                            render: (value: string) => <InvitationStatus status={value} />,
                          },
                          {
                            title: '阶段',
                            dataIndex: 'stage',
                            width: 160,
                            render: (value: string) => localizedText(value),
                          },
                          {
                            title: '排队时长',
                            dataIndex: 'ageSeconds',
                            width: 120,
                            render: (value: number) => invitationDuration(value),
                          },
                          {
                            title: '说明',
                            dataIndex: 'detail',
                            render: (value: string) => localizedText(value),
                          },
                          {
                            title: '操作',
                            width: 110,
                            render: (_, row) => (
                              <Button
                                type="link"
                                size="small"
                                onClick={() => {
                                  setRedispatchJobId(String(row.jobId));
                                  setRedispatchConfirmed(false);
                                  setRedispatchOpen(true);
                                }}
                              >
                                重新投递
                              </Button>
                            ),
                          },
                        ]}
                      />
                    )}
                    <Table<Row>
                      rowKey={(row) => row.job_id || ''}
                      size="small"
                      loading={monitor.loading && !monitor.data}
                      dataSource={(monitor.data?.items || []).filter(filter)}
                      scroll={{ x: 1400 }}
                      pagination={{ defaultPageSize: 20, showSizeChanger: true }}
                      columns={[
                        { title: '作业', dataIndex: 'job_id', width: 180, ellipsis: true },
                        { title: '邮箱', dataIndex: 'email', width: 200, ellipsis: true },
                        {
                          title: '状态',
                          dataIndex: 'status',
                          width: 100,
                          render: (value: string) => <InvitationStatus status={value} />,
                        },
                        {
                          title: '卡密',
                          key: 'cdk',
                          width: 220,
                          render: (_: unknown, row: Row) => cdkCell(row),
                        },
                        {
                          title: '当前阶段 / 进度',
                          key: 'stage',
                          width: 240,
                          render: (_: unknown, row: Row) => stage(row),
                        },
                        { title: '耗时', dataIndex: 'elapsed', width: 110, render: invitationDuration },
                        { title: '开始时间', dataIndex: 'started_at', width: 170, render: invitationTime },
                        {
                          title: '错误 / 结果',
                          dataIndex: 'error',
                          ellipsis: true,
                          width: 220,
                          render: (value: string) => localizedText(value),
                        },
                      ]}
                    />
                  </Space>
                ),
              },
              {
                key: 'events',
                label: '最近事件',
                children: (
                  <Table
                    rowKey={(row: any) => String(row.id)}
                    size="small"
                    dataSource={monitor.data?.events || []}
                    columns={eventColumns}
                    pagination={{ defaultPageSize: 20 }}
                    scroll={{ x: 700 }}
                  />
                ),
              },
              {
                key: 'holds',
                label: `结果待复核${holds.data?.items?.length ? `（${holds.data.items.length}）` : ''}`,
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Alert
                      type="warning"
                      showIcon
                      message="这些任务的结果不明确，卡密与邮箱在复核完成前保持封锁。"
                      description={
                        '解除封锁只清掉复核标记：服务端绝不会把结果改写成"成功"。' +
                        '完成复核后请用卡密工具（重置/作废）决定这张卡密的最终状态。' +
                        '若确认本次从未真正启动或外部执行已经结束，可勾选"同时把这条中断的任务落成失败"，' +
                        '让卡密回到可重试状态。'
                      }
                    />
                    {holds.error && <Alert type="warning" showIcon message={holds.error} />}
                    <Table<Hold>
                      rowKey="jobId"
                      loading={holds.loading && !holds.data}
                      size="small"
                      dataSource={holds.data?.items || []}
                      pagination={false}
                      scroll={{ x: 900 }}
                      locale={{ emptyText: '没有待复核的任务' }}
                      columns={[
                        { title: '任务', dataIndex: 'jobId', width: 160, ellipsis: true },
                        { title: '邮箱', dataIndex: 'email', ellipsis: true },
                        {
                          title: '原状态',
                          dataIndex: 'originalStatus',
                          width: 110,
                          render: (value: string) => <InvitationStatus status={value} />,
                        },
                        {
                          title: '中断原因',
                          dataIndex: 'reason',
                          width: 180,
                          render: (value: string) => holdReasonLabel(value),
                        },
                        { title: '创建时间', dataIndex: 'createdAt', width: 170, render: invitationTime },
                        {
                          title: '操作',
                          width: 110,
                          render: (_, row) => (
                            <Button
                              size="small"
                              onClick={() => {
                                holdForm.resetFields();
                                setHold(row);
                              }}
                            >
                              完成复核
                            </Button>
                          ),
                        },
                      ]}
                    />
                  </Space>
                ),
              },
              {
                key: 'permits',
                label: '执行占位复核',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Alert
                      type="info"
                      showIcon
                      message={
                        permits.data?.automaticReclaim === false
                          ? '执行占位不会自己释放：启动新执行进程时会清空上一进程的泄漏占位，领任务时也会回收非本实例的占位。人工释放前必须确认所有执行实例已经停止并填写复核说明。'
                          : '执行占位的自动回收策略以接口返回为准；人工释放前必须确认所有执行实例已经停止并填写复核说明。'
                      }
                    />
                    {permits.error && <Alert type="warning" showIcon message={permits.error} />}
                    <Table<Permit>
                      rowKey="slotId"
                      loading={permits.loading && !permits.data}
                      size="small"
                      dataSource={permits.data?.items || []}
                      pagination={false}
                      scroll={{ x: 800 }}
                      columns={[
                        { title: '槽位', dataIndex: 'slotId', width: 70 },
                        { title: '邮箱', dataIndex: 'email' },
                        { title: '实例', dataIndex: 'instanceId', ellipsis: true },
                        { title: '任务标识', dataIndex: 'taskId', ellipsis: true },
                        { title: '占用时间', dataIndex: 'acquiredAt', render: invitationTime },
                        {
                          title: '操作',
                          width: 110,
                          render: (_, row) => (
                            <Button
                              danger
                              size="small"
                              onClick={() => {
                                form.resetFields();
                                setRelease(row);
                              }}
                            >
                              人工释放
                            </Button>
                          ),
                        },
                      ]}
                    />
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      </Space>
      <Drawer
        title={`运行详情 · ${detail?.run_id || ''}`}
        open={!!detail}
        onClose={() => setDetail(null)}
        width={Math.min(1000, typeof window === 'undefined' ? 1000 : window.innerWidth)}
        extra={
          <Space>
            <Button onClick={() => void details.reload()}>刷新</Button>
            <Button
              disabled={!details.data?.text}
              onClick={() =>
                void copyInvitationText(details.data?.text || '')
                  .then(() => message.success('日志已复制'))
                  .catch((error) => message.error(invitationErrorMessage(error)))
              }
            >
              复制日志
            </Button>
          </Space>
        }
      >
        {details.error && (
          <Alert type="error" message={details.error} style={{ marginBottom: 16 }} />
        )}
        {details.data && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              size="small"
              column={2}
              bordered
              items={[
                { key: 'email', label: '邮箱', children: details.data.run.email },
                {
                  key: 'status',
                  label: '状态',
                  children: <InvitationStatus status={details.data.run.status} />,
                },
                {
                  key: 'cdk',
                  label: '卡密',
                  children:
                    details.data.run.cdk_code === '***'
                      ? '已脱敏（需要卡密权限）'
                      : details.data.run.cdk_code || '—',
                },
                {
                  key: 'cdk-masked',
                  label: '卡密脱敏值',
                  children: details.data.run.cdk_code_masked || '—',
                },
                {
                  key: 'cdk-batch',
                  label: '卡密批次',
                  children: details.data.run.cdk_batch || '—',
                },
                {
                  key: 'cdk-status',
                  label: '卡密状态',
                  children: details.data.run.cdk_status ? (
                    <InvitationStatus status={details.data.run.cdk_status} />
                  ) : (
                    '—'
                  ),
                },
                {
                  key: 'cdk-fail',
                  label: '卡密失败次数',
                  children: details.data.run.cdk_fail_count ?? '—',
                },
                {
                  key: 'cdk-until',
                  label: '卡密有效期',
                  children: invitationTime(details.data.run.cdk_valid_until ?? null),
                },
                {
                  key: 'cdk-sync',
                  label: '卡密状态同步',
                  children: <CdkSyncTag status={details.data.run.cdk_sync_status} />,
                  span: 2,
                },
                {
                  key: 'start',
                  label: '开始',
                  children: invitationTime(details.data.run.started_at),
                },
                {
                  key: 'finish',
                  label: '结束',
                  children: invitationTime(details.data.run.finished_at),
                },
                {
                  key: 'error',
                  label: '结果 / 错误',
                  children: localizedText(details.data.run.error),
                  span: 2,
                },
                {
                  key: 'log',
                  label: '日志',
                  children: `${Math.round((details.data.size || 0) / 1024)} KB${details.data.truncated ? ' · 已截断，仅显示末尾 1024 KB' : ''}`,
                  span: 2,
                },
              ]}
            />
            <CdkSyncDetails
              status={details.data.run.cdk_sync_status}
              error={details.data.run.cdk_sync_error}
            />
            <Table
              rowKey={(row: any) => String(row.id)}
              size="small"
              dataSource={details.data.events || []}
              columns={eventColumns}
              pagination={{ defaultPageSize: 10 }}
              title={() => '该邮箱的卡密事件'}
              scroll={{ x: 600 }}
            />
            <Typography.Title level={5} style={{ margin: 0 }}>
              执行日志
            </Typography.Title>
            <pre
              aria-label="完整执行日志"
              style={{
                padding: 16,
                borderRadius: 8,
                maxHeight: '55vh',
                overflow: 'auto',
                background: '#f5f7f9',
                whiteSpace: 'pre-wrap',
                overflowWrap: 'anywhere',
                margin: 0,
              }}
            >
              {details.data.text ||
                (details.data.exists === false ? '日志文件尚未生成。' : '日志为空。')}
            </pre>
          </Space>
        )}
      </Drawer>
      <Modal
        title="重新投递滞留作业"
        open={redispatchOpen}
        onCancel={() => {
          setRedispatchOpen(false);
          setRedispatchConfirmed(false);
        }}
        onOk={() => void redispatch()}
        okText="重新投递"
        confirmLoading={redispatching}
        okButtonProps={{ danger: true, disabled: !redispatchJobId.trim() || !redispatchConfirmed }}
      >
        <Alert
          type="warning"
          showIcon
          message="只对确认从未真正启动过的作业使用。"
          description="若该作业可能已经执行过外部动作（发出邀请、消耗接码），重新投递会造成重复操作。"
          style={{ marginBottom: 16 }}
        />
        <Form layout="vertical">
          <Form.Item label="作业标识">
            <Input
              value={redispatchJobId}
              onChange={(event) => setRedispatchJobId(event.target.value)}
              placeholder="从滞留队列里选一个，或直接粘贴作业标识"
            />
          </Form.Item>
          <Checkbox
            checked={redispatchConfirmed}
            onChange={(event) => setRedispatchConfirmed(event.target.checked)}
          >
            确认该作业从未真正启动
          </Checkbox>
        </Form>
      </Modal>
      <Modal
        title={`复核任务 ${hold?.jobId || ''}`}
        open={!!hold}
        onCancel={() => {
          setHold(null);
          holdForm.resetFields();
        }}
        onOk={() => void resolveHold()}
        confirmLoading={saving}
        okText="提交复核"
      >
        <Alert
          type="warning"
          showIcon
          message="仅在确认所有执行实例已停止后操作。这一步不会把结果判为成功。"
          style={{ marginBottom: 16 }}
        />
        <Form form={holdForm} layout="vertical" initialValues={{ resolution: 'interrupted_confirmed', closeInterruptedTask: true }}>
          <Form.Item name="resolution" label="复核结论" rules={[{ required: true }]}>
            <Select options={HOLD_RESOLUTIONS} />
          </Form.Item>
          <Form.Item
            name="note"
            label="复核说明"
            rules={[
              { required: true, message: '请填写 3 至 250 字的单行说明。' },
              { max: 250, message: '说明不能超过 250 字。' },
            ]}
          >
            <Input placeholder="例如：已核对执行器日志与外部状态，本次未产生邀请动作" />
          </Form.Item>
          <Form.Item
            name="confirmAllExecutorsStopped"
            valuePropName="checked"
            rules={[
              {
                validator: (_, value) =>
                  value === true
                    ? Promise.resolve()
                    : Promise.reject(new Error('请先确认所有执行实例已停止。')),
              },
            ]}
          >
            <Checkbox>我已确认所有执行实例已停止</Checkbox>
          </Form.Item>
          <Form.Item name="closeInterruptedTask" valuePropName="checked">
            <Checkbox>同时把这条中断的任务落成失败（卡密回到可重试状态）</Checkbox>
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={`人工释放执行槽位 ${release?.slotId || ''}`}
        open={!!release}
        onCancel={() => {
          setRelease(null);
          form.resetFields();
        }}
        onOk={() => void releasePermit()}
        confirmLoading={saving}
        okText="确认释放"
        okButtonProps={{ danger: true }}
      >
        <Alert
          type="warning"
          showIcon
          message="仅在所有执行实例停止后操作。仍有实例运行或占位已变更时，服务会拒绝释放。"
          style={{ marginBottom: 16 }}
        />
        <Form form={form} layout="vertical">
          <Form.Item
            name="confirmAllExecutorsStopped"
            valuePropName="checked"
            rules={[
              {
                validator: (_, value) =>
                  value === true
                    ? Promise.resolve()
                    : Promise.reject(new Error('请先确认所有执行实例已停止。')),
              },
            ]}
          >
            <Checkbox>我已确认所有执行实例均已停止</Checkbox>
          </Form.Item>
          <Form.Item
            name="note"
            label="复核说明"
            rules={[
              {
                required: true,
                whitespace: true,
                min: 3,
                max: 250,
                message: '请填写 3 至 250 字的单行复核说明。',
              },
              { pattern: /^[^\r\n\0]+$/, message: '复核说明必须为单行。' },
            ]}
          >
            <Input maxLength={250} />
          </Form.Item>
        </Form>
      </Modal>
    </AdminPage>
  );
}

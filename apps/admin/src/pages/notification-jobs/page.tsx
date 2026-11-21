import { useEffect, useRef, useState } from 'react';
import { Button, Descriptions, Modal, Popconfirm, Spin, Typography, message } from 'antd';
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components';
import { AdminPage } from '../../components/admin-page';
import { AdminErrorAlert, AdminStatusTag, toneFromStatus } from '../../components/admin-feedback';
import { adminApi } from '../../lib/admin-api';
import { formatDateTime } from '../../lib/format-datetime';
import { resolveLabel } from '../../lib/zh-label';
import {
  NOTIFICATION_KIND_LABEL,
  NOTIFICATION_STATUS_LABEL,
  moduleErrorLabel,
  notificationChannelLabel,
} from '../../lib/zh-maps';

type Job = {
  id: string;
  channel: string;
  kind: string;
  status: string;
  attempts: number;
  lastErrorCode?: string;
  createdAt: string;
};
type JobDetail = Job & {
  eventKey?: string;
  referenceId?: string | null;
  remoteId?: string | null;
  updatedAt?: string;
  expiresAt?: string | null;
  lockedUntil?: string | null;
  to?: string | null;
  subject?: string | null;
  payload?: Record<string, unknown> | null;
};

/** 中文兜底表：先合并通用映射，再补本页特有的枚举，最后走「未知（原始值）」。 */
const STATUS_LABEL: Record<string, string> = {
  ...NOTIFICATION_STATUS_LABEL,
  UNCERTAIN: '结果待核查',
  SKIPPED: '未发送',
};
const KIND_LABEL: Record<string, string> = {
  ...NOTIFICATION_KIND_LABEL,
  SMS_OPS: '接码告警',
};

function statusLabel(status?: string | null) {
  return resolveLabel(STATUS_LABEL, status);
}
function kindLabel(kind?: string | null) {
  return resolveLabel(KIND_LABEL, kind);
}

/** 尚未发出的通知才允许取消（服务器也会再校验一次）。 */
function canCancel(status: string) {
  return status === 'PENDING';
}

function JobDetailView({ id }: { id: string }) {
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    adminApi<JobDetail>(`/api/v1/admin/notifications/outbox/${id}`)
      .then((response) => {
        if (alive) setDetail(response.data);
      })
      .catch((err) => {
        if (alive) setError(err instanceof Error ? err.message : '加载失败');
      });
    return () => {
      alive = false;
    };
  }, [id]);

  if (error) return <AdminErrorAlert message={error} />;
  if (!detail) return <Spin />;
  const body =
    detail.payload && typeof detail.payload.body === 'string' ? detail.payload.body : null;
  const undecryptable = Boolean(detail.payload && detail.payload._undecryptable);
  const recipient =
    detail.to ||
    (detail.payload && typeof detail.payload.chat === 'string' ? detail.payload.chat : null) ||
    null;
  return (
    <Descriptions column={1} size="small" bordered>
      <Descriptions.Item label="通知类型">{kindLabel(detail.kind)}</Descriptions.Item>
      <Descriptions.Item label="渠道">
        {notificationChannelLabel(detail.channel)}
      </Descriptions.Item>
      <Descriptions.Item label="状态">
        <AdminStatusTag tone={toneFromStatus(detail.status)}>
          {statusLabel(detail.status)}
        </AdminStatusTag>
      </Descriptions.Item>
      <Descriptions.Item label="收件人">{recipient || '—'}</Descriptions.Item>
      <Descriptions.Item label="主题">{detail.subject || '—'}</Descriptions.Item>
      <Descriptions.Item label="尝试次数">{detail.attempts}</Descriptions.Item>
      <Descriptions.Item label="失败原因">
        {detail.lastErrorCode ? (
          <>
            <div>{moduleErrorLabel(detail.lastErrorCode)}</div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              技术详情：{detail.lastErrorCode}
            </Typography.Text>
          </>
        ) : (
          '—'
        )}
      </Descriptions.Item>
      <Descriptions.Item label="通道回执号">{detail.remoteId || '—'}</Descriptions.Item>
      <Descriptions.Item label="创建时间">{formatDateTime(detail.createdAt)}</Descriptions.Item>
      <Descriptions.Item label="最后更新">{formatDateTime(detail.updatedAt)}</Descriptions.Item>
      <Descriptions.Item label="正文">
        {undecryptable ? (
          <Typography.Text type="danger">负载无法解密（加密密钥可能已变更）</Typography.Text>
        ) : body ? (
          <Typography.Paragraph
            style={{ whiteSpace: 'pre-wrap', marginBottom: 0, maxHeight: 260, overflow: 'auto' }}
          >
            {body}
          </Typography.Paragraph>
        ) : (
          '—'
        )}
      </Descriptions.Item>
    </Descriptions>
  );
}

export default function NotificationJobsPage() {
  const action = useRef<ActionType>();
  const [busyId, setBusyId] = useState<string | null>(null);
  /**
   * Payload is fetched on demand: the list query deliberately does not decrypt anything, so one
   * unreadable row cannot break the whole page.
   */
  function detail(job: Job) {
    Modal.info({
      title: '发送详情',
      width: 720,
      okText: '关闭',
      content: <JobDetailView id={job.id} />,
      maskClosable: true,
    });
  }
  function retry(job: Job) {
    Modal.confirm({
      title: '重新发送此通知？',
      content:
        job.status === 'UNCERTAIN'
          ? '上次发送结果不确定。请先核查邮件或 Telegram，重发可能造成重复通知。'
          : '请先确认通道配置和网络已恢复。',
      okText: '确认重发',
      cancelText: '取消',
      onOk: async () => {
        try {
          await adminApi(`/api/v1/admin/notifications/outbox/${job.id}/retry`, {
            method: 'POST',
            body: JSON.stringify({ acknowledgeDuplicateRisk: job.status === 'UNCERTAIN' }),
          });
          message.success('已重新加入发送队列');
          action.current?.reload();
        } catch (error) {
          message.error(error instanceof Error ? error.message : '重发失败');
          throw error;
        }
      },
    });
  }
  async function cancel(job: Job) {
    setBusyId(job.id);
    try {
      await adminApi(`/api/v1/admin/notifications/outbox/${job.id}/cancel`, { method: 'POST' });
      message.success('已取消该通知');
      action.current?.reload();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '取消失败');
    } finally {
      setBusyId(null);
    }
  }
  const columns: ProColumns<Job>[] = [
    {
      title: '通知类型',
      dataIndex: 'kind',
      search: false,
      render: (_, row) => kindLabel(row.kind),
    },
    {
      title: '渠道',
      dataIndex: 'channel',
      search: false,
      render: (_, row) => notificationChannelLabel(row.channel),
    },
    {
      title: '状态',
      dataIndex: 'status',
      valueType: 'select',
      valueEnum: Object.fromEntries(
        Object.entries(STATUS_LABEL).map(([key, text]) => [key, { text }]),
      ),
      render: (_, row) => (
        <AdminStatusTag tone={toneFromStatus(row.status)}>{statusLabel(row.status)}</AdminStatusTag>
      ),
    },
    { title: '尝试次数', dataIndex: 'attempts', search: false, width: 100 },
    {
      title: '失败原因',
      dataIndex: 'lastErrorCode',
      search: false,
      ellipsis: true,
      render: (_, row) => (row.lastErrorCode ? moduleErrorLabel(row.lastErrorCode) : '—'),
    },
    {
      title: '创建时间',
      dataIndex: 'createdAt',
      search: false,
      render: (_, row) => formatDateTime(row.createdAt),
    },
    {
      title: '操作',
      valueType: 'option',
      render: (_, row) => [
        <Button key="detail" type="link" onClick={() => detail(row)}>
          详情
        </Button>,
        ['FAILED', 'UNCERTAIN', 'SKIPPED'].includes(row.status) ? (
          <Button key="retry" type="link" onClick={() => retry(row)}>
            重发
          </Button>
        ) : null,
        canCancel(row.status) ? (
          <Popconfirm
            key="cancel"
            title="取消该通知？"
            description="取消后该通知不再发送。"
            onConfirm={() => cancel(row)}
          >
            <Button type="link" danger disabled={busyId === row.id}>
              取消
            </Button>
          </Popconfirm>
        ) : null,
      ],
    },
  ];
  return (
    <AdminPage
      title="通知发送记录"
      description="查看发送进度；结果待核查的通知不会自动重发。通道接收不代表收件人已阅读。"
    >
      <ProTable<Job>
        rowKey="id"
        actionRef={action}
        columns={columns}
        scroll={{ x: 900 }}
        pagination={{ defaultPageSize: 20 }}
        search={{ labelWidth: 'auto' }}
        request={async (params) => {
          const query = new URLSearchParams({
            page: String(params.current || 1),
            pageSize: String(params.pageSize || 20),
          });
          if (params.status) query.set('status', params.status);
          const response = await adminApi<Job[]>(`/api/v1/admin/notifications/outbox?${query}`);
          return { data: response.data, total: response.meta?.total || 0, success: true };
        }}
      />
    </AdminPage>
  );
}

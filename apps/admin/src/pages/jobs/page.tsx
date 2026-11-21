import { useRef, useState } from 'react';
import { Button, Popconfirm, Typography, message } from 'antd';
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components';
import { AdminPage } from '../../components/admin-page';
import { AdminStatusTag, toneFromStatus } from '../../components/admin-feedback';
import { AdminDetailDrawer, CopyableValue } from '../../components/admin-detail-drawer';
import { adminApi } from '../../lib/admin-api';
import { formatDateTime } from '../../lib/format-datetime';
import { JOB_STATUS_LABEL, jobKindLabel, jobStatusLabel, moduleErrorLabel } from '../../lib/zh-maps';

type Job = {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  maxAttempts?: number | null;
  lastErrorCode?: string | null;
  availableAt?: string | null;
  updatedAt: string;
  payload?: unknown;
};

/** 任务专属的补充说明（优先于通用错误码表）。 */
const ERROR_HINT: Record<string, string> = {
  LEASE_EXPIRED: '执行租约过期，可安全重试',
};

const STATUS_VALUE_ENUM = Object.fromEntries(
  Object.entries(JOB_STATUS_LABEL).map(([value, text]) => [value, { text }]),
);

function retryDescription() {
  return '请先核对失败原因与关联资源状态。';
}

export default function JobsPage() {
  const action = useRef<ActionType>();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Job | null>(null);

  async function cancel(row: Job) {
    setBusyId(row.id);
    try {
      await adminApi(`/api/v1/admin/jobs/${encodeURIComponent(row.id)}/cancel`, { method: 'POST' });
      message.success('已取消该任务');
      action.current?.reload();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '取消失败');
    } finally {
      setBusyId(null);
    }
  }

  const columns: ProColumns<Job>[] = [
    {
      title: '任务',
      dataIndex: 'kind',
      search: false,
      render: (_, row) => jobKindLabel(row.kind),
    },
    {
      title: '状态',
      dataIndex: 'status',
      valueType: 'select',
      valueEnum: STATUS_VALUE_ENUM,
      render: (_, row) => (
        <AdminStatusTag tone={toneFromStatus(row.status)}>
          {jobStatusLabel(row.status)}
        </AdminStatusTag>
      ),
    },
    {
      title: '尝试',
      dataIndex: 'attempts',
      search: false,
      width: 90,
      render: (_, row) =>
        row.maxAttempts != null ? `${row.attempts}/${row.maxAttempts}` : String(row.attempts ?? 0),
    },
    {
      title: '失败原因',
      dataIndex: 'lastErrorCode',
      search: false,
      ellipsis: true,
      render: (_, row) =>
        row.lastErrorCode ? moduleErrorLabel(row.lastErrorCode, ERROR_HINT) : '—',
    },
    {
      title: '下次执行',
      dataIndex: 'availableAt',
      search: false,
      render: (_, row) => formatDateTime(row.availableAt),
    },
    {
      title: '更新时间',
      dataIndex: 'updatedAt',
      search: false,
      render: (_, row) => formatDateTime(row.updatedAt),
    },
    {
      title: '操作',
      valueType: 'option',
      render: (_, row) => [
        <Button key="detail" type="link" size="small" onClick={() => setDetail(row)}>
          详情
        </Button>,
        row.status === 'PENDING' || row.status === 'RUNNING' ? (
          <Popconfirm
            key="cancel"
            title="取消该任务？"
            description="取消后任务不再执行；已发生的外部调用不会被回滚。"
            onConfirm={() => cancel(row)}
          >
            <Button type="link" size="small" danger disabled={busyId === row.id}>
              取消
            </Button>
          </Popconfirm>
        ) : null,
        row.status === 'FAILED' ? (
          <Popconfirm
            key="retry"
            title={`重新执行「${jobKindLabel(row.kind)}」？`}
            description={retryDescription()}
            onConfirm={async () => {
              try {
                await adminApi(`/api/v1/admin/jobs/${row.id}/retry`, { method: 'POST' });
                message.success('已重新排队');
                action.current?.reload();
              } catch (error) {
                message.error(error instanceof Error ? error.message : '重试失败');
                throw error;
              }
            }}
          >
            <Button type="link" size="small">
              重新执行
            </Button>
          </Popconfirm>
        ) : null,
      ],
    },
  ];

  return (
    <AdminPage
      title="后台任务"
      description="接码补偿任务。重试前请阅读操作说明，确认不会重复触发外部副作用。"
    >
      <ProTable<Job>
        rowKey="id"
        actionRef={action}
        columns={columns}
        scroll={{ x: 1080 }}
        form={{
          syncToUrl: true,
          initialValues: {
            status:
              typeof window !== 'undefined'
                ? new URLSearchParams(window.location.search).get('status') || undefined
                : undefined,
          },
        }}
        pagination={{ defaultPageSize: 20 }}
        request={async (params) => {
          const query = new URLSearchParams({
            page: String(params.current || 1),
            pageSize: String(params.pageSize || 20),
          });
          if (params.status) query.set('status', String(params.status));
          const result = await adminApi<Job[]>(`/api/v1/admin/jobs?${query}`);
          return { data: result.data, total: result.meta?.total || 0, success: true };
        }}
      />

      <AdminDetailDrawer
        title="后台任务详情"
        subtitle={detail ? `任务 ID：${detail.id}` : undefined}
        open={!!detail}
        onClose={() => setDetail(null)}
        groups={
          detail
            ? [
                {
                  key: 'job',
                  title: '任务',
                  items: [
                    { key: 'id', label: '任务 ID', children: <CopyableValue value={detail.id} /> },
                    { key: 'kind', label: '任务类型', children: jobKindLabel(detail.kind) },
                    {
                      key: 'status',
                      label: '状态',
                      children: (
                        <AdminStatusTag tone={toneFromStatus(detail.status)}>
                          {jobStatusLabel(detail.status)}
                        </AdminStatusTag>
                      ),
                    },
                    {
                      key: 'attempts',
                      label: '尝试次数',
                      children:
                        detail.maxAttempts != null
                          ? `${detail.attempts}/${detail.maxAttempts}`
                          : String(detail.attempts ?? 0),
                    },
                    {
                      key: 'error',
                      label: '失败原因',
                      children: detail.lastErrorCode ? (
                        <>
                          <div>{moduleErrorLabel(detail.lastErrorCode, ERROR_HINT)}</div>
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            技术详情：{detail.lastErrorCode}
                          </Typography.Text>
                        </>
                      ) : (
                        '—'
                      ),
                    },
                    {
                      key: 'availableAt',
                      label: '下次执行',
                      children: formatDateTime(detail.availableAt),
                    },
                    {
                      key: 'updatedAt',
                      label: '更新时间',
                      children: formatDateTime(detail.updatedAt),
                    },
                  ],
                },
                {
                  key: 'payload',
                  title: '负载',
                  items: [
                    {
                      key: 'payload',
                      label: '任务负载',
                      children: detail.payload ? (
                        <Typography.Text code style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
                          {JSON.stringify(detail.payload, null, 2)}
                        </Typography.Text>
                      ) : (
                        '—'
                      ),
                    },
                  ],
                },
              ]
            : []
        }
      />
    </AdminPage>
  );
}

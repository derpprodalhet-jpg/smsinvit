import { ReloadOutlined } from '@ant-design/icons';
import type { ActionType, ProColumns } from '@ant-design/pro-components';
import { ProTable } from '@ant-design/pro-components';
import { Button, Space, Tag, Typography, message } from 'antd';
import { useRef, useState } from 'react';
import { AdminPage } from '../../components/admin-page';
import { AdminDetailDrawer, CopyableValue, type DetailGroup } from '../../components/admin-detail-drawer';
import { adminApi } from '../../lib/admin-api';
import { formatDateTime } from '../../lib/format-datetime';
import { auditActionLabel, auditResourceLabel, legacyFieldLabel } from '../../lib/zh-maps';
import { PRO_TABLE_OPTIONS, PRO_TABLE_SEARCH, buildListQuery } from '../../lib/pro-table-query';

type AuditRow = {
  id: string;
  action: string;
  resourceType: string;
  resourceId: string | null;
  actorUserId: string | null;
  actor?: { username?: string; displayName?: string | null; email?: string } | null;
  beforeJson: unknown;
  afterJson: unknown;
  ipAddress?: string | null;
  requestId: string;
  createdAt: string;
};

/** 把 before/after 渲染成「中文字段名 + 等宽值」，非对象原样 JSON。 */
function snapshots(value: unknown) {
  if (value === null || value === undefined) return <>—</>;
  if (typeof value !== 'object' || Array.isArray(value)) {
    return (
      <Typography.Text code style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
        {JSON.stringify(value)}
      </Typography.Text>
    );
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return <>—</>;
  return (
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      {entries.map(([key, item]) => (
        <div key={key}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {legacyFieldLabel(key)}
          </Typography.Text>
          <div>
            <Typography.Text code style={{ fontSize: 12, overflowWrap: 'anywhere' }}>
              {typeof item === 'string' ? item : JSON.stringify(item)}
            </Typography.Text>
          </div>
        </div>
      ))}
    </Space>
  );
}

export default function AdminAuditPage() {
  const actionRef = useRef<ActionType>();
  const [detail, setDetail] = useState<AuditRow | null>(null);

  const columns: ProColumns<AuditRow>[] = [
    {
      title: '时间',
      dataIndex: 'createdAt',
      search: false,
      width: 180,
      render: (_, row) => (
        <Typography.Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
          {formatDateTime(row.createdAt)}
        </Typography.Text>
      ),
    },
    {
      title: '动作',
      dataIndex: 'action',
      fieldProps: { placeholder: '如 生成卡密 / 修改配置' },
      render: (_, row) => <Tag>{auditActionLabel(row.action)}</Tag>,
    },
    {
      title: '资源类型',
      dataIndex: 'resourceType',
      fieldProps: { placeholder: '卡密 / 接码 / 邀请' },
      render: (_, row) => (
        <Typography.Text style={{ fontSize: 12 }}>
          {auditResourceLabel(row.resourceType)}
        </Typography.Text>
      ),
    },
    {
      title: '操作者',
      search: false,
      render: (_, row) =>
        row.actor?.displayName || row.actor?.username || row.actor?.email || '系统',
    },
    {
      title: '操作',
      valueType: 'option',
      search: false,
      render: (_, row) => (
        <Button type="link" size="small" onClick={() => setDetail(row)}>
          详情
        </Button>
      ),
    },
  ];

  const detailGroups: DetailGroup[] = detail
    ? [
        {
          key: 'record',
          title: '记录',
          items: [
            { key: 'id', label: '记录 ID', children: <CopyableValue value={detail.id} /> },
            { key: 'createdAt', label: '时间', children: formatDateTime(detail.createdAt) },
            { key: 'action', label: '动作', children: auditActionLabel(detail.action) },
            {
              key: 'resource',
              label: '资源',
              children: (
                <Space size={4} wrap>
                  <span>{auditResourceLabel(detail.resourceType)}</span>
                  {detail.resourceId ? <CopyableValue value={detail.resourceId} /> : null}
                </Space>
              ),
            },
            {
              key: 'actor',
              label: '操作者',
              children:
                detail.actor?.displayName ||
                detail.actor?.username ||
                detail.actor?.email ||
                '系统',
            },
            { key: 'ip', label: '来源 IP', children: detail.ipAddress || '—' },
            {
              key: 'requestId',
              label: '请求 ID',
              children: <CopyableValue value={detail.requestId} />,
            },
          ],
        },
        {
          key: 'before',
          title: '变更前',
          items: [{ key: 'beforeJson', label: '变更前快照', children: snapshots(detail.beforeJson) }],
        },
        {
          key: 'after',
          title: '变更后',
          items: [{ key: 'afterJson', label: '变更后快照', children: snapshots(detail.afterJson) }],
        },
      ]
    : [];

  return (
    <AdminPage
      title="操作审计"
      description="只读查看后台敏感操作日志；变更前后字段见每行「详情」。"
      extra={
        <Button icon={<ReloadOutlined />} onClick={() => actionRef.current?.reload()}>
          刷新
        </Button>
      }
    >
      <ProTable<AuditRow>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        cardBordered
        search={PRO_TABLE_SEARCH}
        options={PRO_TABLE_OPTIONS}
        form={{ syncToUrl: true }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true }}
        request={async (params) => {
          const qs = buildListQuery(params);
          try {
            const res = await adminApi<AuditRow[]>(`/api/v1/admin/audit-logs?${qs}`);
            return {
              data: res.data || [],
              success: true,
              total: res.meta?.total ?? 0,
            };
          } catch (err) {
            message.error(err instanceof Error ? err.message : '加载失败');
            return { data: [], success: false, total: 0 };
          }
        }}
      />

      <AdminDetailDrawer
        title="审计详情"
        open={!!detail}
        onClose={() => setDetail(null)}
        groups={detailGroups}
      />
    </AdminPage>
  );
}

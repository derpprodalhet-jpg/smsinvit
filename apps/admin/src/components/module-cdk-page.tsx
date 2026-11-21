import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Empty, Input, Select, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { AdminPage } from './admin-page';
import { AdminDetailDrawer, CopyableValue, type DetailGroup } from './admin-detail-drawer';
import { AdminStatusTag } from './admin-feedback';
import { adminApi } from '../lib/admin-api';
import { formatDateTime } from '../lib/format-datetime';
import { unknownLabel } from '../lib/zh-label';
import { moduleErrorLabel } from '../lib/zh-maps';

/**
 * 「XX 卡密管理」共用的卡密台账（现由接码卡密管理使用）。
 *
 * 接码 / 邀请 的卡密明细字段名一致（`inventoryItemId` /
 * `codeFingerprint` / `codeMasked` / `code`），差异只在各自的业务列与筛选，
 * 所以列表、分页、权限提示、详情抽屉都在这里，页面只提供列定义与详情分组。
 *
 * 明文只在具备 `inventory.export` 时由后端返回（`code`）；没有权限时后端连键都不给，
 * 这里显示脱敏值并说明原因，绝不用前端隐藏冒充权限控制。
 */
export type CdkRow = {
  id: string;
  status: string;
  code?: string | null;
  codeMasked?: string | null;
  codeFingerprint?: string | null;
  inventoryItemId?: string | null;
  batchId?: string | null;
  batchNo?: string | null;
  [key: string]: unknown;
};

export type ModuleCdkConfig<Row extends CdkRow> = {
  /** 页面标题，如「接码卡密管理」。 */
  title: string;
  description: string;
  /** 列表接口路径（不含分页参数）。 */
  endpoint: string;
  /** 从接口响应里取出本页数据与总数（三个模块的响应外壳不同）。 */
  extract: (data: unknown, meta: unknown) => { items: Row[]; total: number };
  /** 状态筛选：参数名与选项；留空表示不需要筛选。 */
  statusFilter?: { param: string; options: { value: string; label: string }[] };
  /** 明文卡密搜索参数名（完整卡密 → 后端按指纹过滤）。 */
  cdkParam?: string;
  /** 业务列（卡密列由本组件统一渲染并放在第一位）。 */
  columns: ColumnsType<Row>;
  /** 详情抽屉分组（卡密/凭据组由本组件统一补充）。 */
  detailGroups?: (row: Row) => DetailGroup[];
  /** 状态文案与配色；未命中会显示「未知（原始值）」。 */
  statusLabel: Record<string, string>;
  statusTone: (status: string) => 'default' | 'success' | 'warning' | 'error' | 'processing';
};

export function ModuleCdkPage<Row extends CdkRow>({ config }: { config: ModuleCdkConfig<Row> }) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [status, setStatus] = useState('');
  const [cdkInput, setCdkInput] = useState('');
  const [cdk, setCdk] = useState('');
  const [items, setItems] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<Row | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const query = new URLSearchParams({ page: String(page), pageSize: String(pageSize) });
      if (status && config.statusFilter) query.set(config.statusFilter.param, status);
      if (cdk && config.cdkParam) query.set(config.cdkParam, cdk);
      const response = await adminApi<unknown>(`${config.endpoint}?${query}`);
      const picked = config.extract(response.data, response.meta);
      setItems(picked.items || []);
      setTotal(picked.total || 0);
    } catch (failure) {
      setItems([]);
      setTotal(0);
      setError(failure instanceof Error ? failure.message : '读取卡密列表失败');
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, status, cdk, config]);

  useEffect(() => {
    void load();
  }, [load]);

  const cardColumn = useMemo(
    () => ({
      title: '卡密',
      key: 'code',
      width: 240,
      render: (_: unknown, row: Row) => {
        if (row.code)
          return (
            <Space direction="vertical" size={0} style={{ maxWidth: '100%' }}>
              <Typography.Text
                code
                copyable={{ text: row.code }}
                style={{ overflowWrap: 'anywhere' }}
              >
                {row.code}
              </Typography.Text>
              {row.batchNo ? (
                <Typography.Text type="secondary">{String(row.batchNo)}</Typography.Text>
              ) : null}
            </Space>
          );
        return (
          <Space direction="vertical" size={0}>
            <Tooltip title="完整卡密需要 inventory.export 权限；后端在没有权限时不会返回明文字段。">
              <Typography.Text type="secondary">
                {row.codeMasked ? String(row.codeMasked) : '已脱敏（需要卡密权限）'}
              </Typography.Text>
            </Tooltip>
            {row.codeFingerprint ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                指纹 {String(row.codeFingerprint).slice(0, 12)}…
              </Typography.Text>
            ) : null}
          </Space>
        );
      },
    }),
    [],
  );

  const columns: ColumnsType<Row> = [
    cardColumn,
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (value: string) => (
        <AdminStatusTag tone={config.statusTone(value)}>
          {config.statusLabel[value] || unknownLabel(value)}
        </AdminStatusTag>
      ),
    },
    ...config.columns,
    {
      title: '操作',
      fixed: 'right',
      width: 88,
      render: (_: unknown, row: Row) => (
        <Button type="link" size="small" onClick={() => setDetail(row)}>
          详情
        </Button>
      ),
    },
  ];

  const groups: DetailGroup[] = detail
    ? [
        {
          key: 'card',
          title: '卡密',
          items: [
            {
              key: 'code',
              label: '卡密',
              children: detail.code ? (
                <CopyableValue value={detail.code} />
              ) : (
                <Typography.Text type="secondary">
                  {detail.codeMasked ? String(detail.codeMasked) : '已脱敏（需要卡密权限）'}
                </Typography.Text>
              ),
            },
            { key: 'fingerprint', label: '指纹', children: <CopyableValue value={detail.codeFingerprint} /> },
            { key: 'inventory', label: '库存记录 ID', children: <CopyableValue value={detail.inventoryItemId} /> },
            { key: 'batch', label: '生成批次', children: <CopyableValue value={detail.batchNo || detail.batchId} /> },
            {
              key: 'status',
              label: '状态',
              children: (
                <AdminStatusTag tone={config.statusTone(String(detail.status))}>
                  {config.statusLabel[String(detail.status)] || unknownLabel(String(detail.status))}
                </AdminStatusTag>
              ),
            },
          ],
        },
        ...(config.detailGroups ? config.detailGroups(detail) : []),
      ]
    : [];

  return (
    <AdminPage title={config.title} description={config.description}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {error && (
          <Alert
            type="error"
            showIcon
            message={error}
            action={<Button onClick={() => void load()}>重试</Button>}
          />
        )}
        <Card>
          <Space wrap style={{ marginBottom: 16 }}>
            {config.statusFilter && (
              <Select
                aria-label="卡密状态"
                value={status || undefined}
                allowClear
                placeholder="全部状态"
                style={{ width: 160 }}
                options={config.statusFilter.options}
                onChange={(value) => {
                  setStatus(value || '');
                  setPage(1);
                }}
              />
            )}
            {config.cdkParam && (
              <Input.Search
                aria-label="按完整卡密查询"
                allowClear
                placeholder="完整卡密（按指纹匹配）"
                value={cdkInput}
                onChange={(event) => setCdkInput(event.target.value)}
                onSearch={(value) => {
                  setCdk((value || '').trim());
                  setPage(1);
                }}
                style={{ width: 300 }}
                enterButton="查询"
              />
            )}
            <Button loading={loading} onClick={() => void load()}>
              刷新
            </Button>
          </Space>
          <Table<Row>
            rowKey="id"
            size="small"
            loading={loading}
            dataSource={items}
            columns={columns}
            scroll={{ x: 1100 }}
            locale={{ emptyText: <Empty description="暂无卡密" /> }}
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: true,
              showTotal: (value) => `共 ${value} 张卡密`,
              onChange: (next, size) => {
                setPage(next);
                setPageSize(size);
              },
            }}
          />
        </Card>
      </Space>
      <AdminDetailDrawer
        title={`卡密详情 · ${detail?.code || detail?.codeMasked || ''}`}
        open={!!detail}
        onClose={() => setDetail(null)}
        groups={groups}
      />
    </AdminPage>
  );
}

/** 时间列的统一渲染（三页共用，避免各写一份 dateTime）。 */
export function cdkTime(value: unknown) {
  return value ? formatDateTime(String(value)) : '—';
}

/** 错误码列：走模块错误码中文映射，未知时显示「未知（码）」而不是裸码。 */
export function cdkError(value: unknown) {
  return value ? moduleErrorLabel(String(value)) : '—';
}

/** 业务标签列（套餐、规格、服务等）。 */
export function cdkTag(value: unknown) {
  return value ? <Tag>{String(value)}</Tag> : '—';
}

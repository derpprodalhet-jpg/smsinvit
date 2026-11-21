import { useEffect, useRef, useState } from 'react';
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components';
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Typography,
  message,
} from 'antd';
import { AdminPage } from '../../components/admin-page';
import { AdminStatusTag } from '../../components/admin-feedback';
import {
  AdminDetailDrawer,
  CopyableValue,
  copyDetailText,
  type DetailGroup,
} from '../../components/admin-detail-drawer';
import { adminApi } from '../../lib/admin-api';
import { formatDateTime } from '../../lib/format-datetime';
import type { Batch, Plan } from '../../lib/redeem-inventory';
import { PRODUCT_KIND_LABEL } from '../../lib/invitation';
import { resolveLabel } from '../../lib/zh-label';
import {
  BATCH_SOURCE_LABEL,
  inventoryStatusLabel,
  inventoryStatusTone,
} from '../../lib/zh-maps';
import {
  EMPTY_BATCH_COUNTS,
  UNASSIGNED_BATCH_LABEL,
  UNASSIGNED_SOURCE_LABEL,
  batchCounts,
  batchCountsText,
  canExportBatch,
  cardCountOf,
  isUnassignedBatch,
  originBatchText,
  sameBatchCounts,
  type BatchCounts,
  type OriginBatch,
} from './model';

/** 列表行：新增批次号 / 备注 / 生成人（后端契约，字段缺失按缺失容错）。 */
type BatchRow = Batch & {
  batchNo?: string;
  note?: string;
  requestedCount?: number;
  actorEmail?: string;
  /** 该批次的实时卡密张数（后端 `cardCount`）；伪批次行由它给出未登记张数。 */
  cardCount?: number | null;
  /** 未登记批次伪批次行的标记（后端保留 id `unassigned`）。 */
  unassigned?: boolean;
  /** 未登记卡密在邀请卡密表里的原始批次名及张数。 */
  originBatches?: OriginBatch[];
};
type BatchExport = {
  batchId: string;
  batchNo?: string;
  count: number;
  secrets?: string[];
  text?: string;
  csv?: string;
  filename?: string;
};
type CardRow = {
  id: string;
  status: string;
  issuedAt?: string;
  secretFingerprint?: string;
  secretMasked?: string;
  secret?: string;
};
type Generated = {
  batchId: string;
  batchNo?: string;
  requestedCount?: number;
  successCount?: number;
  duplicateCount?: number;
  invalidCount?: number;
};
const CARD_PAGE_SIZE = 20;

function downloadText(contents: string, filename: string, mime: string) {
  const url = URL.createObjectURL(new Blob([contents], { type: mime }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function BatchesPage() {
  const action = useRef<ActionType>();
  const [form] = Form.useForm();
  const [plans, setPlans] = useState<Plan[]>([]);
  const [skuId, setSkuId] = useState(
    new URLSearchParams(window.location.search).get('skuId') || '',
  );
  const [open, setOpen] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [busy, setBusy] = useState('');
  const [detail, setDetail] = useState<BatchRow | null>(null);
  /** 批次页三组数字（已登记 / 未登记 / 全部）：后端 meta.counts 为准，缺列时按当前页兜底。 */
  const [counts, setCounts] = useState<BatchCounts>(EMPTY_BATCH_COUNTS);
  const [cards, setCards] = useState<CardRow[]>([]);
  const [cardsTotal, setCardsTotal] = useState(0);
  const [cardsPage, setCardsPage] = useState(1);
  const [cardsLoading, setCardsLoading] = useState(false);
  const [cardsError, setCardsError] = useState('');

  useEffect(() => {
    void adminApi<Plan[]>('/api/v1/admin/plans?page=1&pageSize=100')
      .then((r) => {
        setPlans(r.data || []);
        if (!skuId && r.data?.length) setSkuId(r.data[0].id);
      })
      .catch((e) => message.error(e.message));
    // 首次进入只读取一次套餐列表；skuId 由 URL 或首个套餐决定。
  }, []);

  const planOf = (row: { skuId?: string }) => plans.find((p) => p.id === row.skuId);
  const serviceOf = (row: { skuId?: string; attributesSnapshotJson?: Record<string, unknown> }) =>
    resolveLabel(
      PRODUCT_KIND_LABEL,
      String(
        row.attributesSnapshotJson?.productKind ||
          planOf(row)?.attributesJson?.productKind ||
          '',
      ),
    );
  const validDays = (row: { attributesSnapshotJson?: Record<string, unknown> }) =>
    Number(row.attributesSnapshotJson?.cdkValidDays ?? 15);
  const batchNoOf = (row: { batchNo?: string }) => String(row.batchNo || '').trim();

  /** 拉取某批次的导出内容；失败时返回 null 并提示，调用方不再继续。 */
  async function fetchExport(id: string): Promise<BatchExport | null> {
    try {
      const response = await adminApi<BatchExport>(
        `/api/v1/admin/inventory/batches/${id}/export`,
      );
      return response.data;
    } catch (e) {
      message.error(e instanceof Error ? e.message : '导出失败');
      return null;
    }
  }

  /**
   * 独角「批量录入」把粘贴框按行去空白后**逐行都当卡密**解析，批次号是独立的输入框。
   * 所以这里只复制卡密本身：不要把批次号拼进去，否则会在独角里多出一张叫
   * `BATCH-…` 的假卡密。
   */
  async function copySecrets(row: BatchRow) {
    setBusy(`copy:${row.id}`);
    try {
      const exported = await fetchExport(row.id);
      if (!exported) return;
      const secrets = exported.secrets?.length
        ? exported.secrets.join('\n')
        : (exported.text || '').trim();
      if (!secrets) {
        message.info('该批次没有可复制的卡密');
        return;
      }
      // 结尾不留空行：粘贴进独角前保持干净。
      await copyDetailText(secrets.replace(/\s+$/, ''));
    } finally {
      setBusy('');
    }
  }

  /** 批次号单独复制，粘到独角导入界面的「批次号」输入框。 */
  async function copyBatchNo(row: BatchRow) {
    const batchNo = batchNoOf(row);
    if (!batchNo) {
      message.info('该批次还没有批次号');
      return;
    }
    await copyDetailText(batchNo);
  }

  async function downloadCsv(row: BatchRow) {
    setBusy(`csv:${row.id}`);
    try {
      const exported = await fetchExport(row.id);
      if (!exported) return;
      const contents = exported.csv || exported.text || '';
      if (!contents) {
        message.info('该批次没有可下载的内容');
        return;
      }
      const batchNo = batchNoOf(row) || String(exported.batchNo || '').trim();
      // Excel 打开 UTF-8 CSV 需要 BOM，否则纯中文表头会乱码。
      downloadText(
        exported.csv ? `\ufeff${contents}` : contents,
        exported.filename || `${batchNo || exported.batchId}.csv`,
        exported.csv ? 'text/csv;charset=utf-8' : 'text/plain;charset=utf-8',
      );
      message.success('已开始下载');
    } finally {
      setBusy('');
    }
  }

  async function loadCards(row: BatchRow, page: number) {
    setCardsLoading(true);
    setCardsError('');
    try {
      const q = new URLSearchParams({ page: String(page), pageSize: String(CARD_PAGE_SIZE) });
      // 未登记批次没有批次行，范围只能按套餐圈定（后端按 skuId + import_batch_id IS NULL 查）。
      if (isUnassignedBatch(row) && row.skuId) q.set('skuId', String(row.skuId));
      const response = await adminApi<CardRow[]>(
        `/api/v1/admin/inventory/batches/${row.id}/cards?${q}`,
      );
      setCards(response.data || []);
      setCardsTotal(response.meta?.total ?? (response.data || []).length);
      setCardsPage(response.meta?.page ?? page);
    } catch (e) {
      setCards([]);
      setCardsTotal(0);
      setCardsError(e instanceof Error ? e.message : '读取本批卡密失败');
    } finally {
      setCardsLoading(false);
    }
  }

  function openDetail(row: BatchRow) {
    setDetail(row);
    setCards([]);
    setCardsTotal(0);
    setCardsPage(1);
    void loadCards(row, 1);
  }

  async function invalidateCard(id: string) {
    try {
      await adminApi(`/api/v1/admin/inventory/${id}/invalidate`, { method: 'POST' });
      message.success('卡密已作废');
      if (detail) await loadCards(detail, cardsPage);
      action.current?.reload();
    } catch (e) {
      message.error(e instanceof Error ? e.message : '操作失败');
    }
  }

  async function generate() {
    let values: { skuId: string; count: number; length: number; note?: string };
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setGenerating(true);
    try {
      const response = await adminApi<Generated>('/api/v1/admin/inventory/generate', {
        method: 'POST',
        body: JSON.stringify({
          skuId: values.skuId,
          count: values.count,
          length: values.length,
          note: values.note || undefined,
        }),
      });
      const created = response.data;
      message.success(`已生成 ${created.successCount ?? 0} 条卡密`);
      setOpen(false);
      setSkuId(values.skuId);
      action.current?.reload();
      // 生成后立刻展示批次号，方便直接复制进独角。
      openDetail({
        id: created.batchId,
        skuId: values.skuId,
        batchNo: created.batchNo,
        note: values.note,
        requestedCount: created.requestedCount,
        successCount: created.successCount ?? 0,
        duplicateCount: created.duplicateCount,
        invalidCount: created.invalidCount,
        attributesSnapshotJson: planOf({ skuId: values.skuId })?.attributesJson,
        createdAt: new Date().toISOString(),
      });
      if (!created.batchNo)
        message.warning('后端未返回批次号，请刷新列表后重新查看该批次。');
    } catch (e) {
      if (e instanceof Error && !(e as { errorFields?: unknown }).errorFields)
        message.error(e.message);
    } finally {
      setGenerating(false);
    }
  }

  const columns: ProColumns<BatchRow>[] = [
    {
      title: '批次号',
      dataIndex: 'batchNo',
      search: false,
      ellipsis: true,
      render: (_, row) =>
        // 未登记批次没有批次号：显示行名与原始批次名，绝不拿保留 id 当批次号。
        isUnassignedBatch(row) ? (
          <Space direction="vertical" size={0}>
            <Typography.Text strong>{UNASSIGNED_BATCH_LABEL}</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              原始批次名：{originBatchText(row) || '—'}
            </Typography.Text>
          </Space>
        ) : batchNoOf(row) ? (
          <Typography.Text copyable={{ text: batchNoOf(row) }} code>
            {batchNoOf(row)}
          </Typography.Text>
        ) : (
          '—'
        ),
    },
    {
      title: '套餐',
      search: false,
      render: (_, row) => planOf(row)?.name || '—',
    },
    {
      title: '服务',
      search: false,
      render: (_, row) => serviceOf(row),
    },
    {
      title: '卡密张数',
      search: false,
      render: (_, row) => cardCountOf(row),
    },
    {
      title: '生成数量',
      dataIndex: 'successCount',
      search: false,
      render: (_, row) => (isUnassignedBatch(row) ? '—' : (row.successCount ?? '—')),
    },
    {
      title: '有效期',
      search: false,
      // 未登记批次不是本站按套餐生成的，没有「生成起算」的有效期，不能套默认 15 天。
      render: (_, row) => (isUnassignedBatch(row) ? '—' : `${validDays(row)} 天（生成起算）`),
    },
    {
      title: '生成时间',
      dataIndex: 'createdAt',
      search: false,
      render: (_, row) => formatDateTime(row.createdAt),
    },
    {
      title: '备注',
      dataIndex: 'note',
      search: false,
      ellipsis: true,
      render: (_, row) => String(row.note || '').trim() || '—',
    },
    {
      title: '操作',
      valueType: 'option',
      width: 300,
      render: (_, row) =>
        // 未登记批次只读：没有批次行，复制批次号 / 复制卡密 / 下载 CSV 都不适用。
        !canExportBatch(row) ? (
          <Space wrap size={8}>
            <Button type="link" size="small" onClick={() => openDetail(row)}>
              详情
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              只读（无批次导出）
            </Typography.Text>
          </Space>
        ) : (
          <Space wrap size={0}>
            <Button
              type="link"
              size="small"
              loading={busy === `copy:${row.id}`}
              onClick={() => void copySecrets(row)}
            >
              复制卡密
            </Button>
            <Button
              type="link"
              size="small"
              disabled={!batchNoOf(row)}
              onClick={() => void copyBatchNo(row)}
            >
              复制批次号
            </Button>
            <Button type="link" size="small" onClick={() => openDetail(row)}>
              详情
            </Button>
            <Button
              type="link"
              size="small"
              loading={busy === `csv:${row.id}`}
              onClick={() => void downloadCsv(row)}
            >
              下载 CSV
            </Button>
          </Space>
        ),
    },
  ];

  const detailBatchNo = detail ? batchNoOf(detail) : '';
  /** 未登记批次（历史导入 / 手工创建）只读：没有批次行，导出与作废都不适用。 */
  const detailReadOnly = isUnassignedBatch(detail);
  const detailGroups: DetailGroup[] = detail
    ? [
        detailReadOnly
          ? {
              key: 'unregistered',
              title: UNASSIGNED_BATCH_LABEL,
              items: [
                {
                  key: 'why',
                  label: '说明',
                  children:
                    '这些卡密是历史导入或人工创建，没有批次归属（`inventory_import_batches` 里没有对应行）。这里按套餐只读列出，不提供批次号、批次导出与作废。',
                },
                {
                  key: 'origin',
                  label: '原始批次名',
                  children: originBatchText(detail) || '—（未记录）',
                },
                { key: 'total', label: '卡密张数', children: cardCountOf(detail) },
              ],
            }
          : {
              key: 'dujiao',
              title: '导入独角',
              items: [
                { key: 'step1', label: '①', children: '在独角「卡密管理 → 导入」选定商品与 SKU' },
                { key: 'step2', label: '②', children: '把「复制卡密」的内容粘贴进卡密框（每行一个），或在另一个区域上传 CSV' },
                {
                  key: 'step3',
                  label: '③',
                  children: `批次号填 ${detailBatchNo || 'BATCH-…'}`,
                },
                { key: 'step4', label: '④', children: '备注可选' },
                {
                  key: 'warning',
                  label: '注意',
                  children: '不要把批次号跟卡密粘在一起，独角会把批次号也当成一张卡密。',
                },
                {
                  key: 'note',
                  label: '说明',
                  children: '独角导入只读取 secret 列 / 每行一个卡密。',
                },
              ],
            },
        {
          key: 'batch',
          title: '批次信息',
          items: [
            {
              key: 'batchNo',
              label: '批次号',
              children: detailReadOnly ? (
                '—（未登记批次没有批次号）'
              ) : detailBatchNo ? (
                <CopyableValue value={detailBatchNo} />
              ) : (
                '—（后端未返回批次号，请刷新列表）'
              ),
            },
            {
              key: 'id',
              label: '批次 ID',
              // 未登记批次不是数据库里的批次，把保留值标出来，避免被当成真批次号复制走。
              children: detailReadOnly ? (
                `${detail.id}（保留值，非数据库批次）`
              ) : (
                <CopyableValue value={detail.id} />
              ),
            },
            { key: 'plan', label: '套餐', children: planOf(detail)?.name || '—' },
            { key: 'service', label: '服务', children: serviceOf(detail) },
            { key: 'count', label: '卡密张数', children: cardCountOf(detail) },
            { key: 'requested', label: '生成数量', children: detail.requestedCount ?? '—' },
            { key: 'success', label: '成功生成', children: detail.successCount ?? '—' },
            { key: 'duplicate', label: '重复跳过', children: detail.duplicateCount ?? '—' },
            { key: 'invalid', label: '无效数量', children: detail.invalidCount ?? '—' },
            {
              key: 'days',
              label: '有效期',
              children: detailReadOnly ? '—' : `${validDays(detail)} 天（生成起算）`,
            },
            { key: 'noteText', label: '备注', children: String(detail.note || '').trim() || '—' },
            {
              key: 'source',
              label: '来源',
              children: detailReadOnly
                ? UNASSIGNED_SOURCE_LABEL
                : detail.source
                  ? resolveLabel(BATCH_SOURCE_LABEL, detail.source)
                  : '—',
            },
            ...(detailReadOnly && originBatchText(detail)
              ? [{ key: 'origin', label: '原始批次名', children: originBatchText(detail) }]
              : []),
            { key: 'actor', label: '生成人', children: String(detail.actorEmail || '').trim() || '—' },
            {
              key: 'createdAt',
              label: '生成时间',
              children: formatDateTime(detail.createdAt),
            },
          ],
        },
      ]
    : [];

  return (
    <AdminPage
      title="卡密批次"
      // 三组数字由 batchCountsText 统一生成：已登记 + 未登记 = 全部，与「卡密管理」页对得上。
      description={batchCountsText(counts)}
    >
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          style={{ minWidth: 240 }}
          value={skuId || undefined}
          placeholder="选择套餐"
          options={plans.map((p) => ({
            value: p.id,
            label: `${p.name} · ${PRODUCT_KIND_LABEL[String(p.attributesJson.productKind)] || '—'}`,
          }))}
          onChange={(value) => {
            setSkuId(value || '');
            queueMicrotask(() => action.current?.reload());
          }}
        />
        <Button
          type="primary"
          onClick={() => {
            form.setFieldsValue({ skuId: skuId || undefined, count: 100, length: 20, note: '' });
            setOpen(true);
          }}
        >
          批量生成卡密
        </Button>
      </Space>
      <ProTable<BatchRow>
        rowKey="id"
        actionRef={action}
        columns={columns}
        search={false}
        scroll={{ x: 1280 }}
        params={{ skuId }}
        pagination={{ defaultPageSize: 20 }}
        request={async (p) => {
          if (!skuId) {
            setCounts((prev) => (sameBatchCounts(prev, EMPTY_BATCH_COUNTS) ? prev : EMPTY_BATCH_COUNTS));
            return { data: [], total: 0, success: true };
          }
          const q = new URLSearchParams({
            page: String(p.current || 1),
            pageSize: String(p.pageSize || 20),
          });
          q.set('skuId', skuId);
          const r = await adminApi<BatchRow[]>(`/api/v1/admin/inventory/batches?${q}`);
          // 三组数字取后端整表口径（未登记批次不在本页时也算得对）；相等时不触发重渲染。
          const next = batchCounts(r.data || [], r.meta);
          setCounts((prev) => (sameBatchCounts(prev, next) ? prev : next));
          return { data: r.data || [], total: r.meta?.total || 0, success: true };
        }}
      />
      <AdminDetailDrawer
        title={
          detailBatchNo
            ? `批次 · ${detailBatchNo}`
            : detailReadOnly
              ? `批次 · ${UNASSIGNED_BATCH_LABEL}`
              : `批次 · ${detail?.id || ''}`
        }
        subtitle={
          detail
            ? detailReadOnly
              ? `${planOf(detail)?.name || '—'} · ${serviceOf(detail)} · ${cardCountOf(detail)} 条 · ${UNASSIGNED_SOURCE_LABEL}`
              : `${planOf(detail)?.name || '—'} · ${serviceOf(detail)} · ${detail.successCount ?? 0} 条 · ${validDays(detail)} 天（生成起算）· ${formatDateTime(detail.createdAt)}`
            : undefined
        }
        open={!!detail}
        onClose={() => setDetail(null)}
        width={900}
        extra={
          <>
            <Button
              disabled={!detailBatchNo || detailReadOnly}
              onClick={() =>
                detailBatchNo && !detailReadOnly
                  ? void copyDetailText(detailBatchNo)
                  : message.info(
                      detailReadOnly ? '未登记批次没有批次号' : '该批次还没有批次号',
                    )
              }
            >
              复制批次号
            </Button>
            <Button
              disabled={detailReadOnly}
              onClick={async () => {
                if (!detail || detailReadOnly) return;
                const exported = await fetchExport(detail.id);
                if (!exported) return;
                const secrets = exported.secrets?.length
                  ? exported.secrets.join('\n')
                  : (exported.text || '').trim();
                if (!secrets) {
                  message.info('该批次没有可复制的卡密');
                  return;
                }
                await copyDetailText(secrets.replace(/\s+$/, ''));
              }}
            >
              复制卡密（每行一个）
            </Button>
            <Button
              disabled={!detail || detailReadOnly}
              onClick={() => detail && !detailReadOnly && void downloadCsv(detail)}
            >
              下载 CSV
            </Button>
          </>
        }
        groups={detailGroups}
      >
        <Typography.Title level={5} style={{ margin: 0 }}>
          本批卡密
        </Typography.Title>
        {detailReadOnly && (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 8 }}
            message={`${UNASSIGNED_BATCH_LABEL}只读：不提供批次导出与作废。`}
            description="这些卡密没有批次归属，作废/导出会以「批次」为单位留下记录，对历史卡密没有意义，因此本页不给这两个操作。"
          />
        )}
        {cardsError && (
          <Alert
            type="warning"
            showIcon
            message={`本批卡密读取失败：${cardsError}`}
            description="后端可能尚未提供「本批卡密」接口；批次号与导出仍可正常使用。"
          />
        )}
        <Table<CardRow>
          rowKey="id"
          size="small"
          loading={cardsLoading}
          dataSource={cards}
          scroll={{ x: 760 }}
          locale={{ emptyText: cardsError ? '暂不可用' : '该批次没有卡密' }}
          pagination={{
            current: cardsPage,
            pageSize: CARD_PAGE_SIZE,
            total: cardsTotal,
            showSizeChanger: false,
            showTotal: (total) => `共 ${total} 张卡密`,
            onChange: (page) => void (detail && loadCards(detail, page)),
          }}
          columns={[
            {
              title: '卡密',
              dataIndex: 'secret',
              ellipsis: true,
              render: (_: unknown, row: CardRow) =>
                row.secret ? (
                  <Typography.Text copyable={{ text: row.secret }} code>
                    {row.secret}
                  </Typography.Text>
                ) : row.secretMasked ? (
                  row.secretMasked
                ) : (
                  '已脱敏（需要卡密权限）'
                ),
            },
            {
              title: '状态',
              dataIndex: 'status',
              width: 110,
              // 库存状态走共用中文映射（含 `CONSUMED`→已核销）：以前这里只有
              // ISSUED/INVALID 两条，卡密被核销后列表会显示「未知（CONSUMED）」。
              render: (status: string) => (
                <AdminStatusTag tone={inventoryStatusTone(status)}>
                  {inventoryStatusLabel(status)}
                </AdminStatusTag>
              ),
            },
            {
              title: '发行时间',
              dataIndex: 'issuedAt',
              width: 180,
              render: (value: string) => (value ? formatDateTime(value) : '—'),
            },
            {
              title: '操作',
              width: 110,
              // 未登记批次只读：不提供逐卡作废（历史卡密没有批次凭据，避免误操作）。
              render: (_: unknown, row: CardRow) =>
                !detailReadOnly && row.status === 'ISSUED' ? (
                  <Popconfirm
                    title="作废这张卡密？"
                    description="作废后不能兑换；已经提交的上游操作仍需跟进结果。"
                    onConfirm={() => void invalidateCard(row.id)}
                  >
                    <Button type="link" size="small" danger>
                      作废
                    </Button>
                  </Popconfirm>
                ) : null,
            },
          ]}
        />
      </AdminDetailDrawer>
      <Modal
        title="生成卡密"
        open={open}
        onCancel={() => !generating && setOpen(false)}
        onOk={() => void generate()}
        confirmLoading={generating}
      >
        <Alert
          type="info"
          showIcon
          message="生成成功即开始计算有效期；生成卡密不会立即触发对外兑换。"
          style={{ marginBottom: 16 }}
        />
        <Form form={form} layout="vertical">
          <Form.Item name="skuId" label="套餐" rules={[{ required: true }]}>
            <Select
              options={plans
                .filter((p) => p.status === 'ACTIVE')
                .map((p) => ({
                  value: p.id,
                  label: `${p.name} · ${PRODUCT_KIND_LABEL[String(p.attributesJson.productKind)] || '—'}`,
                }))}
            />
          </Form.Item>
          <Form.Item name="count" label="数量" rules={[{ required: true }]}>
            <InputNumber min={1} max={1000} />
          </Form.Item>
          <Form.Item name="length" label="卡密长度" rules={[{ required: true }]}>
            <InputNumber min={12} max={64} />
          </Form.Item>
          <Form.Item name="note" label="备注" extra="会写入批次，方便在独角对上商品。">
            <Input maxLength={120} placeholder="可选，例如：独角商城 3 月批" />
          </Form.Item>
        </Form>
      </Modal>
    </AdminPage>
  );
}

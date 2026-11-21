import { Card, Table, Tooltip, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Link } from '../lib/navigation';
import {
  COST_COLUMN_HINTS,
  costAmountText,
  costAmountTooltip,
  costBasisTooltip,
  costUnpricedNote,
  groupCostsByKey,
  type StatsOverviewCost,
} from '../lib/stats-overview';

function header(title: string, hint: string) {
  return (
    <Tooltip title={hint}>
      <span>{title}</span>
    </Tooltip>
  );
}

function AmountCell({ cost, which }: { cost: StatsOverviewCost; which: 'total' | 'today' }) {
  const text = costAmountText(cost, which);
  const hint = costAmountTooltip(cost);
  const body = <span className="admin-stat-number">{text}</span>;
  // available:false 或 unpricedCount>0 时必须能悬浮看到原因；没有说明就别套空 Tooltip。
  return hint ? <Tooltip title={hint}>{body}</Tooltip> : body;
}

/**
 * 块② 成本：行 = `costs`（同 key 多币种分行、按币种升序）。
 *
 * 金额是 minor 整数，用 `formatMinor` 显示（`$0.15` / `300.00 BDT`）；
 * **没有跨币种合计行** —— 不同币种直接相加没有业务含义。
 */
export function StatsCostsTable({ costs }: { costs: StatsOverviewCost[] }) {
  const rows = groupCostsByKey(costs ?? []);
  const columns: ColumnsType<StatsOverviewCost> = [
    {
      title: '项目',
      dataIndex: 'label',
      key: 'label',
      width: 200,
      render: (_, row) =>
        row.detailPath ? <Link href={row.detailPath}>{row.label}</Link> : row.label,
    },
    {
      title: '币种',
      dataIndex: 'currency',
      key: 'currency',
      width: 90,
      render: (_, row) => row.currency || '—',
    },
    {
      title: header('累计金额', COST_COLUMN_HINTS.totalMinor),
      dataIndex: 'totalMinor',
      key: 'totalMinor',
      align: 'right',
      render: (_, row) => <AmountCell cost={row} which="total" />,
    },
    {
      title: header('今日金额', COST_COLUMN_HINTS.todayMinor),
      dataIndex: 'todayMinor',
      key: 'todayMinor',
      align: 'right',
      render: (_, row) => <AmountCell cost={row} which="today" />,
    },
    {
      title: header('口径', COST_COLUMN_HINTS.basis),
      dataIndex: 'basis',
      key: 'basis',
      render: (_, row) => {
        const unpriced = costUnpricedNote(row);
        const hint = costBasisTooltip(row);
        const body = (
          <span>
            {row.basis?.trim() || '—'}
            {unpriced ? (
              <>
                {' '}
                <Typography.Text type="warning" style={{ fontSize: 12 }}>
                  {unpriced}
                </Typography.Text>
              </>
            ) : null}
          </span>
        );
        return hint ? <Tooltip title={hint}>{body}</Tooltip> : body;
      },
    },
  ];
  return (
    <Card size="small" title="② 成本（按币种分行，不做跨币种合计）" className="admin-stat-block" style={{ marginBottom: 16 }}>
      <Typography.Paragraph type="secondary" style={{ marginTop: 0, fontSize: 12 }}>
        金额单位是最小单位整数（minor）；同一项目不同币种分行展示，跨币种直接相加没有业务含义，因此不提供合计。
      </Typography.Paragraph>
      <Table<StatsOverviewCost>
        size="small"
        rowKey={(row) => `${row.key}:${row.currency}`}
        columns={columns}
        dataSource={rows}
        pagination={false}
        scroll={{ x: 'max-content' }}
      />
    </Card>
  );
}

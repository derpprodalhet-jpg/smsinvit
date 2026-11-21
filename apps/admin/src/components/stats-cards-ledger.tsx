import { Card, Table, Tooltip, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Link } from '../lib/navigation';
import {
  EXPIRED_NOT_APPLICABLE,
  EXPIRED_NOT_APPLICABLE_TOOLTIP,
  LEDGER_COLUMN_HINTS,
  TOTAL_LABEL,
  TOTAL_MODULE_KEY,
  formatCount,
  moduleDetailPath,
  orderModules,
  type StatsOverviewCards,
} from '../lib/stats-overview';

/** 台账行：模块数据 + 末尾合计行（合计行没有 module 语义，用哨兵 key 区分）。 */
type LedgerRow = {
  module: string;
  label: string;
  generatedTotal: number | null;
  generatedToday: number | null;
  redeemedTotal: number | null;
  redeemedToday: number | null;
  expired: number | null;
  expiredApplicable: boolean;
};

type NumberField = 'generatedTotal' | 'generatedToday' | 'redeemedTotal' | 'redeemedToday' | 'expired';

function header(title: string, hint: string) {
  return (
    <Tooltip title={hint}>
      <span>{title}</span>
    </Tooltip>
  );
}

/**
 * 块① 卡密台账：行 = 模块（接码 / Codex 邀请）+ 合计；
 * 列 = 累计生成 / 今日生成 / 累计核销 / 今日核销 / 已过期。
 *
 * 每个数字格都是到该模块明细页的下钻链接；Codex 邀请没有有效期概念，
 * 「已过期」格显示「不适用」而不是 0。
 */
export function StatsCardsLedger({ cards }: { cards: StatsOverviewCards }) {
  const rows: LedgerRow[] = orderModules(cards.modules ?? []);
  if (cards.totals) {
    rows.push({
      module: TOTAL_MODULE_KEY,
      label: TOTAL_LABEL,
      ...cards.totals,
      expiredApplicable: true,
    });
  }
  function numberCell(row: LedgerRow, field: NumberField) {
    const text = formatCount(row[field]);
    if (row.module === TOTAL_MODULE_KEY) {
      return <span className="admin-stat-number">{text}</span>;
    }
    const href = moduleDetailPath(row.module);
    const value = <span className="admin-stat-number">{text}</span>;
    return href ? (
      <Tooltip title={`查看「${row.label}」明细`}>
        <Link href={href}>{value}</Link>
      </Tooltip>
    ) : (
      value
    );
  }
  const columns: ColumnsType<LedgerRow> = [
    {
      title: '模块',
      dataIndex: 'label',
      key: 'label',
      width: 160,
      render: (_, row) =>
        row.module === TOTAL_MODULE_KEY ? (
          <strong>{row.label}</strong>
        ) : moduleDetailPath(row.module) ? (
          <Link href={moduleDetailPath(row.module)!}>{row.label}</Link>
        ) : (
          row.label
        ),
    },
    {
      title: header('累计生成', LEDGER_COLUMN_HINTS.generatedTotal),
      dataIndex: 'generatedTotal',
      key: 'generatedTotal',
      align: 'right',
      render: (_, row) => numberCell(row, 'generatedTotal'),
    },
    {
      title: header('今日生成', LEDGER_COLUMN_HINTS.generatedToday),
      dataIndex: 'generatedToday',
      key: 'generatedToday',
      align: 'right',
      render: (_, row) => numberCell(row, 'generatedToday'),
    },
    {
      title: header('累计核销', LEDGER_COLUMN_HINTS.redeemedTotal),
      dataIndex: 'redeemedTotal',
      key: 'redeemedTotal',
      align: 'right',
      render: (_, row) => numberCell(row, 'redeemedTotal'),
    },
    {
      title: header('今日核销', LEDGER_COLUMN_HINTS.redeemedToday),
      dataIndex: 'redeemedToday',
      key: 'redeemedToday',
      align: 'right',
      render: (_, row) => numberCell(row, 'redeemedToday'),
    },
    {
      title: header('已过期', LEDGER_COLUMN_HINTS.expired),
      dataIndex: 'expired',
      key: 'expired',
      align: 'right',
      render: (_, row) => {
        if (!row.expiredApplicable) {
          return (
            <Tooltip title={EXPIRED_NOT_APPLICABLE_TOOLTIP}>
              <Typography.Text type="secondary">{EXPIRED_NOT_APPLICABLE}</Typography.Text>
            </Tooltip>
          );
        }
        return numberCell(row, 'expired');
      },
    },
  ];
  return (
    <Card size="small" title="① 卡密台账（按模块）" className="admin-stat-block" style={{ marginBottom: 16 }}>
      <Table<LedgerRow>
        size="small"
        rowKey="module"
        columns={columns}
        dataSource={rows}
        pagination={false}
        scroll={{ x: 'max-content' }}
        rowClassName={(row) => (row.module === TOTAL_MODULE_KEY ? 'admin-stat-total-row' : '')}
      />
    </Card>
  );
}

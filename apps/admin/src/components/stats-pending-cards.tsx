import { Card, Col, Row } from 'antd';
import { AdminMetricCard } from './admin-metric-card';
import { formatCount, pendingTone, type StatsOverviewPending } from '../lib/stats-overview';

/**
 * 块③ 待处理：一排小卡，每项 = `pending[]` 的 label + count。
 *
 * 这是工作台上**唯一**允许出现卡片的地方，且卡片数量严格跟随接口返回的
 * `pending[]`（不是硬编码的指标清单）；count>0 用 warning/error 色高亮，
 * count===0 弱化；有 detailPath 时整卡可点下钻。
 */
export function StatsPendingCards({ pending }: { pending: StatsOverviewPending[] }) {
  const items = pending ?? [];
  if (!items.length) return null;
  return (
    <Card size="small" title="③ 待处理" className="admin-stat-block admin-pending-block">
      <Row gutter={[12, 12]}>
        {items.map((item) => {
          const count = item.count ?? 0;
          return (
            <Col key={item.key} xs={12} sm={12} md={8} xl={6}>
              <div
                className={
                  count > 0 ? 'admin-pending-card' : 'admin-pending-card admin-pending-card--idle'
                }
              >
                <AdminMetricCard
                  title={item.label}
                  value={formatCount(item.count)}
                  tone={pendingTone(item.key, item.count)}
                  href={item.detailPath || undefined}
                />
              </div>
            </Col>
          );
        })}
      </Row>
    </Card>
  );
}

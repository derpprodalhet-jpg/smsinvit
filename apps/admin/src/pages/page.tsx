import { useEffect, useState } from 'react';
import { Alert, Button, Space, Typography } from 'antd';
import { AdminPage } from '../components/admin-page';
import { StatsCardsLedger } from '../components/stats-cards-ledger';
import { StatsCostsTable } from '../components/stats-costs-table';
import { StatsPendingCards } from '../components/stats-pending-cards';
import { adminApi } from '../lib/admin-api';
import { setBusinessTimeZone, formatDateTime } from '../lib/format-datetime';
import { Link } from '../lib/navigation';
import { parseStatsOverview, type StatsOverview } from '../lib/stats-overview';

/**
 * 兑换工作台：三块（卡密台账 / 成本 / 待处理）。
 *
 * 之前这里是 12 张同级扁平卡片，把「生成 / 兑换成败 / 成本 / 例外量」四种性质
 * 混在一起。结构改成三块后，`page.tsx` 只负责取数与页头，指标一律由三块组件渲染；
 * 任何人都不要再往这里加同权卡片（tests/workbench-blocks.test.mjs 有守卫）。
 */
export default function Dashboard() {
  const [data, setData] = useState<StatsOverview | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  async function load() {
    setLoading(true);
    setError('');
    try {
      const r = await adminApi<unknown>('/api/v1/admin/stats/overview');
      const overview = parseStatsOverview(r.data);
      setData(overview);
      if (overview.businessTimeZone) setBusinessTimeZone(overview.businessTimeZone);
    } catch (e) {
      // 失败时清掉旧数据：宁可什么都不显示，也不要显示一张全是 0（或过期）的表。
      setData(null);
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  // 只用「成功且没有错误」的数据渲染三块；失败态不显示任何数字。
  const ready = data && !error ? data : null;
  return (
    <AdminPage title="兑换工作台" description="卡密台账、已确认成本与待处理队列。业务日按所配置时区统计。">
      <Space wrap style={{ marginBottom: 16 }}>
        <Link href="/batches">
          <Button type="primary">生成 / 复制卡密</Button>
        </Link>
        <Button loading={loading} onClick={() => void load()}>
          刷新
        </Button>
        {ready ? (
          <Typography.Text type="secondary">
            业务时区：{ready.businessTimeZone || '—'} · 数据截至{' '}
            {formatDateTime(ready.generatedAt)}
          </Typography.Text>
        ) : null}
      </Space>
      {error ? (
        <Alert
          type="error"
          showIcon
          message="统计加载失败"
          description={error}
          style={{ marginBottom: 16 }}
          action={
            <Button size="small" onClick={() => void load()}>
              重试
            </Button>
          }
        />
      ) : null}
      {ready ? (
        <>
          <StatsCardsLedger cards={ready.cards} />
          <StatsCostsTable costs={ready.costs} />
          <StatsPendingCards pending={ready.pending} />
        </>
      ) : null}
    </AdminPage>
  );
}

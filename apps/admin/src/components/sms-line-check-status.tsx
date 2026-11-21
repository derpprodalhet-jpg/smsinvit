import { Alert, Button, Card, Descriptions, Space, Table, Tag } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { adminApi } from '../lib/admin-api';

type Provider = { provider: string; configured: boolean; state: string; reason?: string; remaining: number | null; total: number | null; checkedAt?: string; quotaUpdatedAt?: string; cooldownUntil?: string };
type Decision = { type: string; provider?: string; lineType?: string; reasons: Record<string, string>; createdAt: string };
type Snapshot = { providers: Provider[]; history: Decision[]; lastDecision: Decision | null };
const names: Record<string, string> = { numlookupapi: 'numlookupapi', abstractapi: 'Abstract API' };
const labels: Record<string, string> = {
  checking: '检测中', not_configured: '未配置密钥', not_checked: '尚未检测', available: '最近调用成功',
  quota_exhausted: '额度耗尽', rate_limited: '请求过于频繁', invalid_credentials: '密钥无效或无权限',
  service_error: '服务异常', unavailable: '连接失败或超时', invalid_response: '返回数据无法确认', awaiting_retry: '冷却结束，等待重试',
};
const time = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '暂无记录';
const reasons = (value: Record<string, string>) => Object.entries(value).map(([provider, reason]) => `${names[provider] || '检测接口'}：${labels[reason] || '暂不可用'}`).join('；');
function decisionText(value: Decision) {
  if (value.type === 'LINE_TYPE_SKIPPED') return '已跳过检测，继续取号';
  if (value.type === 'LINE_TYPE_REJECTED') return '非手机号码，已拦截';
  return Object.keys(value.reasons).length ? '已切换备用接口，检测通过' : '检测通过';
}
export function SmsLineCheckStatus() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await adminApi<Snapshot>('/api/v1/admin/sms/line-check-status');
      setSnapshot(data); setError('');
    } catch { setError('读取检测状态失败，请稍后刷新。'); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => { if (document.visibilityState === 'visible') void refresh(); }, 15000);
    return () => window.clearInterval(interval);
  }, [refresh]);
  const last = snapshot?.lastDecision;
  return <Card size="small" title="检测接口额度与状态" style={{ margin: '12px 0' }}
    extra={<Button aria-label="刷新检测状态" onClick={() => void refresh()} loading={loading}>刷新检测状态</Button>}>
    <p>这里只读取本地记录，刷新不会请求检测接口。额度为最近一次接口返回值，可能滞后；未返回具体额度时显示未知。</p>
    {error && <Alert type="error" showIcon message={error} />}
    <Space wrap align="start" style={{ width: '100%' }}>
      {snapshot?.providers.map(row => <Card key={row.provider} size="small" title={names[row.provider]} style={{ width: 280, maxWidth: '100%' }}>
        <Descriptions size="small" column={1} items={[
          { key: 'state', label: '当前状态', children: <Tag color={row.state === 'available' ? 'green' : 'default'}>{labels[row.state] || '未知'}</Tag> },
          { key: 'quota', label: '剩余额度', children: row.remaining == null ? '未知（接口未返回）' : `${row.remaining} 次${row.total == null ? '' : ` / 总计 ${row.total} 次`}` },
          { key: 'quotaTime', label: '额度记录时间', children: time(row.quotaUpdatedAt) },
          { key: 'checkTime', label: '最近调用', children: time(row.checkedAt) },
          { key: 'retry', label: '冷却截止', children: time(row.cooldownUntil) },
        ]} />
      </Card>)}
    </Space>
    {last && <Alert style={{ marginTop: 12 }} showIcon type={last.type === 'LINE_TYPE_SKIPPED' ? 'warning' : 'info'}
      message={`最近结果：${decisionText(last)}`}
      description={`${last.provider ? `使用 ${names[last.provider] || '检测接口'}。` : ''}${reasons(last.reasons)}（${time(last.createdAt)}）`} />}
    <Table<Decision> style={{ marginTop: 12 }} size="small" dataSource={snapshot?.history || []}
      rowKey={(_, index) => String(index)} pagination={{ pageSize: 5, showSizeChanger: false }}
      locale={{ emptyText: '暂无检测记录' }} scroll={{ x: 620 }} columns={[
        { title: '时间', dataIndex: 'createdAt', render: value => time(value), width: 180 },
        { title: '结果', render: (_, row) => decisionText(row), width: 210 },
        { title: '使用接口 / 原因', render: (_, row) => [row.provider ? names[row.provider] : '', reasons(row.reasons)].filter(Boolean).join('；') || '主接口正常' },
      ]} />
  </Card>;
}

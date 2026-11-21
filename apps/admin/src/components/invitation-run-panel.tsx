import { useEffect } from 'react';
import { Alert, Button, Card, Space, Tag, Typography } from 'antd';
import { useInvitationRuntime } from '../lib/invitation-runtime';
import { InvitationStatus } from './invitation-status';
import { Link } from '../lib/navigation';

export function InvitationRunPanel({ runId }: { runId?: string | null }) {
  const runtime = useInvitationRuntime();
  const { watchRun } = runtime;
  useEffect(() => {
    if (runId) watchRun(runId);
  }, [runId, watchRun]);
  if (!runtime.runId) return null;
  return (
    <Card
      title="任务状态与日志"
      extra={
        <Space>
          <Link href="/invitation/runs">运行记录</Link>
          <Button size="small" onClick={() => void runtime.refresh()}>
            刷新
          </Button>
          <Button size="small" onClick={runtime.clearRun}>
            关闭查看
          </Button>
        </Space>
      }
    >
      <Space wrap style={{ marginBottom: 12 }}>
        <Typography.Text code>{runtime.runId}</Typography.Text>
        {/* 以前这里默认显示"运行中"：详情还没回来/查询失败时就是假的实时状态。 */}
        {runtime.run ? (
          <InvitationStatus status={runtime.run.status} />
        ) : (
          <Tag color={runtime.error ? 'red' : 'default'}>
            {runtime.error ? '状态查询失败' : '加载中'}
          </Tag>
        )}
        <Typography.Text>{runtime.run?.email}</Typography.Text>
      </Space>
      {runtime.error && (
        <Alert type="error" showIcon message={runtime.error} style={{ marginBottom: 12 }} />
      )}
      {runtime.truncated && (
        <Alert type="info" message="日志较长，显示最近 128 KB。" style={{ marginBottom: 12 }} />
      )}
      <pre
        aria-label="执行日志"
        style={{
          maxHeight: 380,
          overflow: 'auto',
          whiteSpace: 'pre-wrap',
          overflowWrap: 'anywhere',
          padding: 16,
          borderRadius: 8,
          background: '#f5f7f9',
          margin: 0,
        }}
      >
        {runtime.log || '等待日志输出…'}
      </pre>
    </Card>
  );
}

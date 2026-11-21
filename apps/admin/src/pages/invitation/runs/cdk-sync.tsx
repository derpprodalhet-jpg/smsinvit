import { Alert, Space, Tag } from 'antd';
import { resolveLabel } from '../../../lib/zh-label';
import { CDK_SYNC_LABEL } from '../../../lib/zh-maps';

export type CdkSyncStatus = 'done' | 'unbound' | 'pending' | 'failed';

const SYNC_TONE: Record<string, string | undefined> = {
  done: 'green',
  unbound: undefined,
  pending: 'orange',
  failed: 'red',
};

/**
 * 卡密写回进度。
 *
 * 未命中的状态以前会让 `SYNC_STATUS[status].color` 直接抛异常，现在统一走
 * `resolveLabel`：出现新状态时显示「未知（原始值）」而不是白屏。
 */
export function CdkSyncTag({ status }: { status?: string | null }) {
  if (!status) return <>—</>;
  return <Tag color={SYNC_TONE[status]}>{resolveLabel(CDK_SYNC_LABEL, status)}</Tag>;
}

export function CdkSyncDetails({ status, error }: { status?: CdkSyncStatus; error?: string }) {
  if (!status || status === 'done' || status === 'unbound') return null;
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert
        showIcon
        type={status === 'failed' ? 'warning' : 'info'}
        message={status === 'failed' ? '卡密状态同步失败' : '卡密状态尚待同步'}
        description={
          error || '授权结果与卡密同步状态分别记录。请刷新运行详情确认同步结果，或到卡密管理查看。'
        }
      />
    </Space>
  );
}

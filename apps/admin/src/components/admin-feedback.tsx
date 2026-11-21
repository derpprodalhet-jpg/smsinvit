import { Alert, Button, Empty, Tag } from 'antd';
import type { CSSProperties, ReactNode } from 'react';

export type AdminTone = 'default' | 'success' | 'warning' | 'error' | 'processing';

const TONE_COLOR: Record<AdminTone, string> = {
  default: 'default',
  success: 'success',
  warning: 'warning',
  error: 'error',
  processing: 'processing',
};

/** Semantic status chip — prefer tone over raw Ant colors at call sites. */
export function AdminStatusTag({
  tone = 'default',
  children,
}: {
  tone?: AdminTone;
  children: ReactNode;
}) {
  return <Tag color={TONE_COLOR[tone]}>{children}</Tag>;
}

export function AdminErrorAlert({
  message,
  description,
  onRetry,
  retryLabel = '重试',
  style,
}: {
  message: string;
  description?: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  style?: CSSProperties;
}) {
  return (
    <Alert
      type="error"
      showIcon
      message={message}
      description={description}
      style={style}
      action={
        onRetry ? (
          <Button size="small" onClick={onRetry}>
            {retryLabel}
          </Button>
        ) : undefined
      }
    />
  );
}

export function AdminEmptyState({
  description,
  extra,
}: {
  description: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={description}>
      {extra}
    </Empty>
  );
}

/** Map common lifecycle words to tones without leaking domain enums into every page. */
export function toneFromStatus(status?: string | null): AdminTone {
  if (!status) return 'default';
  const s = status.toUpperCase();
  if (
    [
      'SUCCESS',
      'SUCCEEDED',
      'COMPLETED',
      'RESOLVED',
      'PAID',
      'USED',
      'DONE',
      'ACTIVE',
      'SENT',
      'AVAILABLE',
      'PUBLISHED',
      'CONFIRMED',
      'SOLD',
    ].includes(s)
  ) {
    return 'success';
  }
  if (
    s.includes('FAIL') ||
    s.includes('ERROR') ||
    s.includes('REJECT') ||
    [
      'CANCELLED',
      'EXPIRED',
      'BANNED',
      'DISABLED',
      'MANUAL_REQUIRED',
      'INVALID',
      'UNAVAILABLE',
    ].includes(s)
  ) {
    return 'error';
  }
  if (
    ['MANUAL', 'ACQUIRE_UNKNOWN', 'CANCEL_PENDING', 'OPEN', 'LOW', 'UNCERTAIN', 'SKIPPED'].includes(
      s,
    ) ||
    s.includes('WARN')
  ) {
    return 'warning';
  }
  if (
    s.includes('PENDING') ||
    s.includes('WAIT') ||
    s.includes('RUNNING') ||
    s.includes('PROCESSING') ||
    s.includes('REVIEW') ||
    s.includes('SENDING') ||
    ['FULFILLING', 'LOCKED', 'CREATED', 'REFUNDING', 'UNUSED'].includes(s)
  ) {
    return 'processing';
  }
  return 'default';
}

import { userError } from '../lib/user-error';
import { useEffect, useState } from 'react';

/** Storefront error text only — never show request / trace ids to buyers. */
export function UserFacingError({
  err,
  fallback = '操作失败',
  className,
}: {
  err: unknown;
  fallback?: string;
  className?: string;
}) {
  const [, refresh] = useState(0);
  const retryAt = err && typeof err === 'object' && 'retryAt' in err && typeof err.retryAt === 'number'
    ? err.retryAt : 0;
  useEffect(() => {
    if (retryAt <= Date.now()) return;
    const timer = setInterval(() => refresh(value => value + 1), 1000);
    const finish = setTimeout(() => clearInterval(timer), retryAt - Date.now() + 1000);
    return () => { clearInterval(timer); clearTimeout(finish); };
  }, [retryAt]);
  const message = userError(err, 'zh', 'UNKNOWN_ERROR') || fallback;

  return (
    <div className={className}>
      <p className="m-0 whitespace-pre-wrap text-sm text-[var(--color-danger)]">{message}</p>
    </div>
  );
}

/** @deprecated Use UserFacingError — kept so older imports keep compiling. */
export const ErrorWithRequestId = UserFacingError;

import { useCallback, useEffect, useRef, useState } from 'react';
import { adminApi } from '../../lib/admin-api';
import { invitationErrorMessage } from '../../lib/invitation-api';

/** These retained APIs use SmsInvit's envelope instead of native responses. */
export function useInvitationAdminQuery<T>(path: string, interval = 5000) {
  const [data, setData] = useState<T>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const controller = useRef<AbortController>();
  const reload = useCallback(async () => {
    controller.current?.abort();
    const pending = new AbortController();
    controller.current = pending;
    const current = ++generation.current;
    setLoading(true);
    try {
      const response = await adminApi<T>(path, { signal: pending.signal });
      if (current === generation.current && !pending.signal.aborted) {
        setData(response.data);
        setError('');
      }
    } catch (failure) {
      if (current === generation.current && !pending.signal.aborted)
        setError(invitationErrorMessage(failure));
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    setData(undefined);
    setError('');
    let stopped = false;
    let tickVersion = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const version = ++tickVersion;
      if (stopped || document.hidden) return;
      await reload();
      if (!stopped && !document.hidden && version === tickVersion && interval > 0)
        timer = setTimeout(() => void tick(), interval);
    };
    const visible = () => {
      tickVersion++;
      clearTimeout(timer);
      if (document.hidden) controller.current?.abort();
      else void tick();
    };
    void tick();
    document.addEventListener('visibilitychange', visible);
    return () => {
      stopped = true;
      tickVersion++;
      generation.current++;
      clearTimeout(timer);
      controller.current?.abort();
      document.removeEventListener('visibilitychange', visible);
    };
  }, [reload, interval]);
  return { data, loading, error, reload };
}

export function invitationDuration(seconds: number | null | undefined) {
  if (seconds == null) return '—';
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
  return `${Math.floor(value / 3600)} 时 ${Math.floor((value % 3600) / 60)} 分`;
}

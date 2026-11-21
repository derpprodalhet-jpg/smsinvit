import { useCallback, useEffect, useRef, useState } from 'react';
import { invitationApi, invitationErrorMessage } from './invitation-api';

/** Read-only polling pauses in background tabs and never replays a mutation. */
export function useInvitationQuery<T = any>(path: string | null, interval = 5000) {
  const [data, setData] = useState<T>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  /** 最近一次成功刷新的时间戳（毫秒）。查询失败时旧数据仍在，必须能看出它有多旧。 */
  const [lastLoadedAt, setLastLoadedAt] = useState<number>();
  const generation = useRef(0);
  const controller = useRef<AbortController>();
  const reload = useCallback(async () => {
    if (!path) return;
    controller.current?.abort();
    const pending = new AbortController();
    controller.current = pending;
    const current = ++generation.current;
    setLoading(true);
    try {
      const next = await invitationApi<T>(path, { signal: pending.signal });
      if (current === generation.current && !pending.signal.aborted) {
        setData(next);
        setError('');
        setLastLoadedAt(Date.now());
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
    setLastLoadedAt(undefined);
    setLoading(false);
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
  return { data, setData, loading, error, reload, lastLoadedAt };
}

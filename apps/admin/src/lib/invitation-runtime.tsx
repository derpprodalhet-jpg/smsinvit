import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useAdminAuth } from './admin-auth-context';
import { useInvitationQuery } from './use-invitation-query';
import { resetRegistrationSecrets } from '../pages/invitation/register/preferences';

type RunResponse = { run?: any; text?: string; truncated?: boolean; events?: any[] };
type Runtime = {
  runId: string | null;
  watchRun: (id: string) => void;
  clearRun: () => void;
  run: any;
  log: string;
  error: string;
  running: boolean;
  refresh: () => Promise<void>;
  events: any[];
  truncated: boolean;
};
const Context = createContext<Runtime | null>(null);
export function InvitationRuntimeProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAdminAuth();
  const [runId, setRunId] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);
  const query = useInvitationQuery<RunResponse>(
    user && runId ? `/runs/${encodeURIComponent(runId)}/log?tail_kb=128` : null,
    finished ? 0 : 2000,
  );
  const watchRun = useCallback((id: string) => {
    setFinished(false);
    setRunId(id);
  }, []);
  const clearRun = useCallback(() => {
    setRunId(null);
    setFinished(false);
  }, []);
  useEffect(() => {
    if (!user) {
      clearRun();
      resetRegistrationSecrets();
    }
  }, [user, clearRun]);
  useEffect(() => {
    if (query.data?.run && ['done', 'failed', 'void', 'cancelled'].includes(query.data.run.status))
      setFinished(true);
  }, [query.data]);
  const value = useMemo(
    () => ({
      runId,
      watchRun,
      clearRun,
      run: query.data?.run,
      log: query.data?.text || '',
      error: query.error,
      running: Boolean(runId && !finished),
      refresh: query.reload,
      events: query.data?.events || [],
      truncated: Boolean(query.data?.truncated),
    }),
    [runId, watchRun, clearRun, query.data, query.error, query.reload, finished],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function useInvitationRuntime(): Runtime {
  const runtime = useContext(Context);
  if (!runtime) throw new Error('邀请任务状态尚未初始化。');
  return runtime;
}

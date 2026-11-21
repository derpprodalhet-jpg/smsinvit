import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiClientError, apiFetch, pollRetryDelay } from '../lib/api';
import {
  hasInvitationAction,
  INVITATION_BATCH_LIMIT,
  invalidInvitationCapability,
  invitationCapabilities,
  invitationPollDelay,
  invitationRow,
  invitationStatusLabel,
  mergeInvitationRows,
  parseInvitationCodes,
  type InvitationBatch,
  type InvitationError,
  type InvitationItem,
  type InvitationRow,
} from '../lib/invitation';
import {
  createInvitationAttempt,
  INVITATION_RECOVERY_LIMIT,
  INVITATION_RECOVERY_TTL,
  readInvitationRecovery,
  validInvitationSession,
  writeInvitationRecovery,
  type StoredInvitationAttempt,
  type StoredInvitationSession,
} from '../lib/invitation-recovery';
import { useDialogFocus } from '../lib/dialog-focus';
import { UserFacingError } from './error-with-request-id';
import { NumericProgress } from './numeric-progress';

type LineError = { line: number; error: ApiClientError };
type PageContent = {
  canRedeem: boolean;
  canStart: boolean;
  unavailableMessage?: string;
  poolAvailable?: number;
  /** 后端下发的流程说明（"兑换后把邮箱交给邀请方…"）。以前拿到就丢了。 */
  description?: string;
};
type AttemptNotice = { id: string; status: 'pending' | 'review'; message: string };
/** 批量开始/确认邀请的结果：逐行错误 + 成功失败计数。 */
type BatchOutcome = { action: 'START' | 'CONFIRM_INVITE'; succeeded: number; failures: { email: string; error: ApiClientError }[] };
type PoolSubmission = { codes: string[]; lines: number[] };
type PoolPrompt = PoolSubmission & { available: number; requested: number };
/**
 * 后端下发的"不可用原因"只允许这几句公开文案；其它一律不展示。
 *
 * 服务端本来就会把它们改写成通用文案，但页面绝不能把任意服务端字符串直接当
 * 用户可见文本 —— 一个内部原因（"执行器正在维护"之类）就会漏到前台。
 */
const PUBLIC_AVAILABILITY_REASONS = new Set([
  '请稍候重试。',
  '暂时无法开始，请稍候再试。',
  '当前可用账号不足，请稍后重试。',
]);
function safeAvailabilityReason(value?: string): string {
  const text = (value || '').trim();
  return PUBLIC_AVAILABILITY_REASONS.has(text) ? text : '';
}
function publicError(error: InvitationError): ApiClientError {
  return new ApiClientError(error.code, error.message, error.requestId);
}
const customerErrorLabels: Record<string, string> = {
  INVITATION_CDK_NOT_FOUND: '卡密无效或不存在，请检查后再试。',
  INVITATION_CDK_USED: '已领取过的卡密，请稍后再次输入查看。',
  INVITATION_CDK_DISABLED: '这份卡密已停用，请联系客服。',
  INVITATION_CDK_EXPIRED: '这份卡密已过期，请联系客服。',
  // 这几类要给出**下一步**，不能都糊成"请稍候重试"：
  // 库存不足是"少提交几条或稍后"；连接/配置是"稍后重试"；执行不可用是"等恢复"。
  INVITATION_MAIL_INSUFFICIENT: '当前可用账号不足，请减少提交数量或稍后重试。',
  INVITATION_MAIL_CONFIGURATION: '邮箱服务配置异常，请稍后重试或联系客服。',
  INVITATION_MAIL_CONNECTION: '邮箱服务暂时连接不上，请稍后重试。',
  INVITATION_MAIL_UNAVAILABLE: '邮箱服务暂时不可用，请稍后重试。',
  INVITATION_POOL_INSUFFICIENT: '可用账号不足，请稍候重试。',
  INVITATION_BUSY: '当前任务较多，请稍后重试。',
  INVITATION_START_DENIED: '这条记录现在无法开始，请刷新页面后重试。',
  SERVICE_UNAVAILABLE: '服务暂时不可用，请稍后重试。',
  INVITATION_INVALID_STATE: '当前记录暂时无法进行此操作，请稍后再试。',
  INVITATION_REVIEW_REQUIRED: '结果待确认，请联系客服。',
  NETWORK_ERROR: '网络连接失败，请稍后再试。',
  VALIDATION_ERROR: '请检查卡密后再试。',
};
const localCustomerErrors = new Set([
  `每次最多兑换 ${INVITATION_BATCH_LIMIT} 份 CDK，请减少数量后再提交。`,
  '本页领取记录较多，请在新的标签页输入卡密查看。',
  '卡密格式或长度不正确，请检查这一行。',
  '复制失败，请选择邮箱后手动复制',
]);
function customerError(error: unknown): Error {
  if (error instanceof ApiClientError && error.httpStatus === 429) return error;
  const message = error instanceof Error ? error.message : '';
  if (localCustomerErrors.has(message)) return new Error(message);
  if (error instanceof ApiClientError && invalidInvitationCapability({ code: error.code }))
    return new Error('这条领取记录暂时无法使用，请联系客服。');
  return new Error(
    error instanceof ApiClientError && customerErrorLabels[error.code]
      ? customerErrorLabels[error.code]
      : '请稍候重试。',
  );
}
const primaryButton =
  'rounded-xl bg-[var(--color-brand)] px-5 py-3 text-sm font-semibold text-[var(--color-ink)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-ink)] disabled:opacity-50';
const secondaryButton =
  'rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2 text-sm text-[var(--color-ink)] hover:bg-[var(--color-canvas)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-ink)] disabled:opacity-50';

export function InvitationRedeemPanel() {
  const [codeInput, setCodeInput] = useState('');
  const [rows, setRows] = useState<InvitationRow[]>([]);
  const [content, setContent] = useState<PageContent | null>(null);
  const [loadingMetadata, setLoadingMetadata] = useState(true);
  const [initializationError, setInitializationError] = useState<unknown>(null);
  const [storageNotice, setStorageNotice] = useState('');
  const [attemptNotices, setAttemptNotices] = useState<AttemptNotice[]>([]);
  const [restoringIds, setRestoringIds] = useState<Set<string>>(new Set());
  const [redeeming, setRedeeming] = useState(false);
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirmingIds, setConfirmingIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<unknown>(null);
  const [lineErrors, setLineErrors] = useState<LineError[]>([]);
  const [duplicates, setDuplicates] = useState<{ line: number; firstLine: number }[]>([]);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [visible, setVisible] = useState(!document.hidden);
  const [pollEpoch, setPollEpoch] = useState(0);
  const [querying, setQuerying] = useState(false);
  const [poolPrompt, setPoolPrompt] = useState<PoolPrompt | null>(null);
  const [batchOutcome, setBatchOutcome] = useState<BatchOutcome | null>(null);
  const [redeemRetryAt, setRedeemRetryAt] = useState(0);
  const poolPromptRef = useRef<HTMLDivElement>(null);
  const closePoolPrompt = useCallback(() => setPoolPrompt(null), []);
  useDialogFocus(!!poolPrompt, poolPromptRef, closePoolPrompt);
  const secretsRef = useRef(new Map<string, string>());
  const storedSessionsRef = useRef(new Map<string, StoredInvitationSession>());
  const attemptsRef = useRef(new Map<string, StoredInvitationAttempt>());
  const activeAttemptIdsRef = useRef(new Set<string>());
  const confirmedReceiptsRef = useRef(new Set<string>());
  const capExpirationsRef = useRef(new Map<string, number>());
  const attemptNoticesRef = useRef(new Map<string, AttemptNotice>());
  const rowsRef = useRef(rows);
  const contentRef = useRef<PageContent | null>(null);
  const restoringRef = useRef(new Set<string>());
  const pendingRef = useRef(new Set<string>());
  const redeemingRef = useRef(false);
  const redeemAttemptRef = useRef<string | null>(null);
  const poolSubmissionRef = useRef<(PoolSubmission & { attemptId: string }) | null>(null);
  const poolRoundsRef = useRef(0);
  const metadataControllerRef = useRef<AbortController | null>(null);
  const controllersRef = useRef(new Set<AbortController>());
  const statusControllerRef = useRef<AbortController | null>(null);
  const mutationsRef = useRef(new Map<string, 'START' | 'CONFIRM_INVITE'>());
  const epochsRef = useRef(new Map<string, number>());
  const mountedRef = useRef(true);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  rowsRef.current = rows;

  const updateRows = useCallback((next: InvitationRow[]) => {
    rowsRef.current = next;
    setRows(next);
  }, []);
  const updatePending = useCallback((next: Set<string>) => {
    pendingRef.current = next;
    setPendingIds(next);
  }, []);
  const updateAvailability = useCallback((next: InvitationBatch) => {
    if (typeof next.canRedeem !== 'boolean' || typeof next.canStart !== 'boolean') return;
    const value = {
      canRedeem: next.canRedeem,
      canStart: next.canStart,
      unavailableMessage: next.unavailableMessage,
      ...(Number.isInteger(next.poolAvailable) ? { poolAvailable: next.poolAvailable } : {}),
    };
    contentRef.current = value;
    setContent(value);
  }, []);
  const persist = useCallback((requiredAttempts: StoredInvitationAttempt[] = []) => {
    try {
      writeInvitationRecovery(
        window.sessionStorage,
        {
          version: 1,
          sessions: [...storedSessionsRef.current.values()],
          attempts: [...attemptsRef.current.values()],
        },
        Date.now(),
        requiredAttempts,
      );
      return true;
    } catch {
      if (mountedRef.current)
        setStorageNotice(
          '当前浏览器无法保存领取记录，请允许网站保存数据后再试。请保留当前页面；刷新后可重新输入卡密查看，已有邮箱仍可复制。',
        );
      return false;
    }
  }, []);
  const updateAttemptNotice = useCallback((notice: AttemptNotice) => {
    attemptNoticesRef.current.set(notice.id, notice);
    setAttemptNotices([...attemptNoticesRef.current.values()]);
  }, []);
  const request = useCallback(
    async <T,>(
      path: string,
      body?: unknown,
      controller = new AbortController(),
      secret?: string,
    ) => {
      const transport = new AbortController();
      const abort = () => transport.abort();
      controller.signal.addEventListener('abort', abort, { once: true });
      if (controller.signal.aborted) transport.abort();
      let timedOut = false;
      const timeout = setTimeout(
        () => {
          timedOut = true;
          transport.abort();
        },
        body === undefined || path.endsWith('/status') ? 15000 : 60000,
      );
      controllersRef.current.add(transport);
      try {
        return await apiFetch<T>(path, {
          method: body === undefined ? 'GET' : 'POST',
          headers: {
            ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
            ...(secret ? { 'X-Session-Secret': secret } : {}),
          },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
          signal: transport.signal,
        });
      } catch (err) {
        if (timedOut)
          throw new ApiClientError('NETWORK_ERROR', '操作超时，正在确认原结果，请勿重复提交。');
        throw err;
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener('abort', abort);
        controllersRef.current.delete(transport);
      }
    },
    [],
  );
  const acceptUpdates = useCallback(
    (items: InvitationItem[]) => {
      const pending = new Set(pendingRef.current);
      const restoring = new Set(restoringRef.current);
      for (const item of items) {
        if (!item.id) continue;
        if (invalidInvitationCapability(item.error)) {
          secretsRef.current.delete(item.id);
          storedSessionsRef.current.delete(item.id);
          capExpirationsRef.current.delete(item.id);
        }
        if (!item.error || invalidInvitationCapability(item.error)) {
          pending.delete(item.id);
          restoring.delete(item.id);
        }
      }
      restoringRef.current = restoring;
      setRestoringIds(restoring);
      updatePending(pending);
      updateRows(mergeInvitationRows(rowsRef.current, items));
      persist();
    },
    [persist, updatePending, updateRows],
  );
  const clearInvalidCapabilities = useCallback(
    (err: unknown, ids: string[]) => {
      if (!(err instanceof ApiClientError) || !invalidInvitationCapability({ code: err.code }))
        return;
      acceptUpdates(ids.map((id) => ({ id, error: { code: err.code, message: err.message } })));
    },
    [acceptUpdates],
  );

  const acceptRedemption = useCallback(
    (
      data: InvitationBatch,
      attempt: StoredInvitationAttempt,
      recoveryQuery = false,
      readEpochs?: ReadonlyMap<string, number>,
    ) => {
      if (
        !data ||
        !Array.isArray(data.items) ||
        (data.attemptId && data.attemptId !== attempt.id)
      ) {
        throw new ApiClientError(
          'SERVICE_UNAVAILABLE',
          '领取结果暂时无法核对，请查询结果，勿重复领取。',
        );
      }
      confirmedReceiptsRef.current.delete(attempt.id);
      updateAvailability(data);
      if (data.attemptStatus !== undefined) {
        const ids = new Set<string>(),
          lines = new Set<number>();
        const malformed =
          !['completed', 'rejected', 'pending', 'review'].includes(data.attemptStatus) ||
          data.items.some((item) => {
            if (
              !Number.isInteger(item.line) ||
              item.line! < 1 ||
              item.line! > attempt.lines.length ||
              lines.has(item.line!)
            )
              return true;
            lines.add(item.line!);
            if (item.id && ids.has(item.id)) return true;
            if (item.id) ids.add(item.id);
            return false;
          });
        if (malformed) {
          updateAttemptNotice({
            id: attempt.id,
            status: 'review',
            message: '领取结果暂时无法核对，请联系客服，勿重复领取。',
          });
          return;
        }
      }
      if (data.attemptStatus === 'pending' || data.attemptStatus === 'review') {
        updateAttemptNotice({
          id: attempt.id,
          status: data.attemptStatus,
          message:
            data.attemptStatus === 'review'
              ? '领取结果待确认，请联系客服，勿重复领取。'
              : '正在确认领取结果，请勿重复领取。',
        });
        return;
      }
      const failures = data.items.filter((item) => item.error);
      // The backend either allocates the whole batch or allocates none of it.
      if (!recoveryQuery && failures.length && data.items.some((item) => !item.error)) {
        updateAttemptNotice({
          id: attempt.id,
          status: 'review',
          message: '领取结果不完整，请联系客服，勿重复领取。',
        });
        return;
      }
      // A short account pool binds nothing; offer a smaller batch before any retry.
      if (
        failures.length &&
        failures.every((item) => item.error?.code === 'INVITATION_POOL_INSUFFICIENT')
      ) {
        const available = Number.isInteger(data.poolAvailable) ? data.poolAvailable! : 0;
        const record = poolSubmissionRef.current;
        const submission = record && record.attemptId === attempt.id ? record : null;
        poolRoundsRef.current += 1;
        if (
          available > 0 &&
          poolRoundsRef.current <= 3 &&
          submission &&
          submission.codes.length > available
        ) {
          attemptsRef.current.delete(attempt.id);
          activeAttemptIdsRef.current.delete(attempt.id);
          attemptNoticesRef.current.delete(attempt.id);
          setAttemptNotices([...attemptNoticesRef.current.values()]);
          persist();
          setError(null);
          setLineErrors([]);
          setPoolPrompt({
            codes: submission.codes,
            lines: submission.lines,
            available,
            requested:
              Number.isInteger(data.poolRequested) && data.poolRequested! > 0
                ? data.poolRequested!
                : submission.lines.length,
          });
          return;
        }
      }
      const nextSessions: StoredInvitationSession[] = [];
      const knownExpirations = new Map(
        attempt.knownSessionExpirations?.map(({ id, expiresAt }) => [id, expiresAt]),
      );
      const restoreIds = attempt.restoreIds ? new Set(attempt.restoreIds) : null;
      const recoverableItems = data.items.filter((item) => {
        if (!recoveryQuery || !item.id || secretsRef.current.has(item.id)) return true;
        if (restoreIds && !restoreIds.has(item.id)) return false;
        return !knownExpirations.has(item.id) || knownExpirations.get(item.id)! > Date.now();
      });
      if (data.attemptStatus !== 'rejected' && (!failures.length || recoveryQuery)) {
        for (const item of recoverableItems.filter((item) => !item.error)) {
          const record = {
            id: item.id || '',
            sessionSecret: item.sessionSecret || '',
            line: attempt.lines[(item.line ?? 1) - 1] ?? 1,
            createdAt: attempt.createdAt,
            expiresAt: attempt.expiresAt,
          };
          if (!invitationRow(item) || !validInvitationSession(record)) {
            updateAttemptNotice({
              id: attempt.id,
              status: 'review',
              message: '领取结果暂时无法核对，请勿重复领取。',
            });
            return;
          }
          const stored = storedSessionsRef.current.get(record.id);
          const currentExpiry = capExpirationsRef.current.get(record.id);
          const originalExpiry =
            (stored && validInvitationSession(stored) ? stored.expiresAt : undefined) ??
            (recoveryQuery && currentExpiry && currentExpiry > Date.now()
              ? currentExpiry
              : undefined) ??
            knownExpirations.get(record.id) ??
            (recoveryQuery ? currentExpiry : undefined);
          if (originalExpiry !== undefined) {
            record.expiresAt = originalExpiry;
            record.createdAt = originalExpiry - INVITATION_RECOVERY_TTL;
          }
          nextSessions.push(record);
        }
        if (
          !nextSessions.length &&
          !failures.length &&
          (!data.items.length || recoverableItems.length)
        ) {
          updateAttemptNotice({
            id: attempt.id,
            status: 'review',
            message: '领取结果暂时无法确认，请勿重复领取。',
          });
          return;
        }
      }
      const currentItems = recoverableItems.filter(
        (item) =>
          !item.id ||
          !readEpochs?.has(item.id) ||
          (readEpochs.get(item.id) === (epochsRef.current.get(item.id) || 0) &&
            !mutationsRef.current.has(item.id)),
      );
      for (const [id, session] of storedSessionsRef.current) {
        if (!validInvitationSession(session)) storedSessionsRef.current.delete(id);
      }
      let recoveryOverflow = false;
      const overflowIds: string[] = [];
      nextSessions.forEach((session) => {
        secretsRef.current.set(session.id, session.sessionSecret);
        capExpirationsRef.current.set(
          session.id,
          Math.max(capExpirationsRef.current.get(session.id) || 0, session.expiresAt),
        );
        if (!validInvitationSession(session)) return;
        if (!storedSessionsRef.current.has(session.id)) {
          if (storedSessionsRef.current.size < INVITATION_RECOVERY_LIMIT)
            storedSessionsRef.current.set(session.id, session);
          else {
            recoveryOverflow = true;
            overflowIds.push(session.id);
          }
        }
      });
      updateRows(mergeInvitationRows(rowsRef.current, currentItems));
      if (recoveryQuery && failures.length)
        acceptUpdates(currentItems.filter((item) => item.error));
      setSelectedIds(
        (previous) =>
          new Set([
            ...previous,
            ...currentItems
              .filter((item) => item.id && item.allowedActions?.includes('START'))
              .map((item) => item.id!),
          ]),
      );
      setLineErrors(
        failures.map((item) => ({
          line: attempt.lines[(item.line ?? 1) - 1] ?? item.line ?? 1,
          error: publicError(item.error!),
        })),
      );
      if (data.attemptStatus === 'rejected' && data.message && !failures.length)
        // 只有在**没有**逐行原因时才补一个顶层提示：否则会同时出现"第 N 行：卡密…"
        // 和"邮箱服务暂时不可用"两条，指向同一件事却互相矛盾，用户不知道该改哪个。
        setError(
          new ApiClientError('INVITATION_MAIL_UNAVAILABLE', '暂时无法获取邮箱，请稍后再试。'),
        );
      const retryResult =
        recoveryQuery &&
        data.attemptStatus === 'completed' &&
        failures.some((item) => !invalidInvitationCapability(item.error));
      if (recoveryOverflow) {
        const overflowExpirations = overflowIds
          .filter((id) => capExpirationsRef.current.get(id) !== attempt.expiresAt)
          .map((id) => ({ id, expiresAt: capExpirationsRef.current.get(id)! }));
        attemptsRef.current.set(attempt.id, {
          ...attempt,
          restoreIds: overflowIds,
          knownSessionExpirations: overflowExpirations.length ? overflowExpirations : undefined,
        });
        if (data.attemptStatus === 'completed') confirmedReceiptsRef.current.add(attempt.id);
        updateAttemptNotice({
          id: attempt.id,
          status: 'review',
          message:
            '本页领取记录较多，已保存本次领取结果。请在新的标签页输入卡密查看，已有邮箱仍可复制。',
        });
      } else if (retryResult)
        updateAttemptNotice({
          id: attempt.id,
          status: 'pending',
          message: '部分邮箱状态暂时无法核对，正在继续确认，请勿重复领取。',
        });
      else {
        attemptsRef.current.delete(attempt.id);
        activeAttemptIdsRef.current.delete(attempt.id);
        attemptNoticesRef.current.delete(attempt.id);
      }
      setAttemptNotices([...attemptNoticesRef.current.values()]);
      persist(recoveryOverflow ? [attemptsRef.current.get(attempt.id)!] : []);
    },
    [acceptUpdates, persist, updateAttemptNotice, updateAvailability, updateRows],
  );

  const loadMetadata = useCallback(async () => {
    if (metadataControllerRef.current || !mountedRef.current) return;
    const controller = new AbortController();
    metadataControllerRef.current = controller;
    setLoadingMetadata(true);
    try {
      const { data } = await request<PageContent>(
        '/api/v1/invitation/page-content',
        undefined,
        controller,
      );
      if (!mountedRef.current || controller.signal.aborted) return;
      if (
        !data ||
        typeof data.canRedeem !== 'boolean' ||
        typeof data.canStart !== 'boolean'
      ) {
        throw new ApiClientError('SERVICE_UNAVAILABLE', '暂时无法获取邮箱，请稍后再试。');
      }
      const { poolAvailable, ...availability } = data;
      contentRef.current = {
        ...availability,
        ...(Number.isInteger(poolAvailable) ? { poolAvailable } : {}),
      };
      setContent(contentRef.current);
      setInitializationError(null);
    } catch (err) {
      if (mountedRef.current && !controller.signal.aborted) {
        contentRef.current = null;
        setContent(null);
        setInitializationError(err);
      }
    } finally {
      if (metadataControllerRef.current === controller) metadataControllerRef.current = null;
      if (mountedRef.current) setLoadingMetadata(false);
    }
  }, [request]);

  const pollStatus = useCallback(
    async (includeReady = false) => {
      if (document.hidden || !mountedRef.current || statusControllerRef.current) return;
      if (pollRetryDelay('invitation') > 0) { setPollEpoch(value => value + 1); return; }
      if (!rowsRef.current.length && !activeAttemptIdsRef.current.size) return;
      const controller = new AbortController();
      statusControllerRef.current = controller;
      setQuerying(true);
      try {
        for (const attempt of [...attemptsRef.current.values()]) {
          if (
            !activeAttemptIdsRef.current.has(attempt.id) ||
            attempt.id === redeemAttemptRef.current ||
            (!includeReady && attemptNoticesRef.current.get(attempt.id)?.status === 'review')
          )
            continue;
          try {
            const readEpochs = new Map(
              [...secretsRef.current.keys()].map((id) => [id, epochsRef.current.get(id) || 0]),
            );
            const { data } = await request<InvitationBatch>(
              `/api/v1/invitation/redemption-attempts/${attempt.id}`,
              undefined,
              controller,
              attempt.recoverySecret,
            );
            if (!mountedRef.current || controller.signal.aborted) return;
            acceptRedemption(data, attempt, true, readEpochs);
          } catch (err) {
            if (!mountedRef.current || controller.signal.aborted) return;
            const missing =
              err instanceof ApiClientError && err.code === 'INVITATION_ATTEMPT_NOT_FOUND';
            updateAttemptNotice({
              id: attempt.id,
              status: missing ? 'review' : 'pending',
              message: missing
                ? '领取结果仍无法确认，请勿重复领取；请稍后再试或联系客服。'
                : '暂时无法确认领取结果，请勿重复领取。',
            });
            setError(err);
            if (pollRetryDelay('invitation') > 0) return;
          }
        }
        const eligible = rowsRef.current.filter(
          (row) =>
            !mutationsRef.current.has(row.id) &&
            (includeReady ||
              (!hasInvitationAction(row, 'START') && !hasInvitationAction(row, 'CONFIRM_INVITE')) ||
              pendingRef.current.has(row.id)),
        );
        const ids = new Set(eligible.map((row) => row.id));
        restoringRef.current.forEach((id) => {
          if (!mutationsRef.current.has(id)) ids.add(id);
        });
        const sessions = [...secretsRef.current]
          .filter(([id]) => ids.has(id))
          .map(([id, sessionSecret]) => ({ id, sessionSecret }));
        for (let offset = 0; offset < sessions.length; offset += INVITATION_BATCH_LIMIT) {
          const chunk = sessions.slice(offset, offset + INVITATION_BATCH_LIMIT);
          const epochs = new Map(chunk.map(({ id }) => [id, epochsRef.current.get(id) || 0]));
          try {
            const { data } = await request<InvitationBatch>(
              '/api/v1/invitation/status',
              { sessions: chunk },
              controller,
            );
            if (!mountedRef.current || controller.signal.aborted) return;
            const current = data.items.filter(
              (item) =>
                item.id &&
                epochs.has(item.id) &&
                epochs.get(item.id) === (epochsRef.current.get(item.id) || 0) &&
                !mutationsRef.current.has(item.id),
            );
            acceptUpdates(current);
            updateAvailability(data);
            const itemError = current.find((item) => item.error)?.error;
            setError(itemError ? publicError(itemError) : null);
          } catch (err) {
            if (!mountedRef.current || controller.signal.aborted) return;
            clearInvalidCapabilities(
              err,
              chunk.map(({ id }) => id),
            );
            setError(err);
            if (pollRetryDelay('invitation') > 0) return;
          }
        }
      } finally {
        if (statusControllerRef.current === controller) {
          statusControllerRef.current = null;
          if (mountedRef.current) setQuerying(false);
        }
        if (mountedRef.current && !controller.signal.aborted) setPollEpoch((value) => value + 1);
      }
    },
    [
      acceptRedemption,
      acceptUpdates,
      clearInvalidCapabilities,
      request,
      updateAttemptNotice,
      updateAvailability,
    ],
  );

  useEffect(() => {
    if (!redeemRetryAt) return;
    const timer = setTimeout(() => setRedeemRetryAt(0), Math.max(0, redeemRetryAt - Date.now()));
    return () => clearTimeout(timer);
  }, [redeemRetryAt]);

  useEffect(() => {
    mountedRef.current = true;
    try {
      const { recovery, notice } = readInvitationRecovery(window.sessionStorage);
      setStorageNotice(notice);
      recovery.sessions.forEach((session) => {
        storedSessionsRef.current.set(session.id, session);
        capExpirationsRef.current.set(session.id, session.expiresAt);
      });
      recovery.attempts.forEach((attempt) => {
        attemptsRef.current.set(attempt.id, attempt);
        const knownExpirations = new Map(
          attempt.knownSessionExpirations?.map(({ id, expiresAt }) => [id, expiresAt]),
        );
        for (const id of attempt.restoreIds || []) {
          const expiresAt = knownExpirations.get(id) ?? attempt.expiresAt;
          if (expiresAt <= Date.now()) continue;
          capExpirationsRef.current.set(
            id,
            Math.min(capExpirationsRef.current.get(id) || expiresAt, expiresAt),
          );
        }
      });
    } catch {
      setStorageNotice(
        '当前浏览器无法读取领取记录，请允许网站保存数据后再试。已开始的任务不受影响。',
      );
    }
    void loadMetadata();
    const onVisibility = () => {
      setVisible(!document.hidden);
      if (document.hidden) {
        statusControllerRef.current?.abort();
        statusControllerRef.current = null;
        setQuerying(false);
      } else {
        void pollStatus(true);
        void loadMetadata();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      mountedRef.current = false;
      document.removeEventListener('visibilitychange', onVisibility);
      controllersRef.current.forEach((active) => active.abort());
      controllersRef.current.clear();
      secretsRef.current.clear();
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    };
  }, [loadMetadata, pollStatus]);

  useEffect(() => {
    // 服务不可用时页面不会自愈：挂载、切回标签页、再点提交才会重取。这里加一条
    // 低频的**只读**重取，恢复后提示自动更新；它不会开始任何任务。
    if (!visible) return;
    const recovering = !content || !content.canRedeem || !content.canStart;
    if (!recovering) return;
    const timer = setTimeout(() => void loadMetadata(), 30000);
    return () => clearTimeout(timer);
  }, [visible, content, loadMetadata]);

  useEffect(() => {
    if (!visible) return;
    const unresolved =
      attemptNotices.some(({ status }) => status === 'pending') ||
      restoringIds.size > 0 ||
      pendingIds.size > 0;
    const delay = unresolved ? 2000 : invitationPollDelay(rows, pendingIds);
    if (delay === null) return;
    const timer = setTimeout(
      () => void pollStatus(),
      Math.max(delay, Math.ceil(secretsRef.current.size / INVITATION_BATCH_LIMIT) * 1000),
    );
    return () => clearTimeout(timer);
  }, [rows, pendingIds, restoringIds, visible, pollEpoch, pollStatus, attemptNotices]);

  async function onRedeem(event: FormEvent) {
    event.preventDefault();
    if (redeemRetryAt > Date.now()) return;
    if (redeemingRef.current || metadataControllerRef.current || statusControllerRef.current)
      return;
    if (
      [...activeAttemptIdsRef.current].some(
        (id) => attemptsRef.current.has(id) && !confirmedReceiptsRef.current.has(id),
      )
    ) {
      await pollStatus(true);
      return;
    }
    const { codes, lines, count, duplicates: repeated } = parseInvitationCodes(codeInput);
    if (!codes.length) return;
    if (!contentRef.current) {
      redeemingRef.current = true;
      setRedeeming(true);
      await loadMetadata();
      redeemingRef.current = false;
      if (!mountedRef.current) return;
      setRedeeming(false);
      if (!contentRef.current) {
        setError(new ApiClientError('SERVICE_UNAVAILABLE', '暂时无法获取邮箱，请稍后再试。'));
        return;
      }
    }
    if (count > INVITATION_BATCH_LIMIT) {
      setError(
        new ApiClientError(
          'VALIDATION_ERROR',
          `每次最多兑换 ${INVITATION_BATCH_LIMIT} 份 CDK，请减少数量后再提交。`,
        ),
      );
      return;
    }
    const invalid = codes.flatMap((code, index) =>
      code.length > 160 || code.startsWith('IV2$')
        ? [
            {
              line: lines[index],
              error: new ApiClientError('VALIDATION_ERROR', '卡密格式或长度不正确，请检查这一行。'),
            },
          ]
        : [],
    );
    if (invalid.length) {
      setLineErrors(invalid);
      return;
    }
    if (
      [...attemptsRef.current.values()].filter(({ expiresAt }) => expiresAt > Date.now()).length >=
      50
    ) {
      setError(
        new ApiClientError('VALIDATION_ERROR', '本页领取记录较多，请在新的标签页输入卡密查看。'),
      );
      return;
    }
    poolRoundsRef.current = 0;
    const poolAvailable = contentRef.current?.poolAvailable;
    // 库存只用来提示，不用来拦截：已绑定的旧卡密查询根本不需要库存，而后端对
    // "需要新账号"的那部分会给出准确的 INVITATION_MAIL_INSUFFICIENT。以前这里在
    // 区分新旧卡密之前就按 0 直接拒绝，连"重新输入原 CDK 查原邮箱"都被挡住。
    if (poolAvailable !== undefined && poolAvailable > 0 && codes.length > poolAvailable) {
      setPoolPrompt({ codes, lines, available: poolAvailable, requested: codes.length });
      return;
    }
    await submitCodes(codes, lines, repeated);
  }

  async function submitCodes(
    codes: string[],
    lines: number[],
    repeated: { line: number; firstLine: number }[] = [],
  ) {
    let attempt: StoredInvitationAttempt;
    try {
      attempt = createInvitationAttempt(lines);
      const knownSessionExpirations = [...capExpirationsRef.current]
        .filter(([, expiresAt]) => expiresAt > attempt.createdAt)
        .map(([id, expiresAt]) => ({ id, expiresAt }));
      if (knownSessionExpirations.length) attempt.knownSessionExpirations = knownSessionExpirations;
    } catch (err) {
      setError(err);
      return;
    }
    attemptsRef.current.set(attempt.id, attempt);
    if (!persist([attempt])) {
      attemptsRef.current.delete(attempt.id);
      return;
    }
    activeAttemptIdsRef.current.add(attempt.id);
    poolSubmissionRef.current = { attemptId: attempt.id, codes, lines };
    redeemingRef.current = true;
    redeemAttemptRef.current = attempt.id;
    setRedeeming(true);
    setError(null);
    setLineErrors([]);
    setDuplicates(repeated);
    updateAttemptNotice({
      id: attempt.id,
      status: 'pending',
      message: '正在领取邮箱，请勿重复提交。',
    });
    const controller = new AbortController();
    const readEpochs = new Map(
      [...secretsRef.current.keys()].map((id) => [id, epochsRef.current.get(id) || 0]),
    );
    try {
      const { data } = await request<InvitationBatch>(
        '/api/v1/invitation/redeem',
        { codes, attemptId: attempt.id, recoverySecret: attempt.recoverySecret },
        controller,
      );
      if (!mountedRef.current || controller.signal.aborted) return;
      acceptRedemption(data, attempt, false, readEpochs);
    } catch (err) {
      if (mountedRef.current && !controller.signal.aborted) {
        setError(err);
        if (
          err instanceof ApiClientError &&
          err.structuredResponse &&
          (['VALIDATION_ERROR', 'CSRF_INVALID'].includes(err.code) ||
            (err.code === 'RATE_LIMIT_EXCEEDED' && err.httpStatus === 429))
        ) {
          // This API refusal runs before receipt creation. Gateway/network failures remain uncertain.
          if (err.httpStatus === 429) setRedeemRetryAt(err.retryAt || Date.now() + (err.retryAfterMs || 30000));
          attemptsRef.current.delete(attempt.id);
          activeAttemptIdsRef.current.delete(attempt.id);
          attemptNoticesRef.current.delete(attempt.id);
          setAttemptNotices([...attemptNoticesRef.current.values()]);
          persist();
        } else
          updateAttemptNotice({
            id: attempt.id,
            status: 'pending',
            message: '领取响应未能确认，正在查询原结果，请勿重复领取。',
          });
      }
    } finally {
      redeemingRef.current = false;
      redeemAttemptRef.current = null;
      if (mountedRef.current) {
        setRedeeming(false);
        if (
          attemptsRef.current.has(attempt.id) &&
          attemptNoticesRef.current.get(attempt.id)?.status !== 'review'
        )
          void pollStatus(true);
      }
    }
  }

  function confirmPoolPrompt() {
    const prompt = poolPrompt;
    if (!prompt) return;
    setPoolPrompt(null);
    const size = Math.min(prompt.available, prompt.codes.length);
    void submitCodes(prompt.codes.slice(0, size), prompt.lines.slice(0, size));
  }

  async function mutate(action: 'START' | 'CONFIRM_INVITE', requestedIds: ReadonlySet<string>) {
    if (action === 'START' && !contentRef.current?.canStart) return;
    const ids = new Set(
      [...requestedIds].filter(
        (id) =>
          !mutationsRef.current.has(id) &&
          !pendingRef.current.has(id) &&
          !restoringRef.current.has(id),
      ),
    );
    const sessions = invitationCapabilities(rowsRef.current, secretsRef.current, action, ids);
    if (!sessions.length) return;
    statusControllerRef.current?.abort();
    statusControllerRef.current = null;
    setQuerying(false);
    const pending = new Set(pendingRef.current);
    sessions.forEach(({ id }) => {
      mutationsRef.current.set(id, action);
      epochsRef.current.set(id, (epochsRef.current.get(id) || 0) + 1);
      pending.add(id);
    });
    updatePending(pending);
    setConfirmingIds(
      new Set(
        [...mutationsRef.current]
          .filter(([, operation]) => operation === 'CONFIRM_INVITE')
          .map(([id]) => id),
      ),
    );
    setError(null);
    const controller = new AbortController();
    try {
      for (let offset = 0; offset < sessions.length; offset += INVITATION_BATCH_LIMIT) {
        const chunk = sessions.slice(offset, offset + INVITATION_BATCH_LIMIT);
        const { data } = await request<InvitationBatch>(
          `/api/v1/invitation/${action === 'START' ? 'start' : 'invite-sent'}`,
          { sessions: chunk },
          controller,
        );
        if (!mountedRef.current || controller.signal.aborted) return;
        acceptUpdates(data.items);
        updateAvailability(data);
        if (action === 'CONFIRM_INVITE')
          setSelectedIds(
            (previous) =>
              new Set([
                ...previous,
                ...data.items
                  .filter(
                    (item) => item.id && !item.error && item.allowedActions?.includes('START'),
                  )
                  .map((item) => item.id!),
              ]),
          );
        // 批量操作以前只把第一条错误抛成全局错误，逐行结果直接丢掉：部分成功时
        // 用户根本看不出哪些成了、哪些没成。这里逐行收集，并给出成功/失败计数。
        const failures = data.items
          .filter((item) => item.error)
          .map((item) => ({ email: item.email || '', error: publicError(item.error!) }));
        const succeeded = data.items.filter((item) => item.id && !item.error).length;
        if (failures.length || (succeeded && data.items.length > 1))
          setBatchOutcome({ action, succeeded, failures });
        if (!failures.length) setError(null);
      }
    } catch (err) {
      if (!mountedRef.current || controller.signal.aborted) return;
      clearInvalidCapabilities(
        err,
        sessions.map(({ id }) => id),
      );
      setError(err);
    } finally {
      sessions.forEach(({ id }) => mutationsRef.current.delete(id));
      if (mountedRef.current) {
        setConfirmingIds(
          new Set(
            [...mutationsRef.current]
              .filter(([, operation]) => operation === 'CONFIRM_INVITE')
              .map(([id]) => id),
          ),
        );
        setPendingIds(new Set(pendingRef.current));
        if (sessions.some(({ id }) => pendingRef.current.has(id))) void pollStatus(true);
      }
    }
  }
  async function copyEmail(row: InvitationRow) {
    try {
      await navigator.clipboard.writeText(row.email);
      if (!mountedRef.current) return;
      setCopiedId(row.id);
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => setCopiedId(null), 1800);
    } catch {
      if (mountedRef.current)
        setError(new ApiClientError('UNKNOWN_ERROR', '复制失败，请选择邮箱后手动复制'));
    }
  }
  const startable = rows.filter(
    (row) =>
      content?.canStart &&
      hasInvitationAction(row, 'START') &&
      secretsRef.current.has(row.id) &&
      !pendingIds.has(row.id) &&
      !restoringIds.has(row.id) &&
      !mutationsRef.current.has(row.id),
  );
  const selected = startable.filter((row) => selectedIds.has(row.id));
  const allSelected = startable.length > 0 && selected.length === startable.length;
  /** 不能开始时，用状态说明取代那个点不动的"开始"按钮。 */
  function unavailableAction(row: InvitationRow): string {
    if (row.status === 'review') return '结果待确认，请联系客服';
    if (row.status === 'done') return '已完成，奖励另行核验';
    if (row.status === 'void') return '该卡密已作废，请联系客服';
    if (!content?.canStart)
      return safeAvailabilityReason(content?.unavailableMessage) || '暂时无法开始，请稍候重试';
    if (hasInvitationAction(row, 'CONFIRM_INVITE')) return '请先确认已发送邀请信';
    return '当前状态无法开始，请刷新后重试';
  }
  return (
    <div className="space-y-6">
      {initializationError ? (
        <p role="alert" className="rounded-xl bg-[var(--color-danger-bg)] p-4 text-sm text-[var(--color-danger)]">
          请稍候重试。
        </p>
      ) : loadingMetadata && !content ? (
        <p role="status">请稍候…</p>
      ) : null}
      {content && !content.canRedeem ? (
        <p role="status" className="rounded-xl bg-[var(--color-warning-bg)] p-4 text-sm text-[var(--color-warning)]">
          请稍候重试。
        </p>
      ) : null}
      {rows.length > 0 && content && !content.canStart ? (
        <p role="status" className="rounded-xl bg-[var(--color-warning-bg)] p-4 text-sm text-[var(--color-warning)]">
          请稍候重试。
        </p>
      ) : null}
      {storageNotice ? (
        <p role="status" className="rounded-xl bg-[var(--color-warning-bg)] p-4 text-sm text-[var(--color-warning)]">
          {storageNotice}
        </p>
      ) : null}
      {restoringIds.size > 0 ? (
        <p role="status" className="text-sm text-[var(--color-muted)]">
          正在确认上次领取的邮箱，请稍候，勿重复领取。
        </p>
      ) : null}
      {attemptNotices.map((notice) => (
        <p
          key={notice.id}
          role="status"
          className="rounded-xl bg-[var(--color-warning-bg)] p-4 text-sm text-[var(--color-warning)]"
        >
          {notice.message}
        </p>
      ))}
      {/* 「当前可用账号」已移到页面标题行右侧（components/redeem-intro.tsx），
          这里不再重复渲染，避免同一个数字出现两处。 */}
      <form onSubmit={(event) => void onRedeem(event)} className="service-panel space-y-3">
        <label htmlFor="invitation-cdk-input" className="block text-base font-semibold">
          输入 CDK
        </label>
        <textarea
          id="invitation-cdk-input"
          value={codeInput}
          onChange={(event) => setCodeInput(event.target.value)}
          placeholder="每行一个 CDK"
          rows={5}
          autoComplete="off"
          spellCheck={false}
          className="block w-full resize-y rounded-xl border-2 border-[var(--color-brand)]/35 bg-[var(--color-card)] px-4 py-3 text-base font-mono text-[var(--color-ink)] outline-none ring-[var(--color-brand)]/20 placeholder:text-[var(--color-muted)] focus:border-[var(--color-brand)] focus:ring-4"
        />
        <p className="text-sm text-[var(--color-muted)]">
          每行一份，每次最多 {INVITATION_BATCH_LIMIT} 份 CDK。
          <br />
          兑换后把邮箱交给邀请方；<strong>重新输入原来的 CDK 可以查回原邮箱和当前进度</strong>
          ，刷新页面不会自动显示历史记录。
          {content?.description ? ` ${content.description}` : ''}
        </p>
        <button
          type="submit"
          className={`${primaryButton} w-full sm:w-auto`}
          disabled={
            redeeming ||
            loadingMetadata ||
            querying ||
            redeemRetryAt > Date.now() ||
            (!codeInput.trim() && !attemptNotices.length)
          }
          aria-busy={redeeming}
        >
          {redeeming ? '获取中…' : '获取邮箱'}
        </button>
      </form>
      {duplicates.length ? (
        <p role="status" className="text-sm text-[var(--color-muted)]">
          {duplicates
            .map(
              ({ line, firstLine }) => `第 ${line} 行与第 ${firstLine} 行重复，已合并为一次领取。`,
            )
            .join(' ')}
        </p>
      ) : null}

      {error ? (
        <div role="alert" className="rounded-xl border border-[var(--color-danger)]/30 bg-[var(--color-danger-bg)] px-4 py-3">
          <UserFacingError err={customerError(error)} />
        </div>
      ) : null}
      {batchOutcome ? (
        <div
          role="status"
          className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] px-4 py-3"
        >
          <p className="text-sm font-semibold text-[var(--color-ink)]">
            {batchOutcome.action === 'START' ? '开始执行' : '确认邀请信'}：
            成功 {batchOutcome.succeeded} 条
            {batchOutcome.failures.length ? `，失败 ${batchOutcome.failures.length} 条` : ''}
          </p>
          {batchOutcome.failures.length ? (
            <ul aria-label="批量操作失败明细" className="mt-2 space-y-1">
              {batchOutcome.failures.map(({ email, error: itemError }, index) => (
                <li key={`${email}-${index}`} className="text-sm text-[var(--color-danger)]">
                  {email ? `${email}：` : ''}
                  <UserFacingError err={customerError(itemError)} />
                </li>
              ))}
            </ul>
          ) : null}
          <button
            type="button"
            className={`${secondaryButton} mt-3`}
            onClick={() => setBatchOutcome(null)}
          >
            关闭提示
          </button>
        </div>
      ) : null}
      {lineErrors.length > 0 ? (
        <ul
          aria-label="CDK 兑换错误"
          className="space-y-2 rounded-xl border border-[var(--color-danger)]/30 bg-[var(--color-danger-bg)] px-4 py-3"
        >
          {lineErrors.map(({ line, error: itemError }, index) => (
            <li key={`${line}-${index}`} className="flex flex-wrap gap-x-2" role="alert">
              <span className="text-sm text-[var(--color-danger)]">第 {line} 行：</span>
              <UserFacingError err={customerError(itemError)} />
            </li>
          ))}
        </ul>
      ) : null}

      {rows.length > 0 ? (
        <section aria-label="邀请邮箱" className="space-y-4">
          <p className="text-sm leading-relaxed text-[var(--color-muted)]">
            请先让邀请方向领取的邮箱发送邀请，确认已发送邀请信后，再点击开始。
            成功表示受邀流程完成，邀请奖励另行核验。
          </p>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-3">
              <label className="inline-flex min-h-11 items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-[var(--color-ink)]"
                  checked={allSelected}
                  disabled={!startable.length}
                  onChange={() =>
                    setSelectedIds(
                      allSelected ? new Set() : new Set(startable.map((row) => row.id)),
                    )
                  }
                />
                全选可开始的{startable.length ? `（${startable.length}）` : ''}
              </label>
              <button
                type="button"
                className={primaryButton}
                disabled={!selected.length}
                onClick={() => void mutate('START', new Set(selected.map((row) => row.id)))}
              >
                开始选中{selected.length > 0 ? `（${selected.length}）` : ''}
              </button>
            </div>
          </div>
          <ul className="space-y-3">
            {rows.map((row) => {
              const canStart = startable.some((item) => item.id === row.id);
              const pending = pendingIds.has(row.id);
              const showProgress = ['processing', 'done', 'failed'].includes(row.status);
              return (
                <li
                  key={row.id}
                  className="space-y-3 rounded-2xl border border-[var(--color-border)] bg-[var(--color-card)] p-4 shadow-[var(--shadow-card)] sm:p-5"
                >
                  <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-3 sm:flex sm:flex-wrap">
                    <input
                      type="checkbox"
                      className="h-4 w-4 shrink-0 accent-[var(--color-ink)]"
                      aria-label={`选择 ${row.email}`}
                      checked={canStart && selectedIds.has(row.id)}
                      disabled={!canStart}
                      onChange={() =>
                        setSelectedIds((previous) => {
                          const next = new Set(previous);
                          if (next.has(row.id)) next.delete(row.id);
                          else next.add(row.id);
                          return next;
                        })
                      }
                    />
                    <span className="min-w-0 flex-1 break-all font-mono text-base select-all">
                      {row.email}
                    </span>
                    {row.status !== 'processing' && <span className="col-start-2 text-sm text-[var(--color-muted)]">
                      {invitationStatusLabel(row)}
                    </span>}
                    <div className="col-start-2 flex items-center justify-self-end gap-3 sm:ml-auto">
                      <button
                        type="button"
                        className={secondaryButton}
                        aria-label={`复制 ${row.email}`}
                        onClick={() => void copyEmail(row)}
                      >
                        {copiedId === row.id ? '已复制' : '复制'}
                      </button>
                      {hasInvitationAction(row, 'CONFIRM_INVITE') ? (
                        <button
                          type="button"
                          className={primaryButton}
                          disabled={
                            confirmingIds.has(row.id) ||
                            pendingIds.has(row.id) ||
                            restoringIds.has(row.id)
                          }
                          onClick={() => void mutate('CONFIRM_INVITE', new Set([row.id]))}
                        >
                          我已发送邀请信
                        </button>
                      ) : row.displayResult === 'SUCCESS' ? (
                        <span
                          role="status"
                          className={`text-sm font-semibold ${row.displayResult === 'SUCCESS' ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'}`}
                        >
                          {row.displayResult === 'SUCCESS' ? '成功' : '失败'}
                        </span>
                      ) : canStart ? (
                        <button
                          type="button"
                          className={primaryButton}
                          disabled={pending}
                          aria-label={`开始邀请 ${row.email}`}
                          aria-busy={pending}
                          onClick={() => void mutate('START', new Set([row.id]))}
                        >
                          {row.displayResult === 'FAILED' ? '重试' : '开始'}
                        </button>
                      ) : row.status === 'processing' ? null : (
                        // 待复核时不要摆一个灰着的"开始"：那看起来像坏了，
                        // 直接说明现在处于什么状态、要不要等。
                        <span role="status" className="text-sm text-[var(--color-muted)]">
                          {unavailableAction(row)}
                        </span>
                      )}
                    </div>
                  </div>
                  {row.status === 'bound' ? (
                    <p className="text-sm text-[var(--color-muted)]">
                      {hasInvitationAction(row, 'CONFIRM_INVITE')
                        ? '邮箱已领取，等待邀请信；发送后请点击“我已发送邀请信”。尚未开始执行。'
                        : content?.canStart
                          ? '尚未开始执行，请点击“开始”。'
                          : '请稍候重试。'}
                    </p>
                  ) : row.status === 'review' ? (
                    <p className="text-sm text-[var(--color-warning)]">结果待确认，请联系客服，勿重复领取。</p>
                  ) : null}
                  {showProgress && <NumericProgress value={row.progressPercent} label={`${row.email} 邀请进度`} success={row.displayResult === 'SUCCESS'} failed={row.displayResult === 'FAILED'} />}

                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
      <span className="sr-only" role="status" aria-live="polite">
        {copiedId ? '邮箱已复制' : ''}
      </span>
      {poolPrompt ? (
        <div className="fixed inset-0 z-[90] flex items-center justify-center px-4">
          <button
            type="button"
            className="absolute inset-0 bg-[var(--color-header)]/55 backdrop-blur-md"
            aria-label="关闭"
            onClick={closePoolPrompt}
          />
          <div
            ref={poolPromptRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="invitation-pool-prompt-title"
            className="relative w-full max-w-sm rounded-2xl border border-[var(--color-border)] bg-[var(--color-card)] p-5 shadow-[0_24px_80px_rgba(19,33,55,0.28)] sm:p-6"
          >
            <p
              id="invitation-pool-prompt-title"
              className="text-center text-base font-semibold text-[var(--color-ink)]"
            >
              可用账号不足
            </p>
            <p className="mt-3 text-sm leading-relaxed text-[var(--color-muted)]">
              {`当前可用账号 ${poolPrompt.available} 个，少于你提交的 ${poolPrompt.requested} 份 CDK。继续只会为前 ${poolPrompt.available} 份兑换邮箱，剩余 ${poolPrompt.requested - poolPrompt.available} 份仍可稍后再试。`}
            </p>
            <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:justify-center">
              <button type="button" className={secondaryButton} onClick={closePoolPrompt}>
                取消
              </button>
              <button type="button" className={primaryButton} onClick={confirmPoolPrompt}>
                {`继续兑换前 ${Math.min(poolPrompt.available, poolPrompt.codes.length)} 份`}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

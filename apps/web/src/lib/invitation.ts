export type InvitationAction = 'START' | 'POLL' | 'CONFIRM_INVITE';
/** 单次提交上限。必须与服务端 INVITATION_BATCH_LIMIT 保持一致。 */
export const INVITATION_BATCH_LIMIT = 10;
export type InvitationCompletionBasis =
  'INVITEE_FLOW_CLI' | 'LEGACY_REWARD_VERIFIED' | 'CODEX_INVIT_ACTUAL_REPLY';
export type InvitationError = { code: string; message?: string; requestId?: string };
export type InvitationItem = {
  line?: number;
  id?: string;
  email?: string;
  sessionSecret?: string;
  progressPercent?: number;
  displayResult?: 'SUCCESS' | 'FAILED' | null;
  completionBasis?: InvitationCompletionBasis | null;
  cliCompleted?: boolean;
  rewardVerified?: boolean;
  status?: string;
  allowedActions?: string[];
  error?: InvitationError;
};
export type InvitationRow = {
  id: string;
  email: string;
  progressPercent: number;
  displayResult: 'SUCCESS' | 'FAILED' | null;
  allowedActions: InvitationAction[];
  status: string;
};
export type InvitationAttemptStatus = 'completed' | 'rejected' | 'pending' | 'review';
export type InvitationBatch = {
  items: InvitationItem[];
  attemptId?: string;
  attemptStatus?: InvitationAttemptStatus;
  message?: string;
  canRedeem?: boolean;
  canStart?: boolean;
  unavailableMessage?: string;
  poolAvailable?: number;
  poolRequested?: number;
};
export type InvitationCapability = { id: string; sessionSecret: string };

export function parseInvitationCodes(input: string): {
  codes: string[];
  lines: number[];
  count: number;
  duplicates: { line: number; firstLine: number }[];
} {
  const codes: string[] = [];
  const lines: number[] = [];
  const firstLines = new Map<string, number>();
  const duplicates: { line: number; firstLine: number }[] = [];
  let count = 0;
  input.split(/\r?\n/).forEach((value, index) => {
    const code = value.trim().toUpperCase();
    if (code) {
      count++;
      const firstLine = firstLines.get(code);
      if (firstLine !== undefined) {
        duplicates.push({ line: index + 1, firstLine });
        return;
      }
      firstLines.set(code, index + 1);
      codes.push(code);
      lines.push(index + 1);
    }
  });
  return { codes, lines, count, duplicates };
}

/** Keep capabilities and submitted CDKs out of React's rendered row state. */
export function invitationRow(item: InvitationItem): InvitationRow | null {
  if (!item.id || !item.email || item.error) return null;
  const result =
    item.displayResult === 'SUCCESS' || item.displayResult === 'FAILED' ? item.displayResult : null;
  const progress = Number.isFinite(item.progressPercent) ? item.progressPercent! : 0;
  return {
    id: item.id,
    email: item.email,
    progressPercent: result === 'SUCCESS' ? 100 : Math.max(0, Math.min(95, progress)),
    displayResult: result,
    status:
      item.status ||
      (result === 'SUCCESS'
        ? 'done'
        : result === 'FAILED'
          ? 'failed'
          : item.allowedActions?.includes('CONFIRM_INVITE')
            ? 'bound'
            : item.allowedActions?.includes('START')
              ? 'bound'
              : 'processing'),
    allowedActions:
      result === 'SUCCESS'
        ? []
        : (item.allowedActions ?? []).filter(
            (action): action is InvitationAction =>
              action === 'START' || action === 'POLL' || action === 'CONFIRM_INVITE',
          ),
  };
}

export function hasInvitationAction(row: InvitationRow, action: InvitationAction): boolean {
  return row.allowedActions.includes(action);
}

export function invalidInvitationCapability(error?: InvitationError): boolean {
  return Boolean(
    error &&
    [
      'SESSION_SECRET_REQUIRED',
      'SESSION_SECRET_INVALID',
      'INVITATION_SESSION_NOT_FOUND',
      'INVITATION_SESSION_SUPERSEDED',
      'SESSION_SUPERSEDED',
    ].includes(error.code),
  );
}

export function invitationCapabilities(
  rows: InvitationRow[],
  secrets: ReadonlyMap<string, string>,
  action: InvitationAction,
  ids?: ReadonlySet<string>,
): InvitationCapability[] {
  return rows.flatMap((row) => {
    const secret = secrets.get(row.id);
    return secret && hasInvitationAction(row, action) && (!ids || ids.has(row.id))
      ? [{ id: row.id, sessionSecret: secret }]
      : [];
  });
}

export function invitationPollDelay(
  rows: InvitationRow[],
  pending: ReadonlySet<string>,
): number | null {
  const polling = rows.filter(
    (row) =>
      hasInvitationAction(row, 'POLL') &&
      ((!hasInvitationAction(row, 'START') && !hasInvitationAction(row, 'CONFIRM_INVITE')) ||
        pending.has(row.id)),
  );
  if (!polling.length) return null;
  return polling.some((row) => row.progressPercent < 95) ? 2000 : 15000;
}

/** Merge known tasks by session ID; errors never discard unrelated tasks. */
export function mergeInvitationRows(
  rows: InvitationRow[],
  items: InvitationItem[],
): InvitationRow[] {
  const merged = new Map(rows.map((row) => [row.id, row]));
  for (const item of items) {
    if (!item.id) continue;
    const previous = merged.get(item.id);
    if (previous && invalidInvitationCapability(item.error)) {
      merged.set(item.id, { ...previous, allowedActions: [], status: 'session_invalid' });
    } else {
      const next = invitationRow(item);
      if (next) merged.set(item.id, next);
    }
  }
  return [...merged.values()];
}

export function invitationStatusLabel(row: InvitationRow): string {
  if (row.status === 'bound')
    return hasInvitationAction(row, 'CONFIRM_INVITE') ? '等待邀请信' : '尚未开始';
  return (
    (
      {
        done: '成功',
        failed: '处理失败',
        review: '结果待确认',
        void: '已作废',
        unknown: '结果待确认',
        session_invalid: '领取记录不可用',
      } as Record<string, string>
    )[row.status] || '状态待确认'
  );
}

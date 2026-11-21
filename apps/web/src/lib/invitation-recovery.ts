/** Only capabilities and original line numbers belong in tab-scoped storage. */
export const INVITATION_RECOVERY_KEY = 'lc_invitation_recovery_v1';
export const INVITATION_RECOVERY_TTL = 24 * 60 * 60 * 1000;
export const INVITATION_RECOVERY_LIMIT = 200;
const MAX_STORAGE_LENGTH = 64 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SECRET = /^[0-9a-f]{64}$/;
type TimedRecord = { createdAt: number; expiresAt: number };
export type StoredInvitationSession = TimedRecord & {
  id: string;
  sessionSecret: string;
  line: number;
};
export type StoredInvitationAttempt = TimedRecord & {
  id: string;
  recoverySecret: string;
  lines: number[];
  knownSessionExpirations?: { id: string; expiresAt: number }[];
  restoreIds?: string[];
};
export type InvitationRecovery = {
  version: 1;
  sessions: StoredInvitationSession[];
  attempts: StoredInvitationAttempt[];
};
export type RecoveryStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export function emptyInvitationRecovery(): InvitationRecovery {
  return { version: 1, sessions: [], attempts: [] };
}

function validTime(value: TimedRecord, now: number): boolean {
  return (
    Number.isFinite(value.createdAt) &&
    Number.isFinite(value.expiresAt) &&
    value.createdAt > 0 &&
    value.createdAt <= now + 5 * 60 * 1000 &&
    value.expiresAt === value.createdAt + INVITATION_RECOVERY_TTL &&
    value.expiresAt > now
  );
}
function validLine(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) > 0 && Number(value) <= 100000;
}
export function validInvitationSession(value: StoredInvitationSession, now = Date.now()): boolean {
  return Boolean(
    value &&
    UUID.test(value.id) &&
    SECRET.test(value.sessionSecret) &&
    validLine(value.line) &&
    validTime(value, now),
  );
}

export function parseInvitationRecovery(raw: string, now = Date.now()): InvitationRecovery {
  if (raw.length > MAX_STORAGE_LENGTH) throw new Error('恢复记录过大，已忽略。');
  const value = JSON.parse(raw);
  if (
    value?.version !== 1 ||
    !Array.isArray(value.sessions) ||
    !Array.isArray(value.attempts) ||
    value.sessions.length > INVITATION_RECOVERY_LIMIT ||
    value.attempts.length > 50
  ) {
    throw new Error('恢复记录格式不正确，已忽略。');
  }
  const sessions = new Map<string, StoredInvitationSession>();
  for (const record of value.sessions) {
    if (!validInvitationSession(record, now)) continue;
    const { id, sessionSecret, line, createdAt, expiresAt } = record;
    sessions.set(id, { id, sessionSecret, line, createdAt, expiresAt });
  }
  const attempts = new Map<string, StoredInvitationAttempt>();
  for (const record of value.attempts) {
    if (
      !record ||
      !UUID.test(record.id) ||
      !SECRET.test(record.recoverySecret) ||
      !validTime(record, now) ||
      !Array.isArray(record.lines) ||
      !record.lines.length ||
      record.lines.length > 50 ||
      !record.lines.every(validLine) ||
      (record.restoreIds !== undefined &&
        (!Array.isArray(record.restoreIds) ||
          !record.restoreIds.length ||
          record.restoreIds.length > 50 ||
          !record.restoreIds.every((id: unknown) => typeof id === 'string' && UUID.test(id)) ||
          new Set(record.restoreIds).size !== record.restoreIds.length)) ||
      (record.knownSessionExpirations !== undefined &&
        (!Array.isArray(record.knownSessionExpirations) ||
          record.knownSessionExpirations.length > 2700 ||
          record.knownSessionExpirations.some(
            (entry: { id?: unknown; expiresAt?: unknown }) =>
              !entry ||
              typeof entry.id !== 'string' ||
              !UUID.test(entry.id) ||
              typeof entry.expiresAt !== 'number' ||
              !Number.isFinite(entry.expiresAt) ||
              entry.expiresAt <= record.createdAt ||
              entry.expiresAt - INVITATION_RECOVERY_TTL <= 0 ||
              entry.expiresAt - INVITATION_RECOVERY_TTL > record.createdAt + 5 * 60 * 1000,
          ) ||
          new Set(record.knownSessionExpirations.map((entry: { id: string }) => entry.id)).size !==
            record.knownSessionExpirations.length))
    )
      continue;
    const { id, recoverySecret, createdAt, expiresAt } = record;
    attempts.set(id, {
      id,
      recoverySecret,
      createdAt,
      expiresAt,
      lines: [...record.lines],
      ...(record.restoreIds ? { restoreIds: [...record.restoreIds] } : {}),
      ...(record.knownSessionExpirations
        ? {
            knownSessionExpirations: record.knownSessionExpirations.map(
              ({ id, expiresAt }: { id: string; expiresAt: number }) => ({ id, expiresAt }),
            ),
          }
        : {}),
    });
  }
  return { version: 1, sessions: [...sessions.values()], attempts: [...attempts.values()] };
}

export function readInvitationRecovery(
  storage: RecoveryStorage,
  now = Date.now(),
): {
  recovery: InvitationRecovery;
  notice: string;
} {
  const raw = storage.getItem(INVITATION_RECOVERY_KEY);
  if (!raw) return { recovery: emptyInvitationRecovery(), notice: '' };
  let recovery: InvitationRecovery;
  try {
    recovery = parseInvitationRecovery(raw, now);
  } catch {
    storage.removeItem(INVITATION_RECOVERY_KEY);
    return {
      recovery: emptyInvitationRecovery(),
      notice: '上次领取记录暂时无法读取，可重新输入卡密查看。已开始的任务不受影响。',
    };
  }
  const original = JSON.parse(raw);
  const removed =
    original.sessions.length + original.attempts.length >
    recovery.sessions.length + recovery.attempts.length;
  if (removed) {
    try {
      writeInvitationRecovery(storage, recovery, now);
    } catch {
      return {
        recovery,
        notice: '部分领取记录已过期，浏览器暂时无法保存更新，请重新输入卡密查看。',
      };
    }
  }
  return {
    recovery,
    notice: removed ? '部分领取记录已过期，可重新输入卡密查看。已开始的任务不受影响。' : '',
  };
}

export function writeInvitationRecovery(
  storage: RecoveryStorage,
  recovery: InvitationRecovery,
  now = Date.now(),
  requiredAttempts: StoredInvitationAttempt[] = [],
): void {
  if (recovery.sessions.length > INVITATION_RECOVERY_LIMIT || recovery.attempts.length > 50) {
    throw new Error('本页领取记录较多，请在新的标签页输入卡密查看。');
  }
  const clean = parseInvitationRecovery(JSON.stringify(recovery), now);
  const proof = (attempt: StoredInvitationAttempt) =>
    JSON.stringify([
      attempt.id,
      attempt.recoverySecret,
      attempt.createdAt,
      attempt.expiresAt,
      attempt.lines,
      attempt.restoreIds ?? null,
      attempt.knownSessionExpirations?.map(({ id, expiresAt }) => [id, expiresAt]) ?? null,
    ]);
  const preserved = (records: StoredInvitationAttempt[], required: StoredInvitationAttempt[]) =>
    required.every((attempt) =>
      records.some((record) => record.id === attempt.id && proof(record) === proof(attempt)),
    );
  const unexpired = recovery.attempts.filter(
    (attempt) => !Number.isFinite(attempt.expiresAt) || attempt.expiresAt > now,
  );
  if (!preserved(clean.attempts, [...unexpired, ...requiredAttempts]))
    throw new Error('领取记录暂时无法保存，请稍后再试。');
  if (!clean.sessions.length && !clean.attempts.length) storage.removeItem(INVITATION_RECOVERY_KEY);
  else storage.setItem(INVITATION_RECOVERY_KEY, JSON.stringify(clean));
  if (requiredAttempts.length) {
    const raw = storage.getItem(INVITATION_RECOVERY_KEY);
    if (!raw || !preserved(parseInvitationRecovery(raw, now).attempts, requiredAttempts))
      throw new Error('领取记录暂时无法保存，请稍后再试。');
  }
}

export function createInvitationAttempt(
  lines: number[],
  cryptoApi = globalThis.crypto,
  now = Date.now(),
): StoredInvitationAttempt {
  if (!cryptoApi?.randomUUID || !cryptoApi?.getRandomValues)
    throw new Error('当前浏览器暂时无法领取邮箱，请稍后再试或更换浏览器。');
  const bytes = cryptoApi.getRandomValues(new Uint8Array(32));
  return {
    id: cryptoApi.randomUUID(),
    recoverySecret: [...bytes].map((value) => value.toString(16).padStart(2, '0')).join(''),
    lines: [...lines],
    createdAt: now,
    expiresAt: now + INVITATION_RECOVERY_TTL,
  };
}

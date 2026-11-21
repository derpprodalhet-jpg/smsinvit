/**
 * 「这次请求失败了，我还能不能重新找回原来的单子？」的判定规则。
 *
 * 现在只有接码页（/supprot_sim/<CDK> 深链自动兑换）在用它：深链自动兑换失败后，界面要决定
 * 是否仍值得提供"重新查询原兑换"的入口。邀请页有自己的恢复模块
 * （lib/invitation-recovery.ts），不依赖这里。
 *
 * 背景（审查 F06/F07）：早期面板在刷新恢复时，对**所有**请求失败都统一删掉
 * sessionStorage 里的恢复凭证。临时断网、429、502 与"凭证确实失效"被当成同一回事，
 * 于是仍在处理的订单在当前标签页里永久失去自动恢复入口；用户只能重新输入卡密，而
 * 已消费的卡密在公开入口又只会回"已使用"（F07）。
 *
 * 这里把判定收敛成纯函数，便于单测覆盖"断网/429/502 可重试 vs 已失效/不存在不可重试"。
 */

/** 与 @smsinvit/errors / lib/api 的错误形状保持结构兼容，避免强耦合。 */
export type RecoveryErrorLike = {
  code?: string;
  httpStatus?: number;
  name?: string;
} | null;

/** catch 子句里拿到的是 unknown；这里统一收窄，非对象一律当临时故障。 */
export function asRecoveryError(err: unknown): RecoveryErrorLike {
  if (!err || typeof err !== 'object') return null;
  const candidate = err as { code?: unknown; httpStatus?: unknown };
  const code = typeof candidate.code === 'string' ? candidate.code : undefined;
  const httpStatus = typeof candidate.httpStatus === 'number' ? candidate.httpStatus : undefined;
  if (code === undefined && httpStatus === undefined) return { name: (err as { name?: string }).name };
  return { ...(code !== undefined ? { code } : {}), ...(httpStatus !== undefined ? { httpStatus } : {}) };
}

/** 服务端明确表示"这个凭证不再有效"的状态码。 */
const INVALID_STATUS = new Set([400, 401, 403, 404, 410]);

/** 服务端明确表示"会话不存在"的状态码（可以安全放弃恢复）。 */
const NOT_FOUND_STATUS = new Set([404, 410]);

/** 明确表示凭证/会话已失效的错误码（没有状态码时也认）。 */
const INVALID_CODE = /SESSION_SECRET|CREDENTIAL_INVALID/;

/** 明确表示"会话不存在"的错误码。 */
const NOT_FOUND_CODE = /NOT_FOUND|SESSION_EXPIRED|SESSION_CLOSED/;

/**
 * 判定一次失败属于哪一类。
 *
 * 兜底**不能**是 transient：业务拒绝（例如 409「卡密已使用」、422 参数不合法）同样是
 * 终局，把它当可重试会让界面劝用户重试一个注定不会成功的动作。所以只有明确可重试的
 * 形态（网络层错误、429、408、5xx）才返回 transient，其余按终局处理。
 */
export function classifyRecoveryError(
  err: unknown,
): 'transient' | 'invalid' | 'not-found' | 'permanent' {
  const value = asRecoveryError(err);
  if (!value) return 'transient';
  const status = typeof value.httpStatus === 'number' ? value.httpStatus : undefined;
  const code = typeof value.code === 'string' ? value.code : '';
  if (NOT_FOUND_STATUS.has(status as number)) return 'not-found';
  if (INVALID_STATUS.has(status as number)) return 'invalid';
  // 没有 HTTP 状态：网络层失败、解析失败、被中止，或服务端 5xx。
  if (status === undefined) {
    if (code === 'NETWORK_ERROR' || code === 'SERVICE_UNAVAILABLE') return 'transient';
    if (NOT_FOUND_CODE.test(code)) return 'not-found';
    if (INVALID_CODE.test(code)) return 'invalid';
    // 未知且没有状态码：按临时故障处理，避免误删恢复记录（F06 的核心要求）。
    return 'transient';
  }
  if (status >= 500 || status === 429 || status === 408) return 'transient';
  // 其余 4xx 都是业务/校验层面的终局结果（含 409、422 等）。
  return 'permanent';
}

/** 深链自动兑换失败后，是否还值得提供"重新查询原兑换"。 */
export function canRetryDeepLink(err: unknown): boolean {
  return classifyRecoveryError(err) === 'transient';
}

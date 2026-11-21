import { toUserFacingError, type ErrorLocale } from '@smsinvit/errors';

/**
 * 用户前台只允许「卡密/领取记录本身的问题」显示具体原因，其余一律同一句通用提示。
 *
 * 背景：共享的 `toUserFacingError` 有一条规则——**只要服务端消息含中文就原样显示**
 * （packages/errors/src/user-messages.ts）。于是后端把「邮箱服务暂时不可用」
 * 「请求参数格式或长度不合法」这类基础设施状态直接送到了买家面前：一次失败里既有
 * 卡密问题又有系统状态，用户分不清该改什么，也等于把"服务端现在什么不可用"公开了。
 *
 * 规则（按产品要求）：**卡密类错误与请求限流给明确提示**；其余（校验、服务不可用、库存、
 * 队列、复核等）统一为同一句通用文案。卡密正确、只是流程状态不允许时也不给细节。
 *
 * 枚举风险：`*_CDK_NOT_FOUND`（不存在）与 `*_CDK_INVALID`（无效）如果分开给文案，
 * 就等于告诉试探者"这个卡密存在"。两者统一成"卡密错误"；而"已使用/已停用/已过期/
 * 已绑定"是真实卡密的状态，对持有者是有效信息，予以保留。
 */
export const GENERIC_USER_ERROR = '输入错误，请核对后重新输入。';

/**
 * 卡密自身的问题。用后缀集合精确匹配，避免把 `*_CDK_UNAVAILABLE`（服务侧发不出卡）
 * 之类也当卡密错误。`NOT_FOUND`/`INVALID` 由共享文案统一成"卡密错误"。
 */
const CARD_ERROR =
  /^(?:SMS|INVITATION)_CDK_(?:NOT_FOUND|INVALID|USED|EXPIRED|DISABLED|FAILED|AMBIGUOUS|BOUND|BUSY)$/;

/** 卡密已通过校验，只是这次流程状态不支持该动作（持有者可据此操作）。 */
const SESSION_ERROR =
  /_SESSION_(?:NOT_FOUND|EXPIRED|CLOSED|STATE)$|_(?:ATTEMPT|RUN)_(?:NOT_FOUND|REQUIRED|UNBOUND|MISMATCH|CONFLICT|ALREADY_BOUND)$/;

export function isCardError(code: unknown): boolean {
  return typeof code === 'string' && CARD_ERROR.test(code);
}

function codeOf(err: unknown): string {
  if (err && typeof err === 'object' && 'code' in err && typeof (err as { code: unknown }).code === 'string') {
    return (err as { code: string }).code;
  }
  return '';
}

/** 卡密/会话问题可回显文案；其余仍隐藏内部状态。 */
export function userError(
  err: unknown,
  locale: ErrorLocale = 'zh',
  fallbackCode = 'UNKNOWN_ERROR',
): string {
  const code = codeOf(err);
  if (err && typeof err === 'object' &&
    (code === 'RATE_LIMIT_EXCEEDED' || ('httpStatus' in err && err.httpStatus === 429))) {
    const retryAt = 'retryAt' in err && typeof err.retryAt === 'number' ? err.retryAt : undefined;
    const seconds = retryAt ? Math.max(0, Math.ceil((retryAt - Date.now()) / 1000)) : 0;
    return seconds > 0 ? `操作较频繁，请等待 ${seconds} 秒后重试。` : '操作较频繁，请稍后重试。';
  }
  if (!isCardError(code) && !SESSION_ERROR.test(code)) return GENERIC_USER_ERROR;
  return stripReferenceId(toUserFacingError(err, locale, fallbackCode));
}

/** Remove any leaked「参考编号 …」suffix from messages. */
export function stripReferenceId(message: string): string {
  return message
    .replace(/[（(]\s*参考编号\s*[0-9a-fA-F-]{8,}[）)]\s*$/u, '')
    .replace(/\s*参考编号\s*[0-9a-fA-F-]{8,}\s*$/u, '')
    .trim();
}

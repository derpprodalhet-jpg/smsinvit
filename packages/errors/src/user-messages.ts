export type ErrorLocale = 'zh' | 'en';

const ZH: Record<string, string> = {
  AUTH_REQUIRED: '请先登录',
  AUTH_SESSION_EXPIRED: '登录已过期，请重新登录',
  AUTH_SESSION_INVALID: '登录状态无效，请重新登录',
  AUTH_INVALID_CREDENTIALS: '账号或密码错误',
  AUTH_RATE_LIMITED: '操作过于频繁，请稍后再试',
  AUTH_INVALID_TOKEN: '登录凭证无效',
  AUTH_CSRF_INVALID: '安全校验失败，请刷新页面后重试',
  AUTH_TOTP_REQUIRED: '请输入谷歌验证器验证码',
  AUTH_TOTP_INVALID: '验证码错误或已失效',
  AUTH_TOTP_NOT_ENABLED: '尚未启用谷歌验证器',
  AUTH_TOTP_ALREADY_ENABLED: '谷歌验证器已启用',
  AUTH_TOTP_SETUP_REQUIRED: '请先绑定谷歌验证器后再使用后台',
  AUTH_CHALLENGE_EXPIRED: '登录验证已过期，请重新登录',
  EMAIL_DISABLED: '邮件通道未启用，暂时无法发送验证码',
  CAPTCHA_REQUIRED: '请完成验证码',
  CAPTCHA_INVALID: '图形验证码错误，请点图刷新后重试',
  CAPTCHA_EXPIRED: '图形验证码已过期，请点图刷新后重试',
  USER_NOT_FOUND: '用户不存在',
  USERNAME_ALREADY_EXISTS: '用户名已存在',
  EMAIL_ALREADY_EXISTS: '邮箱已注册',
  USER_DISABLED: '账号已禁用',
  USER_BANNED: '账号已封禁',
  USER_CANNOT_MODIFY_TARGET: '无法修改该用户',
  USER_PROTECTED_ACCOUNT: '受保护账号不可操作',
  PASSWORD_TOO_WEAK: '密码强度不足',
  PERMISSION_DENIED: '没有权限执行此操作',
  PERMISSION_SCOPE_DENIED: '权限不足',
  RESOURCE_NOT_OWNED: '只能操作自己的资源',
  ADMIN_ACCESS_DENIED: '无后台访问权限',
  SENSITIVE_PERMISSION_REQUIRED: '需要更高权限',
  INVALID_PRICE: '价格不合法',
  INVALID_QUANTITY: '数量不合法',
  INVENTORY_OUT_OF_STOCK: '库存不足',
  INVENTORY_LOCK_FAILED: '库存锁定失败，请稍后重试',
  INVENTORY_ALREADY_LOCKED: '库存正在处理中',
  INVENTORY_ALREADY_SOLD: '商品已售出',
  INVENTORY_INVALID: '库存数据异常',
  INVENTORY_DUPLICATE: '卡密重复',
  INVENTORY_IMPORT_INVALID: '库存导入数据无效',
  INVENTORY_SKU_INVALID: '规格配置无效，请先修正规格',
  INVENTORY_EXPORT_DENIED: '无权导出库存',
  INVENTORY_RELEASE_CONFLICT: '库存释放冲突',
  INVENTORY_NOT_FOUND: '库存不存在',
  INVENTORY_GENERATE_DENIED: '仅接码 / 邀请 CDK 规格支持一键生成',
  SMS_UNAVAILABLE: '服务器正在维护，请稍后再试',
  // 「不存在」与「无效」统一成同一句：分开写等于告诉试探者"这个卡密存在"（枚举泄露）。
  SMS_CDK_NOT_FOUND: '卡密错误，请核对后重新输入',
  SMS_CDK_INVALID: '卡密错误，请核对后重新输入',
  SMS_CDK_USED: '该 CDK 已使用成功',
  SMS_CDK_FAILED: '该 CDK 已使用失败',
  SMS_CDK_EXPIRED: '该 CDK 已过期',
  SMS_CDK_DISABLED: '该 CDK 已被停用或作废',
  SMS_CDK_AMBIGUOUS: '该 CDK 存在重复记录，请联系客服核查',
  SMS_SESSION_NOT_FOUND: '接码会话不存在',
  SMS_SESSION_STATE: '当前会话状态不可操作',
  SESSION_SECRET_REQUIRED: '会话密钥缺失，请重新兑换',
  SESSION_SECRET_INVALID: '会话密钥无效，请重新兑换',
  SMS_CANCEL_TOO_EARLY: '号码获取后需等待约 2 分钟才能换号',
  SMS_LINE_NOT_MOBILE: '该号码未被确认为手机号码，已申请取消，请重试',
  SMS_PROVIDER_ERROR: '接码服务暂时异常，请稍后再试',
  SMS_SKU_MISCONFIGURED: '接码商品配置异常',
  SMS_WEBHOOK_FORBIDDEN: 'Webhook 未授权',
  IDEMPOTENCY_CONFLICT: '请勿重复提交',
  IDEMPOTENCY_REQUEST_IN_PROGRESS: '请求正在处理中，请稍候',
  MANUAL_DELIVERY_NOTE_REQUIRED: '异常交付须填写实际交付内容或处理结果',
  INVITATION_CDK_NOT_FOUND: '卡密错误，请核对后重新输入',
  INVITATION_CDK_DISABLED: '该卡密已停用或作废',
  INVITATION_CDK_EXPIRED: '该卡密已过期',
  INVITATION_CDK_UNAVAILABLE: '该卡密当前不可用，请联系客服',
  SESSION_REQUIRED: '需要完整 Session（含 sessionToken）',
  STORAGE_UNAVAILABLE: '存储服务暂不可用',
  RATE_LIMIT_EXCEEDED: '操作过于频繁，请稍后再试',
  RATE_LIMITED: '操作过于频繁，请稍后再试',
  VALIDATION_ERROR: '填写内容有误，请检查后重试',
  BAD_REQUEST: '请求无效，请检查后重试',
  RESOURCE_NOT_FOUND: '内容不存在',
  CONFLICT: '操作冲突，请刷新后重试',
  INTERNAL_ERROR: '服务繁忙，请稍后重试',
  INTERNAL_SERVER_ERROR: '服务繁忙，请稍后重试',
  SERVICE_UNAVAILABLE: '服务暂时不可用，请稍后重试',
  UPSTREAM_ERROR: '服务暂时不可用，请稍后重试',
  INVALID_REQUEST_BODY: '提交数据无效',
  NETWORK_ERROR: '网络连接失败，请检查网络或确认服务已启动',
  UNKNOWN_ERROR: '操作失败，请稍后重试',
  HTTP_401: '请先登录',
  HTTP_403: '没有权限执行此操作',
  HTTP_404: '内容不存在',
  HTTP_500: '服务繁忙，请稍后重试',
  HTTP_502: '服务暂时不可用，请稍后重试',
  HTTP_503: '服务暂时不可用，请稍后重试',
  HTTP_504: '服务响应超时，请稍后重试',
};

const EN: Record<string, string> = {
  AUTH_REQUIRED: 'Please sign in',
  AUTH_SESSION_EXPIRED: 'Session expired. Please sign in again',
  AUTH_SESSION_INVALID: 'Invalid session. Please sign in again',
  AUTH_INVALID_CREDENTIALS: 'Incorrect account or password',
  AUTH_RATE_LIMITED: 'Too many attempts. Please try again later',
  AUTH_INVALID_TOKEN: 'Invalid access token',
  AUTH_CSRF_INVALID: 'Security check failed. Please refresh and retry',
  AUTH_TOTP_REQUIRED: 'Enter your authenticator code',
  AUTH_TOTP_INVALID: 'Invalid or expired authenticator code',
  AUTH_TOTP_NOT_ENABLED: 'Authenticator is not enabled',
  AUTH_TOTP_ALREADY_ENABLED: 'Authenticator is already enabled',
  AUTH_TOTP_SETUP_REQUIRED: 'Set up authenticator before using admin',
  AUTH_CHALLENGE_EXPIRED: 'Login challenge expired. Please sign in again',
  EMAIL_DISABLED: 'Email delivery is disabled; cannot send verification codes',
  CAPTCHA_REQUIRED: 'Please complete the captcha',
  CAPTCHA_INVALID: 'Incorrect captcha. Click the image to refresh and try again',
  CAPTCHA_EXPIRED: 'Captcha expired. Click the image to refresh and try again',
  USER_NOT_FOUND: 'User not found',
  USERNAME_ALREADY_EXISTS: 'Username already taken',
  EMAIL_ALREADY_EXISTS: 'Email already registered',
  USER_DISABLED: 'Account disabled',
  USER_BANNED: 'Account banned',
  USER_CANNOT_MODIFY_TARGET: 'Cannot modify this user',
  USER_PROTECTED_ACCOUNT: 'Protected account',
  PASSWORD_TOO_WEAK: 'Password is too weak',
  PERMISSION_DENIED: 'Permission denied',
  PERMISSION_SCOPE_DENIED: 'Insufficient permission scope',
  RESOURCE_NOT_OWNED: 'You can only manage your own resources',
  ADMIN_ACCESS_DENIED: 'Admin access denied',
  SENSITIVE_PERMISSION_REQUIRED: 'Higher permission required',
  INVALID_PRICE: 'Invalid price',
  INVALID_QUANTITY: 'Invalid quantity',
  INVENTORY_OUT_OF_STOCK: 'Out of stock',
  INVENTORY_LOCK_FAILED: 'Failed to reserve stock. Please retry',
  INVENTORY_ALREADY_LOCKED: 'Stock is being processed',
  INVENTORY_ALREADY_SOLD: 'Already sold',
  INVENTORY_INVALID: 'Invalid inventory',
  INVENTORY_DUPLICATE: 'Duplicate card key',
  INVENTORY_IMPORT_INVALID: 'Invalid import data',
  INVENTORY_SKU_INVALID: 'Invalid SKU configuration. Correct it before importing',
  INVENTORY_EXPORT_DENIED: 'Export not allowed',
  INVENTORY_RELEASE_CONFLICT: 'Inventory release conflict',
  INVENTORY_NOT_FOUND: 'Inventory not found',
  INVENTORY_GENERATE_DENIED: 'One-click generate is only for SMS / Codex invite redeem SKUs',
  SMS_UNAVAILABLE: 'Service temporarily unavailable. Please try again later',
  SMS_CDK_NOT_FOUND: 'Invalid CDK',
  SMS_CDK_USED: 'This CDK was already used',
  SMS_CDK_FAILED: 'This CDK has failed',
  SMS_CDK_EXPIRED: 'This CDK has expired',
  SMS_CDK_DISABLED: 'This CDK has been disabled or recalled',
  SMS_CDK_AMBIGUOUS: 'Duplicate CDK records. Contact support',
  SMS_SESSION_NOT_FOUND: 'SMS session not found',
  SMS_SESSION_STATE: 'Session cannot perform this action',
  SESSION_SECRET_REQUIRED: 'Session secret missing. Redeem the CDK again',
  SESSION_SECRET_INVALID: 'Invalid session secret. Redeem the CDK again',
  SMS_CANCEL_TOO_EARLY: 'Please wait about 2 minutes before requesting a new number',
  SMS_LINE_NOT_MOBILE: 'This number was not verified as mobile. Cancellation was requested',
  SMS_PROVIDER_ERROR: 'SMS provider error. Please try again later',
  SMS_SKU_MISCONFIGURED: 'SMS product misconfigured',
  SMS_WEBHOOK_FORBIDDEN: 'Webhook forbidden',
  IDEMPOTENCY_CONFLICT: 'Duplicate submission',
  IDEMPOTENCY_REQUEST_IN_PROGRESS: 'Request is still processing',
  MANUAL_DELIVERY_NOTE_REQUIRED:
    'Describe the actual delivery or resolution before completing this order',
  SESSION_REQUIRED: 'A full Session (with sessionToken) is required',
  STORAGE_UNAVAILABLE: 'Storage unavailable',
  RATE_LIMIT_EXCEEDED: 'Too many requests. Please try later',
  RATE_LIMITED: 'Too many requests. Please try later',
  VALIDATION_ERROR: 'Invalid input. Please check and retry',
  BAD_REQUEST: 'Invalid request. Please check and retry',
  RESOURCE_NOT_FOUND: 'Resource not found',
  CONFLICT: 'Conflict. Please refresh and retry',
  INTERNAL_ERROR: 'Service busy. Please try later',
  INTERNAL_SERVER_ERROR: 'Service busy. Please try later',
  SERVICE_UNAVAILABLE: 'Service temporarily unavailable',
  UPSTREAM_ERROR: 'Service temporarily unavailable',
  INVALID_REQUEST_BODY: 'Invalid submitted data',
  NETWORK_ERROR: 'Network error. Please check connection or start the API service',
  UNKNOWN_ERROR: 'Something went wrong. Please try later',
  HTTP_401: 'Please sign in',
  HTTP_403: 'Permission denied',
  HTTP_404: 'Resource not found',
  HTTP_500: 'Service busy. Please try later',
  HTTP_502: 'Service temporarily unavailable',
  HTTP_503: 'Service temporarily unavailable',
  HTTP_504: 'Service timed out. Please try later',
};

export const ErrorCodeLabelsZh = ZH;
export const ErrorCodeLabelsEn = EN;
/** Shared error handling for redemption and administrator interfaces. */
export const ErrorCodeLabels = ZH;

const TECHNICAL_RE =
  /fetch failed|econnrefused|enotfound|etimedout|socket|network|errno|http\s*\d{3}|failed to fetch|abort|aggregateerror|typeerror|econnreset|und_err/i;

function resolveCode(code?: string | null): string {
  if (!code) return 'UNKNOWN_ERROR';
  if (ZH[code] || EN[code]) return code;
  if (/^HTTP_\d+$/.test(code)) return code;
  return 'UNKNOWN_ERROR';
}

export function getUserFacingMessage(code?: string | null, locale: ErrorLocale = 'zh'): string {
  const table = locale === 'en' ? EN : ZH;
  const key = resolveCode(code);
  return table[key] || table.UNKNOWN_ERROR;
}

/** 将任意异常归一为安全的用户文案；优先展示已有中文业务说明 */
export function toUserFacingError(
  err: unknown,
  locale: ErrorLocale = 'zh',
  fallbackCode = 'UNKNOWN_ERROR',
): string {
  const raw = err instanceof Error ? err.message : typeof err === 'string' ? err : '';
  if (locale === 'zh' && raw && /[\u4e00-\u9fff]/.test(raw) && !TECHNICAL_RE.test(raw)) {
    // Strip Alipay gateway dump after the useful reason
    const cut = raw.split('，网关生成的验签字符串')[0]?.split('网关生成的验签字符串')[0] || raw;
    return cut.length > 180 ? `${cut.slice(0, 180)}…` : cut;
  }

  if (
    err &&
    typeof err === 'object' &&
    'code' in err &&
    typeof (err as { code: unknown }).code === 'string'
  ) {
    return getUserFacingMessage((err as { code: string }).code, locale);
  }

  if (!raw || TECHNICAL_RE.test(raw)) {
    if (/econnrefused|enotfound|fetch failed|failed to fetch|network|socket/i.test(raw)) {
      return getUserFacingMessage('NETWORK_ERROR', locale);
    }
    if (/http\s*5\d\d|service unavailable|internal server/i.test(raw)) {
      return getUserFacingMessage('SERVICE_UNAVAILABLE', locale);
    }
    return getUserFacingMessage(fallbackCode, locale);
  }

  return getUserFacingMessage(fallbackCode, locale);
}

export function localizeErrorMessage(
  code?: string | null,
  _message?: string | null,
  locale: ErrorLocale = 'zh',
): string {
  return getUserFacingMessage(code || 'UNKNOWN_ERROR', locale);
}

/** 用户可见文案：只按错误码映射，不拼接后端原文 */
export function formatErrorDisplay(code?: string | null, _message?: string | null): string {
  return getUserFacingMessage(code || 'UNKNOWN_ERROR', 'zh');
}

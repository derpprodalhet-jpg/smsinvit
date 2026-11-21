export type Credentials = Record<string, any> & {
  email?: string;
  password?: string;
  totp_secret?: string;
  codex?: Record<string, any>;
};
export const credentialKeys = [
  'totp_secret',
  'totp_factor_id',
  'access_token',
  'session_token',
  'refresh_token',
  'id_token',
  'device_id',
  'csrf_token',
  'cookie_header',
  'password',
];
export const codexCredentialKeys = ['refresh_token', 'access_token', 'id_token', 'account_id'];
/** 凭证字段 → 中文（详情抽屉的字段名，绝不直接显示机器键）。 */
export const CREDENTIAL_FIELD_LABEL: Record<string, string> = {
  totp_secret: '2FA 密钥',
  totp_factor_id: '2FA 因子标识',
  access_token: '访问令牌',
  session_token: '会话令牌',
  refresh_token: '刷新令牌',
  id_token: 'ID 令牌',
  device_id: '设备标识',
  csrf_token: 'CSRF 令牌',
  cookie_header: '会话罐（Cookie）',
  password: '密码',
  'codex.refresh_token': 'Codex 刷新令牌',
  'codex.access_token': 'Codex 访问令牌',
  'codex.id_token': 'Codex ID 令牌',
  'codex.account_id': 'Codex 账号 ID',
};
export function credentialFieldLabel(key: string) {
  return CREDENTIAL_FIELD_LABEL[key] || `字段（${key}）`;
}
/** `registered/check_plus` 返回的检测结论。 */
export const PLUS_CHECK_LABEL: Record<string, string> = {
  plus_eligible: '可领 Plus',
  plus_active: '已是 Plus',
  free: 'Free',
  banned: '已封号',
  token_invalid: '凭证失效',
  no_at: '缺少访问令牌',
  not_found: '账号不存在',
  error: '检测失败',
};
/** 注册来源。 */
export const REGISTERED_SOURCE_LABEL: Record<string, string> = {
  invite: '邀请',
  single: '单次注册',
};
export function credentialRows(data: Credentials) {
  return [
    ...credentialKeys.filter((key) => data[key]).map((key) => ({ key, value: String(data[key]) })),
    ...codexCredentialKeys
      .filter((key) => data.codex?.[key])
      .map((key) => ({ key: `codex.${key}`, value: String(data.codex![key]) })),
  ];
}
export function credentialEditPayload(
  email: string,
  original: Credentials,
  password: string,
  secret: string,
) {
  const payload: { email: string; password?: string; totp_secret?: string } = { email };
  if (password !== (original.password || '')) payload.password = password;
  if (secret.trim() !== (original.totp_secret || '')) payload.totp_secret = secret.trim();
  return payload;
}
export function registeredExportPayload(format: string, emails: string[]) {
  return emails.length ? { format, emails } : { format, all: true };
}
export function plusCheckSummary(results: Record<string, { status: string }>, targetCount: number) {
  const counts: Record<string, number> = {};
  Object.values(results).forEach((info) => {
    counts[info.status] = (counts[info.status] || 0) + 1;
  });
  const plus = (counts.plus_eligible || 0) + (counts.plus_active || 0);
  const free = counts.free || 0,
    banned = counts.banned || 0,
    invalid = counts.token_invalid || 0;
  return {
    plus,
    free,
    banned,
    invalid,
    failed: Math.max(0, targetCount - plus - free - banned - invalid),
  };
}

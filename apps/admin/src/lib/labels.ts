import { resolveLabel } from './zh-label';
import type { StatusTone } from './zh-maps';

/**
 * 接码卡密状态（`sms_redeem_keys.status`）。
 *
 * `USED` 是卡密终态：后端把它当作「卡密核销完成」（`sms/service.py` 的核销
 * 通知出口），所以文案与其它三个模块统一成「已核销」，不再显示「已使用」。
 */
export const SMS_KEY_STATUS_LABEL: Record<string, string> = {
  UNUSED: '未使用',
  LOCKED: '锁定中',
  USED: '已核销',
  FAILED: '失败',
  EXPIRED: '已过期',
  DISABLED: '已停用',
};

/** 接码卡密状态配色：未使用=橘黄（四个卡密模块一致），终态=绿，失败=红。 */
export const SMS_KEY_STATUS_TONE: Record<string, StatusTone> = {
  UNUSED: 'warning',
  LOCKED: 'warning',
  USED: 'success',
  FAILED: 'error',
  EXPIRED: 'warning',
  DISABLED: 'default',
};

/**
 * 映射取值。
 *
 * `fallback` 只用于**空值**占位（例如「未执行」）；命中失败时一律返回
 * 「未知（原始值）」。以前的实现把调用方传进来的原始英文值当兜底输出，
 * 于是数据库出现新状态时列表里就直接显示英文。
 */
export function labelOf(map: Record<string, string>, value?: string | null, fallback?: string) {
  if (!value) return fallback || '—';
  return resolveLabel(map, value);
}

/** 接码卡密状态的中文标签（未命中走「未知（原始值）」）。 */
export function smsKeyStatusLabel(status?: string | null): string {
  return labelOf(SMS_KEY_STATUS_LABEL, status);
}

/** 接码卡密状态的标签色（未使用=橘黄）。 */
export function smsKeyStatusTone(status?: string | null): StatusTone {
  return SMS_KEY_STATUS_TONE[String(status ?? '').trim()] || 'default';
}

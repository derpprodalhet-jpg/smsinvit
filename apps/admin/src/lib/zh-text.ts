/**
 * 面向用户的原文兜底。
 *
 * 后端有些字段既可能是错误码（`SMS_CDK_NOT_FOUND`），也已经是中文说明
 * （「代理认证未通过，请检查认证信息」）。列表里既不能漏出裸英文，也不能把
 * 后端写好的中文说明包成「未知（…）」，所以统一经过这里：
 *   1. 空值 → `empty`；
 *   2. 含中文 → 原样显示（后端已给出可读说明）；
 *   3. 其余（错误码 / 纯英文）→ `moduleErrorLabel`，未确诊时显示「未知（原文）」。
 *
 * 单独成文件是因为 `lib/invitation.ts` 被 `tests/labels.test.mjs` 以严格依赖加载，
 * 不能再向它引入 `./zh-maps`。
 */
import { moduleErrorLabel } from './zh-maps';

export function localizedText(value?: string | null, empty = '—'): string {
  const text = String(value ?? '').trim();
  if (!text) return empty;
  if (/[\u4e00-\u9fff]/.test(text)) return text;
  return moduleErrorLabel(text);
}

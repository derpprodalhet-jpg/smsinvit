/**
 * 金额格式化：后端金额一律是「最小单位整数（minor）+ 币种代码」。
 *
 * 冻结契约（admin stats overview 的 `costs[]`）：
 *   - `totalMinor` / `todayMinor` 是整数最小单位，一律按 2 位小数展示；
 *   - USD 用 `$` 前缀（`$0.15`），其它币种用「金额 + 空格 + 币种代码」（`300.00 BDT`）；
 *   - 未知币种不做换算、不猜符号：按 2 位小数展示并原样保留代码；
 *   - 缺失值（null / undefined / 非数字）返回 `—`，绝不显示成 0。
 *
 * 这里只做**同币种**的展示格式化；跨币种合计必须由调用方按币种分行，
 * 本文件不提供任何求和能力。
 */
const SYMBOLS: Record<string, string> = { USD: '$' };

/** 格式化最小单位金额；未知币种保底 2 位小数 + 原样代码。 */
export function formatMinor(
  minor: number | string | null | undefined,
  currency: string | null | undefined,
): string {
  if (minor == null || minor === '') return '—';
  const value = typeof minor === 'number' ? minor : Number(minor);
  if (!Number.isFinite(value)) return '—';
  const code = (currency ?? '').trim();
  const sign = value < 0 ? '-' : '';
  const amount = (Math.abs(value) / 100).toFixed(2);
  const symbol = SYMBOLS[code.toUpperCase()];
  if (symbol) return `${sign}${symbol}${amount}`;
  return code ? `${sign}${amount} ${code}` : `${sign}${amount}`;
}

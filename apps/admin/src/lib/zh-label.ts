/**
 * 中文标签解析。所有面向用户的枚举都必须经过这里。
 *
 * 以前的映射在未命中时直接返回原始键（`map[key] || key`），于是一旦数据库里
 * 出现新的枚举值，界面上就冒出一段英文（「错误 / 结果」列最容易看到）。
 *
 * 这里统一兜底为「未知（原始值）」：界面保持中文，同时保留可排查的原始线索，
 * 既不显示裸英文，也不静默丢掉未知值。
 */

/** 未知枚举的展示文案：中文 + 原始值。 */
export function unknownLabel(raw?: string | null): string {
  const value = String(raw ?? '').trim();
  return value ? `未知（${value}）` : '—';
}

/**
 * 命中映射返回中文；未命中返回「未知（原始值）」；空值返回 `empty`。
 *
 * 第三个参数只用于**空值**占位，不会把原始值当作文案输出。
 */
export function resolveLabel(
  map: Record<string, string>,
  key?: string | null,
  empty = '—',
): string {
  const value = String(key ?? '').trim();
  if (!value) return empty;
  return map[value] || unknownLabel(value);
}

/** 大小写不敏感的版本：上游枚举大小写不稳定（如 `ABANDONED` / `abandoned`）时使用。 */
export function resolveLabelInsensitive(
  map: Record<string, string>,
  key?: string | null,
  empty = '—',
): string {
  const value = String(key ?? '').trim();
  if (!value) return empty;
  if (map[value]) return map[value];
  const lowered = value.toLowerCase();
  for (const [name, text] of Object.entries(map)) {
    if (name.toLowerCase() === lowered) return text;
  }
  return unknownLabel(value);
}

/** 布尔值的中文展示。 */
export function yesNoLabel(value: unknown): string {
  if (value === true || value === 1 || value === '1') return '是';
  if (value === false || value === 0 || value === '0') return '否';
  return '—';
}

/** Default matches smsinvit.business-time-zone; override when settings are loaded. */
export const DEFAULT_BUSINESS_TIME_ZONE = 'Asia/Shanghai';

let cachedZone = DEFAULT_BUSINESS_TIME_ZONE;

export function setBusinessTimeZone(zone: string | null | undefined) {
  if (zone && zone.trim()) cachedZone = zone.trim();
}

export function getBusinessTimeZone() {
  return cachedZone;
}

/** Parse API timestamps: Instant (`…Z`) or naive DATETIME (UTC storage) → Date. */
export function parseApiDate(value: string | Date): Date {
  if (value instanceof Date) return value;
  const s = value.trim();
  if (!s) return new Date(NaN);
  if (/^\d{4}-\d{2}-\d{2} \d/.test(s)) {
    return new Date(s.replace(' ', 'T') + 'Z');
  }
  // Jackson LocalDateTime / MySQL DATETIME via serverTimezone=UTC — no offset means UTC wall.
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) {
    return new Date(s + 'Z');
  }
  return new Date(s);
}

/** Format Instant/ISO/LocalDateTime strings in the business time zone. */
export function formatDateTime(
  value: string | Date | null | undefined,
  opts?: { timeZone?: string; withSeconds?: boolean },
): string {
  if (value == null || value === '') return '—';
  const date = parseApiDate(value instanceof Date ? value : String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  const timeZone = opts?.timeZone || cachedZone;
  const withSeconds = opts?.withSeconds !== false;
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: withSeconds ? '2-digit' : undefined,
      hour12: false,
    })
      .format(date)
      .replace(/\//g, '-');
  } catch {
    return date.toISOString();
  }
}

import dayjs, { type Dayjs } from 'dayjs';

export type ProListParams = Record<string, unknown> & {
  current?: number;
  pageSize?: number;
};

export type ListQueryOptions = {
  /** Map ProTable field → API query key */
  map?: Record<string, string>;
  /** Fields that are date ranges [Dayjs, Dayjs] → createdFrom/createdTo (or custom) */
  dateRangeFields?: Record<string, { from: string; to: string }>;
  /** Drop these keys from output */
  omit?: string[];
};

function isEmpty(v: unknown): boolean {
  if (v == null) return true;
  if (typeof v === 'string') return v.trim() === '';
  if (Array.isArray(v)) return v.length === 0;
  return false;
}

function toDateOnly(v: Dayjs | string): string | undefined {
  const d = dayjs(v);
  if (!d.isValid()) return undefined;
  // Backend interprets YYYY-MM-DD in smsinvit.business-time-zone.
  return d.format('YYYY-MM-DD');
}

/** Build URLSearchParams for admin list APIs from ProTable request params. */
export function buildListQuery(
  params: ProListParams,
  options: ListQueryOptions = {},
): URLSearchParams {
  const qs = new URLSearchParams();
  const page = Number(params.current) || 1;
  const pageSize = Number(params.pageSize) || 20;
  qs.set('page', String(page));
  qs.set('pageSize', String(pageSize));

  const omit = new Set(['current', 'pageSize', 'keyword', ...(options.omit || [])]);
  const map = options.map || {};
  const dateRanges = options.dateRangeFields || {};

  for (const [key, rangeKeys] of Object.entries(dateRanges)) {
    omit.add(key);
    const raw = params[key];
    if (Array.isArray(raw) && raw.length === 2) {
      const from = toDateOnly(raw[0] as Dayjs | string);
      const to = toDateOnly(raw[1] as Dayjs | string);
      if (from) qs.set(rangeKeys.from, from);
      if (to) qs.set(rangeKeys.to, to);
    }
  }

  for (const [key, value] of Object.entries(params)) {
    if (omit.has(key) || isEmpty(value)) continue;
    const apiKey = map[key] || key;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      qs.set(apiKey, String(value).trim());
    }
  }

  return qs;
}

/** Shared ProTable search UX defaults. */
export const PRO_TABLE_SEARCH = {
  labelWidth: 'auto' as const,
  defaultCollapsed: false,
};

export const PRO_TABLE_OPTIONS = {
  density: true,
  fullScreen: false,
  reload: true,
  setting: true,
};

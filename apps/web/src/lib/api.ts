import { getUserFacingMessage } from '@smsinvit/errors';

export function resolveApiBase(): string {
  return '';
}

type PublicService = 'sms' | 'invitation';
const pollDeadlines = new Map<string, number>();
/** Only progress reads share poll backoff; rejecting a new operation must not hide existing work. */
function isProgressRead(path: string, method: string): boolean {
  return (
    (method === 'GET' && /^\/api\/v1\/sms\/sessions\/[^/]+$/.test(path)) ||
    (method === 'GET' && /^\/api\/v1\/invitation\/redemption-attempts\/[^/]+$/.test(path)) ||
    (method === 'POST' && /^\/api\/v1\/invitation\/status$/.test(path))
  );
}
/** Pollers consult this without delaying or replaying any user mutation. */
export function pollRetryDelay(service: PublicService): number {
  return Math.max(0, (pollDeadlines.get(service) || 0) - Date.now());
}

export class ApiClientError extends Error {
  code: string;
  requestId?: string;
  /** Backend message when safe to show (e.g. Alipay gateway reason). */
  serverMessage?: string;
  httpStatus?: number;
  retryAfterMs?: number;
  retryAt?: number;
  /** True only for a validated success:false API error envelope. */
  structuredResponse = false;

  constructor(code: string, message?: string, requestId?: string) {
    const mapped = getUserFacingMessage(code || 'UNKNOWN_ERROR', 'zh');
    const hasZh = Boolean(message && /[\u4e00-\u9fff]/.test(message));
    super(hasZh ? message! : mapped);
    this.name = 'ApiClientError';
    this.code = code;
    this.requestId = requestId;
    this.serverMessage = message;
  }
}

/** User-facing error text (no request / trace id). */
export function formatApiError(err: unknown, fallback: string): string {
  if (err instanceof ApiClientError) {
    return err.message || fallback;
  }
  return fallback;
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}=([^;]*)`),
  );
  try {
    return match ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
}

let csrfRefreshInFlight: Promise<boolean> | null = null;

async function refreshCsrfCookie(): Promise<boolean> {
  if (typeof document === 'undefined') return false;
  if (!csrfRefreshInFlight) {
    csrfRefreshInFlight = (async () => {
      try {
        await fetch(`${resolveApiBase()}/api/v1/sms/page-content`, {
          method: 'GET',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
        });
      } catch {
        /* cookie may still have been written */
      } finally {
        csrfRefreshInFlight = null;
      }
      return Boolean(readCookie('lc_csrf'));
    })();
  }
  return csrfRefreshInFlight;
}

export async function apiFetch<T = any>(
  path: string,
  init?: RequestInit & { skipCsrfRetry?: boolean },
): Promise<{ data: T; meta?: any; raw: any }> {
  const base = resolveApiBase();
  const url = path.startsWith('http') ? path : `${base}${path.startsWith('/') ? path : `/${path}`}`;
  const skipCsrfRetry = init?.skipCsrfRetry;
  const { skipCsrfRetry: _omitCsrf, ...fetchInit } = init || {};
  fetchInit.signal?.throwIfAborted();

  const headers = new Headers(fetchInit.headers || {});
  const method = (fetchInit.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
    const csrf = readCookie('lc_csrf');
    if (csrf && !headers.has('X-CSRF-Token')) {
      headers.set('X-CSRF-Token', csrf);
    }
  }

  let res: Response;
  try {
    headers.set('Cache-Control', 'no-cache');
    headers.set('Pragma', 'no-cache');
    res = await fetch(url, {
      ...fetchInit,
      headers,
      credentials: 'include',
      cache: 'reload',
    });
  } catch (error) {
    if (fetchInit.signal?.aborted || (error && typeof error === 'object' && 'name' in error && error.name === 'AbortError')) throw error;
    throw new ApiClientError('NETWORK_ERROR');
  }

  const responseError = (code: string, message?: string, requestId?: string, structured = false) => {
    const error = new ApiClientError(code, message, requestId || res.headers.get('X-Request-ID') || undefined);
    error.httpStatus = res.status;
    if (res.status === 429) {
      const raw = res.headers.get('Retry-After') || '';
      const seconds = /^\d+(?:\.\d+)?$/.test(raw) ? Number(raw) : NaN;
      error.retryAfterMs = Number.isFinite(seconds) && seconds > 0 ? Math.min(seconds, 86400) * 1000 : 30000;
      error.retryAt = Date.now() + error.retryAfterMs;
      const pathname = new URL(url, 'https://local.invalid').pathname;
      const service = pathname.match(/^\/api\/v1\/([^/]+)/)?.[1];
      if (service && isProgressRead(pathname, method)) {
        pollDeadlines.set(service, Math.max(pollDeadlines.get(service) || 0, error.retryAt));
      }
    }
    error.structuredResponse = structured;
    return error;
  };
  const invalidResponse = () => responseError(res.status === 403 ? 'PERMISSION_DENIED' : 'SERVICE_UNAVAILABLE');
  let json: any;
  try {
    json = await res.json();
  } catch (error) {
    if (fetchInit.signal?.aborted || (error && typeof error === 'object' && 'name' in error && error.name === 'AbortError')) throw error;
    // CORS/proxy failures can return plain text or HTML before the JSON API runs.
    // A response parsing failure does not mean the submitted data was invalid.
    throw invalidResponse();
  }

  if (!json || typeof json !== 'object' || Array.isArray(json) || typeof json.success !== 'boolean') {
    throw invalidResponse();
  }
  if (json.success === false && (!json.error || typeof json.error !== 'object' || Array.isArray(json.error) || typeof json.error.code !== 'string' || !json.error.code)) {
    throw invalidResponse();
  }
  if (!res.ok && json.success === true) throw invalidResponse();
  if (!res.ok || json?.success === false) {
    const structured = json.success === false;
    const code = structured ? json.error.code : `HTTP_${res.status}`;
    if (
      typeof window !== 'undefined' &&
      ['CAPTCHA_UNAVAILABLE', 'CAPTCHA_INVALID'].includes(code)
    ) {
      // Notify the challenge only; never replay a state-changing request after a bot-check error.
      window.dispatchEvent(new Event('smsinvit:captcha-fallback'));
    }
    if (!skipCsrfRetry && code === 'CSRF_INVALID') {
      fetchInit.signal?.throwIfAborted();
      const renewed = await refreshCsrfCookie();
      fetchInit.signal?.throwIfAborted();
      if (renewed) {
        const retryHeaders = new Headers(init?.headers);
        retryHeaders.delete('X-CSRF-Token');
        return apiFetch<T>(path, { ...init, headers: retryHeaders, skipCsrfRetry: true });
      }
    }
    throw responseError(code, structured && typeof json.error.message === 'string' ? json.error.message : undefined,
                        structured && typeof json.error.requestId === 'string' ? json.error.requestId : undefined, structured);
  }

  if (!Object.prototype.hasOwnProperty.call(json, 'data')) throw invalidResponse();
  return { data: json.data as T, meta: json.meta, raw: json };
}

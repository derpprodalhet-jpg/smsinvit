import { formatErrorDisplay } from '@smsinvit/errors';

/**
 * Browser API base:
 * - Prefer same-origin `/api` via Next.js rewrite (avoids CORS / Failed to fetch)
 * - Fall back to absolute URL when explicitly configured
 */
export function resolveApiBase(): string {
  return '';
}

export class ApiClientError extends Error {
  code: string;
  requestId?: string;
  /** Raw API message (admin surfaces this when actionable). */
  serverMessage?: string;
  status?: number;
  details?: any;
  data?: any;

  constructor(code: string, message: string, requestId?: string, status?: number, details?: any) {
    // 中文优先：后端的真实业务说明本身就是中文时才用它；纯英文/技术串一律换成
    // 中文映射，原文仍保留在 serverMessage 里供排查（以前是英文原文直接进 UI）。
    const detail = (message || '').trim();
    const mapped = formatErrorDisplay(code, message);
    const usable = detail && /[\u4e00-\u9fff]/.test(detail) ? detail : mapped;
    super(usable);
    this.name = 'ApiClientError';
    this.code = code;
    this.requestId = requestId;
    this.serverMessage = detail || undefined;
    this.status = status;
    this.details = details;
    this.data = details;
  }
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}=([^;]*)`),
  );
  return match ? decodeURIComponent(match[1]) : null;
}

const AUTH_RETRY_CODES = new Set(['AUTH_REQUIRED', 'AUTH_SESSION_EXPIRED', 'AUTH_INVALID_TOKEN']);

let refreshInFlight: Promise<boolean> | null = null;
let csrfRefreshInFlight: Promise<boolean> | null = null;

async function refreshCsrfCookie(): Promise<boolean> {
  if (typeof document === 'undefined') return false;
  if (!csrfRefreshInFlight) {
    csrfRefreshInFlight = (async () => {
      try {
        // Any same-origin GET forces CsrfCookieFilter to (re)issue lc_csrf.
        await fetch(`${resolveApiBase()}/api/v1/admin/auth/me`, {
          method: 'GET',
          credentials: 'include',
          cache: 'reload',
          headers: {
            'Content-Type': 'application/json',
            'Cache-Control': 'no-cache',
            Pragma: 'no-cache',
          },
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

async function tryRefreshSession(): Promise<boolean> {
  if (typeof document === 'undefined') return false;
  if (readCookie('lc_admin_session') !== '1') return false;

  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      if (!readCookie('lc_csrf')) await refreshCsrfCookie();
      const headers = new Headers({ 'Content-Type': 'application/json' });
      const csrf = readCookie('lc_csrf');
      if (csrf) headers.set('X-CSRF-Token', csrf);
      try {
        const res = await fetch(`${resolveApiBase()}/api/v1/admin/auth/refresh`, {
          method: 'POST',
          headers,
          credentials: 'include',
          cache: 'reload',
          body: '{}',
        });
        const json = await res.json().catch(() => null);
        return Boolean(res.ok && json?.success !== false);
      } catch {
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

export async function apiFetch<T = any>(
  path: string,
  init?: RequestInit & { skipAuthRefresh?: boolean; skipCsrfRetry?: boolean },
): Promise<{ data: T; meta?: any; raw: any }> {
  const base = resolveApiBase();
  const url = path.startsWith('http') ? path : `${base}${path.startsWith('/') ? path : `/${path}`}`;
  const skipAuthRefresh = init?.skipAuthRefresh;
  const skipCsrfRetry = init?.skipCsrfRetry;
  const { skipAuthRefresh: _omitAuth, skipCsrfRetry: _omitCsrf, ...fetchInit } = init || {};

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
    // `reload` bypasses an already-poisoned HTTP disk cache (Baota stop-page HTML),
    // then refreshes the cache entry with the live response. `no-store` alone is not
    // enough in every Chromium path once a 200 HTML body was stored for this URL.
    headers.set('Cache-Control', 'no-cache');
    headers.set('Pragma', 'no-cache');
    res = await fetch(url, {
      ...fetchInit,
      headers,
      credentials: 'include',
      cache: 'reload',
    });
  } catch (error) {
    if (fetchInit.signal?.aborted) throw error;
    throw new ApiClientError(
      'NETWORK_ERROR',
      '无法连接服务器。请确认 API 已启动，或检查网络与跨域配置。',
    );
  }

  let json: any;
  try {
    const text = await res.text();
    if (!text) {
      throw new Error('empty body');
    }
    json = JSON.parse(text);
  } catch (error) {
    if (fetchInit.signal?.aborted) throw error;
    const ctype = res.headers.get('content-type') || 'unknown';
    throw new ApiClientError(
      'INVALID_REQUEST_BODY',
      `服务器返回了无法解析的响应（HTTP ${res.status}, ${ctype}）。请用无痕窗口打开，或清除本站缓存后硬刷新。`,
    );
  }

  if (!res.ok || json?.success === false) {
    const code = json?.error?.code || `HTTP_${res.status}`;
    if (
      typeof window !== 'undefined' &&
      ['CAPTCHA_UNAVAILABLE', 'CAPTCHA_INVALID'].includes(code)
    ) {
      // Notify the challenge only; never replay a state-changing request after a bot-check error.
      window.dispatchEvent(new Event('smsinvit:captcha-fallback'));
    }
    const message =
      json?.error?.message ||
      json?.message ||
      (typeof json?.detail === 'string' ? json.detail : '') ||
      '请求失败';
    const isAuthPath = path.includes('/api/v1/admin/auth/');
    if (!skipCsrfRetry && code === 'CSRF_INVALID') {
      const renewed = await refreshCsrfCookie();
      if (renewed) {
        const retryHeaders = new Headers(init?.headers);
        retryHeaders.delete('X-CSRF-Token');
        return apiFetch<T>(path, { ...init, headers: retryHeaders, skipCsrfRetry: true });
      }
    }
    if (
      !skipAuthRefresh &&
      !isAuthPath &&
      AUTH_RETRY_CODES.has(code) &&
      readCookie('lc_admin_session') === '1'
    ) {
      const refreshed = await tryRefreshSession();
      if (refreshed) {
        return apiFetch<T>(path, { ...init, skipAuthRefresh: true });
      }
    }
    throw new ApiClientError(code, message, json?.error?.requestId, res.status, json);
  }

  return { data: json.data as T, meta: json.meta, raw: json };
}

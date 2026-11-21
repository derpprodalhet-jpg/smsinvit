import { apiFetch } from './api';
import { adminAuthHeaders, hasAdminSession } from './auth';

/** Authenticated JSON helper for admin pages. */
export async function adminApi<T = any>(path: string, init?: RequestInit) {
  if (!hasAdminSession()) {
    throw new Error('未登录或会话已失效，请重新登录。');
  }
  const headers = new Headers(init?.headers || {});
  if (!headers.has('Content-Type') && init?.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  const auth = adminAuthHeaders();
  Object.entries(auth).forEach(([k, v]) => {
    if (v != null && !headers.has(k)) headers.set(k, String(v));
  });
  return apiFetch<T>(path, { ...init, headers });
}

export type PageMeta = {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
};

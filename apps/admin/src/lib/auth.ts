import { apiFetch } from './api';

const LEGACY_ACCESS_KEY = 'smsinvit_admin_access_token';
const LEGACY_REFRESH_KEY = 'smsinvit_admin_refresh_token';

export type AdminUser = {
  id: string;
  username: string;
  email: string;
  displayName: string | null;
  roles: string[];
  totpEnabled?: boolean;
  totpSetupRequired?: boolean;
};

export type LoginResult =
  | {
      needTotp: false;
      accessToken: string;
      refreshToken: string;
      user: AdminUser;
    }
  | {
      needTotp: true;
      challengeToken: string;
      user: { id: string; username: string; email: string; displayName: string | null };
    };

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}=([^;]*)`),
  );
  return match ? decodeURIComponent(match[1]) : null;
}

export function hasAdminSession(): boolean {
  return readCookie('lc_admin_session') === '1';
}

export function adminAuthHeaders(): Record<string, string> {
  const csrf = readCookie('lc_csrf');
  return csrf ? { 'X-CSRF-Token': csrf } : {};
}

export function getAdminAccessToken(): string | null {
  return hasAdminSession() ? 'cookie' : null;
}

export function getAdminRefreshToken(): string | null {
  return null;
}

export function clearAdminTokens() {
  if (typeof window === 'undefined') return;
  localStorage.removeItem(LEGACY_ACCESS_KEY);
  localStorage.removeItem(LEGACY_REFRESH_KEY);
  if (typeof document !== 'undefined') {
    document.cookie = 'lc_admin_session=; Max-Age=0; path=/';
  }
}

export type CaptchaChallenge = {
  captchaToken: string;
  prompt?: string;
  expiresInSeconds: number;
  type: string;
  image?: string;
};

export async function fetchLoginCaptcha() {
  const { data } = await apiFetch<CaptchaChallenge>('/api/v1/admin/auth/captcha', {
    cache: 'no-store',
  });
  return data;
}

export async function adminLogin(
  identifier: string,
  password: string,
  bot: { captchaToken?: string; captchaAnswer?: string },
): Promise<LoginResult> {
  clearAdminTokens();
  const { data } = await apiFetch<LoginResult>('/api/v1/admin/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      identifier,
      password,
      captchaToken: bot.captchaToken || undefined,
      captchaAnswer: bot.captchaAnswer || undefined,
    }),
  });
  return data;
}

export async function adminLoginTotp(challengeToken: string, code: string): Promise<AdminUser> {
  const { data } = await apiFetch<{ user: AdminUser }>('/api/v1/admin/auth/login/totp', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ challengeToken, code }),
  });
  return data.user;
}

export async function fetchAdminMe(): Promise<AdminUser | null> {
  if (!hasAdminSession()) return null;

  try {
    const { data } = await apiFetch<AdminUser>('/api/v1/admin/auth/me', {
      cache: 'no-store',
    });
    return data;
  } catch (err: any) {
    if (
      err?.code === 'AUTH_REQUIRED' ||
      err?.code === 'AUTH_SESSION_EXPIRED' ||
      err?.code === 'AUTH_INVALID_TOKEN'
    ) {
      clearAdminTokens();
      return null;
    }
    throw err;
  }
}

export async function adminLogout() {
  try {
    if (hasAdminSession()) {
      await apiFetch('/api/v1/admin/auth/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
    }
  } catch {
    /* ignore */
  } finally {
    clearAdminTokens();
  }
}

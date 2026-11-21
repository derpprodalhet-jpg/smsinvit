import { useSyncExternalStore } from 'react';

export type RegistrationPreferences = {
  proxy: string;
  otpTimeout: number;
  want2fa: boolean;
  wantAccessToken: boolean;
  wantSessionToken: boolean;
  wantRefreshToken: boolean;
  allowExistingLogin: boolean;
  poolOnly: boolean;
};
const defaults: RegistrationPreferences = {
  proxy: '',
  otpTimeout: 10,
  want2fa: true,
  wantAccessToken: true,
  wantSessionToken: true,
  wantRefreshToken: true,
  allowExistingLogin: true,
  poolOnly: false,
};
const key = 'smsinvit_invitation_register_options';
let state = { ...defaults };
try {
  const saved = JSON.parse(
    typeof window === 'undefined' ? '{}' : localStorage.getItem(key) || '{}',
  );
  for (const name of Object.keys(defaults) as (keyof RegistrationPreferences)[]) {
    if (name !== 'proxy' && typeof saved[name] === typeof defaults[name])
      Object.assign(state, { [name]: saved[name] });
  }
} catch {
  /* Browser storage is optional. */
}
const listeners = new Set<() => void>();
export function resetRegistrationSecrets() {
  state = { ...state, proxy: '' };
  listeners.forEach((listener) => listener());
}
export function useRegistrationPreferences() {
  const value = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
    () => defaults,
  );
  function update(patch: Partial<RegistrationPreferences>) {
    const next = { ...state, ...patch };
    // 账号池注册停在 Codex 授权之前：必须绑 2FA，此时也还没有 refresh token。
    if (next.poolOnly) {
      next.want2fa = true;
      next.wantRefreshToken = false;
    }
    state = next;
    // A proxy can contain a password; retain it only in this tab's memory.
    const { proxy: _proxy, ...safe } = state;
    try {
      localStorage.setItem(key, JSON.stringify(safe));
    } catch {
      /* optional */
    }
    listeners.forEach((listener) => listener());
  }
  return [value, update] as const;
}
export function registrationPayload(email: string, preferences: RegistrationPreferences) {
  const poolOnly = preferences.poolOnly;
  return {
    email: email.trim() || null,
    proxy: preferences.proxy.trim(),
    otp_timeout: preferences.otpTimeout,
    want_access_token: preferences.wantAccessToken,
    want_session_token: preferences.wantSessionToken,
    want_refresh_token: poolOnly ? false : preferences.wantRefreshToken,
    want_2fa: poolOnly ? true : preferences.want2fa,
    allow_existing_login: preferences.allowExistingLogin,
    pool_only: poolOnly,
  };
}

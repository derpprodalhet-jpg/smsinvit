export const TURNSTILE_SECRET_KEY = 'runtime.turnstile_secret_key';
export const SETTING_SECRET_MASK = '********';

type SettingValue = {
  key: string;
  isSecret?: boolean;
  valueType?: string;
  valueJson?: Record<string, unknown>;
};

export function isSecretSetting(row: SettingValue) {
  return row.isSecret === true || row.key === TURNSTILE_SECRET_KEY;
}

export function initialSettingDraft(row: SettingValue): string | boolean {
  const value = row.valueJson?.value ?? row.valueJson?.enabled ?? '';
  if (isSecretSetting(row)) {
    // Never put an old plaintext response into the input. The mask means "unchanged";
    // explicitly erasing it is a separate, intentional empty-value save for this runtime key.
    return row.key === TURNSTILE_SECRET_KEY && String(value).trim() ? SETTING_SECRET_MASK : '';
  }
  return row.valueType === 'BOOLEAN' ? value === true || value === 'true' : String(value);
}

export function preservesSecret(row: SettingValue, draft: unknown) {
  if (!isSecretSetting(row)) return false;
  const text = String(draft ?? '').trim();
  return text === SETTING_SECRET_MASK || (row.key !== TURNSTILE_SECRET_KEY && !text);
}

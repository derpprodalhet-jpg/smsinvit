import { adminApi } from './admin-api';
import { ApiClientError } from './api';
import { formatDateTime } from './format-datetime';

export const INVITATION_API_PREFIX = '/api/v1/admin/invitation/native/api';

/** Native business responses are intentionally left intact (some contain a data field). */
export async function invitationApi<T = any>(path: string, init?: RequestInit): Promise<T> {
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('://')) {
    throw new Error('邀请接口必须使用本项目的相对路径。');
  }
  const response = await adminApi<T>(INVITATION_API_PREFIX + path, init);
  const raw = response.raw;
  if (raw?.ok === false) {
    const detail = [raw.message, raw.reason, raw.error].find(
      (value) => typeof value === 'string' && value.trim(),
    );
    throw new ApiClientError(
      'INVITATION_REJECTED',
      detail || '操作未完成，请检查返回详情。',
      undefined,
      200,
      raw,
    );
  }
  return raw as T;
}

export function invitationAction<T = any>(path: string, body: unknown = {}): Promise<T> {
  return invitationApi<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

export function invitationErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请稍后重试。';
}

export function invitationTime(value: number | string | null | undefined): string {
  if (value == null || value === '') return '—';
  if (typeof value === 'number' || /^\d+(\.\d+)?$/.test(String(value))) {
    return formatDateTime(new Date(Number(value) * 1000));
  }
  return formatDateTime(String(value));
}

export async function copyInvitationText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const input = document.createElement('textarea');
  input.value = text;
  input.style.cssText = 'position:fixed;left:-9999px';
  document.body.appendChild(input);
  try {
    input.select();
    if (!document.execCommand('copy')) throw new Error('复制失败，请手动选择并复制。');
  } finally {
    input.remove();
  }
}

export type InvitationExport = {
  text?: string;
  b64?: string;
  mode?: string;
  mime?: string;
  filename?: string;
};
export function downloadInvitationExport(result: InvitationExport): void {
  const contents =
    result.b64 !== undefined
      ? Uint8Array.from(atob(result.b64), (char) => char.charCodeAt(0))
      : result.text || '';
  const url = URL.createObjectURL(
    new Blob([contents], { type: result.mime || 'text/plain;charset=utf-8' }),
  );
  const link = document.createElement('a');
  link.href = url;
  link.download = Array.from(result.filename || 'invitation-export.txt')
    .map((char) => (char === '/' || char === '\\' || char.charCodeAt(0) < 32 ? '_' : char))
    .join('');
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

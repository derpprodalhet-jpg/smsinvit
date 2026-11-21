export type Plan = {
  id: string;
  code: string;
  name: string;
  status: string;
  attributesJson: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
};
export type Batch = {
  id: string;
  skuId: string;
  filename?: string;
  fileName?: string;
  source?: string;
  status?: string;
  totalCount?: number;
  successCount: number;
  duplicateCount?: number;
  invalidCount?: number;
  attributesSnapshotJson?: Record<string, unknown>;
  createdAt: string;
};
export async function copyText(text: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const el = document.createElement('textarea');
  el.value = text;
  el.style.position = 'fixed';
  el.style.left = '-9999px';
  document.body.appendChild(el);
  el.select();
  try {
    if (!document.execCommand('copy')) throw new Error('复制失败，请在文本框中手动复制。');
  } finally {
    el.remove();
  }
}

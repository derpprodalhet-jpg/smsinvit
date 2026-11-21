export type CdkFilters = { status: string; batch: string; search: string };
export function cdkExportPayload(format: string, codes: string[], filters: CdkFilters) {
  return codes.length ? { format, codes } : { format, ...filters };
}
export function cdkCanMutate(row: { code: string; status: string }) {
  return row.code !== '***' && !['queued', 'running'].includes(row.status);
}
export async function collectFilteredCodes(
  filters: CdkFilters,
  fetchPage: (path: string) => Promise<{ items: { code: string }[]; total: number }>,
) {
  const codes = new Set<string>();
  // The authenticated boundary permits at most 5000 items per request.
  const limit = 5000;
  let offset = 0;
  while (true) {
    const page = await fetchPage(
      `/cdk/list?${new URLSearchParams({ ...filters, limit: String(limit), offset: String(offset) })}`,
    );
    for (const row of page.items || []) {
      if (row.code === '***') throw new Error('复制卡密需要凭证读取权限。');
      codes.add(row.code);
    }
    offset += page.items?.length || 0;
    if (!page.items?.length || offset >= page.total) return Array.from(codes);
  }
}

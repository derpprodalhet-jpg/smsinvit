export function normalizeInvitationProxy(input: string, forcedScheme?: ProxyScheme): string {
  let raw = input.trim();
  if (!raw || raw.startsWith('#')) return '';
  let scheme = 'http';
  if (raw.includes('://')) {
    const separator = raw.indexOf('://');
    scheme = raw.slice(0, separator).toLowerCase();
    raw = raw.slice(separator + 3);
  }
  // 操作员显式选了协议就照它来：粘贴 host:port:user:pass 时不用手打 socks5://。
  if (forcedScheme && forcedScheme !== 'auto') scheme = forcedScheme;
  if (scheme === 'socks5') scheme = 'socks5h';
  if (!['http', 'https', 'socks5h'].includes(scheme)) return '';
  const legacy = /^([^:@/\s]+):(\d+):([^:@/\s]+):([^@/\s]+)$/.exec(raw);
  if (legacy) raw = `${legacy[3]}:${legacy[4]}@${legacy[1]}:${legacy[2]}`;
  const valid = /^(?:[^:@/\s]+:[^@/\s]+@)?(?:[^:/\s]+|\[[0-9a-f:]+\]):(\d+)$/i.exec(raw);
  if (!valid || Number(valid[1]) < 1 || Number(valid[1]) > 65535) return '';
  return `${scheme}://${raw}`;
}

/** auto = 按行内协议（默认 HTTP）；http/https/socks5h = 一律按所选协议。 */
export type ProxyScheme = 'auto' | 'http' | 'https' | 'socks5h';

export type ProxyTestResult = {
  ok?: boolean;
  status?: 'success' | 'reachable' | 'failed';
  reachable?: boolean;
  category?: string;
  latency_ms?: number;
  ip?: string;
  error?: string;
  hint?: string;
  alt_scheme?: string;
  attempts?: number;
  request_error?: string;
};

export function mergeProxyTestResults(
  previous: Record<string, ProxyTestResult>,
  urls: string[],
  incoming?: Record<string, ProxyTestResult>,
  requestError = '本次检测未完成，请重试。',
) {
  const next = { ...previous };
  for (const url of urls) {
    const result = incoming?.[url];
    if (result && typeof result.ok === 'boolean') {
      next[url] = { ...result, error: result.ok ? '' : result.error, request_error: undefined };
    } else {
      next[url] = { ...previous[url], request_error: requestError };
    }
  }
  return next;
}

export function previewInvitationProxies(text: string, forcedScheme: ProxyScheme = 'auto') {
  const seen = new Set<string>();
  const rows = text.split(/\r?\n/).flatMap((raw, index) => {
    if (!raw.trim() || raw.trim().startsWith('#')) return [];
    const url = normalizeInvitationProxy(raw, forcedScheme);
    const duplicate = !!url && seen.has(url);
    if (url) seen.add(url);
    return [
      { line: index + 1, raw, url, status: !url ? 'invalid' : duplicate ? 'duplicate' : 'valid' },
    ];
  });
  return {
    rows,
    urls: [...seen],
    invalid: rows.filter((row) => row.status === 'invalid'),
    duplicates: rows.filter((row) => row.status === 'duplicate'),
  };
}

export type MailProvider = {
  kind: string;
  display_name: string;
  pooled: boolean;
  line_segments?: number;
  import_hint?: string;
  import_placeholder?: string;
};
export type ImportLineError = { line: number; error: string };
export function importLineCount(text: string) {
  return text.split('\n').filter((line) => line.trim() && !line.trim().startsWith('#')).length;
}
export function importLineErrors(error: unknown): ImportLineError[] {
  if (!error || typeof error !== 'object') return [];
  const details = error as {
    data?: { errors?: unknown };
    details?: { errors?: unknown };
    errors?: unknown;
  };
  const rows = details.data?.errors || details.details?.errors || details.errors;
  if (!Array.isArray(rows)) return [];
  return rows.filter((row) => row && typeof row.line === 'number' && typeof row.error === 'string');
}

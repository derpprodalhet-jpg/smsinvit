/**
 * 「卡密批次」页的展示口径。
 *
 * 事故背景：批次列表只列 `inventory_import_batches` 里的行，而历史导入与手工创建的卡密
 * 没有批次归属（`inventory_items.import_batch_id IS NULL`）。于是批次页的张数合计小于
 * 「卡密管理」页的全部卡密数，运营看到两个互相矛盾的数字，以为「邀请的卡密和生成的卡密
 * 对不上」。后端为此在批次列表里补了一个保留 id 的「未登记批次」行（只读），把
 * 「已登记批次 + 未登记批次 = 全部卡密」的关系固定成一处，页面只负责渲染，不再各自算一套。
 */

/** 后端保留的未登记批次 id（`common/inventory.py::UNASSIGNED_BATCH_ID`）。 */
export const UNASSIGNED_BATCH_ID = 'unassigned';
/** 未登记批次在页面上的名字。 */
export const UNASSIGNED_BATCH_LABEL = '未登记批次';
/** 未登记批次的来源文案：没有 `inventory_import_batches` 行，不套用 GENERATE / PASTE。 */
export const UNASSIGNED_SOURCE_LABEL = '未登记（历史导入 / 手工创建）';

export type BatchCounts = { registered: number; unassigned: number; total: number };

/** 还没选套餐 / 还没拿到数据时的三组数字：不显示任何张数，只说明口径。 */
export const EMPTY_BATCH_COUNTS: BatchCounts = { registered: 0, unassigned: 0, total: 0 };

/** 只依赖计数相关的字段，避免页面类型与模型模块互相 import。 */
export type CountableBatch = {
  id?: string;
  unassigned?: boolean;
  cardCount?: number | null;
  successCount?: number | null;
};

export type OriginBatch = { batch?: string; total?: number };

export type BatchListMeta = {
  total?: number;
  counts?: { registeredCards?: number; unassignedCards?: number; totalCards?: number };
};

/** 这一行是不是「未登记批次」的伪批次行。 */
export function isUnassignedBatch(row?: CountableBatch | null): boolean {
  if (!row) return false;
  return row.unassigned === true || String(row.id ?? '') === UNASSIGNED_BATCH_ID;
}

/** 未登记批次没有批次行可导出/作废，页面对它只提供只读查看。 */
export function canExportBatch(row?: CountableBatch | null): boolean {
  return !isUnassignedBatch(row);
}

function count(value: unknown): number {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.trunc(number) : 0;
}

/** 列表里的卡密张数：优先用后端实时计数，缺列时退回批次记录的生成数量。 */
export function cardCountOf(row?: CountableBatch | null): number {
  if (!row) return 0;
  return count(row.cardCount ?? row.successCount);
}

/**
 * 三组数字。后端 `meta.counts` 是整表口径（与分页无关）；缺失时按当前页的行兜底，
 * 兜底口径只覆盖当前页，但「已登记 + 未登记 = 全部」的关系恒成立。
 */
export function batchCounts(
  rows: CountableBatch[] = [],
  meta?: BatchListMeta | null,
): BatchCounts {
  const server = meta?.counts;
  if (server && (server.registeredCards != null || server.unassignedCards != null)) {
    const registered = count(server.registeredCards);
    const unassigned = count(server.unassignedCards);
    const total = count(server.totalCards);
    return { registered, unassigned, total: total || registered + unassigned };
  }
  let registered = 0;
  let unassigned = 0;
  for (const row of rows || []) {
    if (isUnassignedBatch(row)) unassigned += cardCountOf(row);
    else registered += cardCountOf(row);
  }
  return { registered, unassigned, total: registered + unassigned };
}

export function sameBatchCounts(left?: BatchCounts, right?: BatchCounts): boolean {
  return (
    !!left &&
    !!right &&
    left.registered === right.registered &&
    left.unassigned === right.unassigned &&
    left.total === right.total
  );
}

/**
 * 页面说明文案：三个数字必须同时出现，运营才能一眼把批次页与「卡密管理」页对上。
 * 数据还没到时只说明口径，不显示 0/0/0 这种会误导的「合计」。
 */
export function batchCountsText(counts?: BatchCounts | null): string {
  const tail = '再次导出返回原批次卡密；有效期从生成开始，不随导出延长。';
  const readOnly = `「${UNASSIGNED_BATCH_LABEL}」是历史导入或人工创建、没有批次归属的卡密，只读列出，不提供批次导出与作废。`;
  if (!counts || counts.total <= 0)
    return `本页按套餐列出卡密批次，并把没有批次归属的卡密单列成「${UNASSIGNED_BATCH_LABEL}」。${readOnly} ${tail}`;
  return (
    `已登记批次 ${counts.registered} 张（本站生成 / 粘贴导入）+ ` +
    `${UNASSIGNED_BATCH_LABEL} ${counts.unassigned} 张 = 全部 ${counts.total} 张卡密，` +
    `与「卡密管理」页的「总数」同一口径。${readOnly} ${tail}`
  );
}

/** 未登记批次的原始批次名（邀请卡密表的 `batch` 列，如 `20261007-014828` / `manual`）。 */
export function originBatchText(row?: { originBatches?: OriginBatch[] } | null): string {
  return (row?.originBatches || [])
    .map((item) => {
      const name = String(item?.batch ?? '').trim() || '未填写批次名';
      return `${name}（${count(item?.total)} 张）`;
    })
    .join('、');
}

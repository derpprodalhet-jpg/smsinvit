/**
 * 「兑换工作台」的冻结接口契约：`GET /api/v1/admin/stats/overview`（权限 inventory.read）。
 *
 * 页面只认这一份结构；本文件里的纯函数负责**排序、分组、口径文案**，
 * 让页面/组件不重复实现，也让断言能直接打在纯函数上。
 *
 * 契约要点：
 *   - `cards.modules[]` 顺序固定：接码 / Codex 邀请；
 *   - `costs[]` 金额是 minor 整数 + currency，同一 key 可能多行（不同币种），
 *     绝不做跨币种合计；
 *   - `available:false` → 金额显示 `—`；`availableSince` → 说明起始日期；
 *     `unpricedCount>0` → 标注「N 条缺金额，未计入」。
 */
import type { AdminTone } from '../components/admin-feedback';
import { formatMinor } from './money';

export type StatsOverviewModuleKey = 'sms' | 'invitation';

export type StatsOverviewModule = {
  module: string;
  label: string;
  generatedTotal: number | null;
  generatedToday: number | null;
  redeemedTotal: number | null;
  redeemedToday: number | null;
  expired: number | null;
  expiredApplicable: boolean;
};

export type StatsOverviewTotals = {
  generatedTotal: number | null;
  generatedToday: number | null;
  redeemedTotal: number | null;
  redeemedToday: number | null;
  expired: number | null;
};

export type StatsOverviewCost = {
  key: string;
  label: string;
  currency: string;
  totalMinor: number | null;
  todayMinor: number | null;
  basis?: string | null;
  available: boolean;
  availableSince?: string | null;
  unpricedCount?: number | null;
  detailPath?: string | null;
};

export type StatsOverviewPending = {
  key: string;
  label: string;
  count: number | null;
  detailPath?: string | null;
};

export type StatsOverviewCards = {
  modules: StatsOverviewModule[];
  totals: StatsOverviewTotals | null;
};

export type StatsOverview = {
  businessTimeZone: string;
  generatedAt: string;
  cards: StatsOverviewCards;
  costs: StatsOverviewCost[];
  pending: StatsOverviewPending[];
};

/** 契约里模块的展示顺序；表头与数据行都按它排。 */
export const MODULE_ORDER: readonly string[] = ['sms', 'invitation'];

/**
 * 模块 → 明细页。只用目标页**已经支持**的入口，不编造筛选参数：
 * 目前两个页面都没有「今日 / 已过期 / 生成」维度的 URL 参数，所以这里只给页面本身。
 * 需要补的参数见交付报告的「下钻参数缺口」。
 */
export const MODULE_DETAIL_PATH: Record<string, string> = {
  sms: '/sms/redeems',
  invitation: '/invitation/cdk',
};

export const TOTAL_MODULE_KEY = '__total';
export const TOTAL_LABEL = '合计';

/** Codex 邀请卡密没有有效期概念：显示「不适用」，不显示 0。 */
export const EXPIRED_NOT_APPLICABLE = '不适用';
export const EXPIRED_NOT_APPLICABLE_TOOLTIP = '该模块卡密没有有效期概念';
export const COST_UNAVAILABLE_NOTE = '该项成本暂不可用：后端尚未提供可用的数据口径';

export const LEDGER_COLUMN_HINTS = {
  generatedTotal: '该模块自上线以来生成的卡密总数（含已核销与已过期）。',
  generatedToday: '业务时区内当日生成的卡密数。',
  redeemedTotal: '该模块卡密进入已核销终态的数量。',
  redeemedToday: '业务时区内当日进入已核销终态的数量。',
  expired: '已过期 = expires_at < 现在 且未核销。',
  expiredNotApplicable: EXPIRED_NOT_APPLICABLE_TOOLTIP,
} as const;

export const COST_COLUMN_HINTS = {
  totalMinor: '该成本项自可用之日起累计的已确认金额（按币种分别统计，不做跨币种合计）。',
  todayMinor: '业务时区内当日已确认金额；未确认的成本不计入。',
  basis: '口径 = 该成本项的统计范围与确认条件。',
} as const;

/** 模块排序：契约顺序在前，未知模块保持响应里的相对顺序排在最后。 */
export function orderModules(modules: StatsOverviewModule[]): StatsOverviewModule[] {
  const rank = (module: string) => {
    const index = MODULE_ORDER.indexOf(module);
    return index === -1 ? MODULE_ORDER.length : index;
  };
  return modules
    .map((module, index) => ({ module, index }))
    .sort((a, b) => rank(a.module.module) - rank(b.module.module) || a.index - b.index)
    .map((entry) => entry.module);
}

export function moduleDetailPath(module: string): string | undefined {
  return MODULE_DETAIL_PATH[module];
}

/**
 * costs 分组：同 key 的行放在一起，组内按币种升序；
 * key 的组间顺序保持响应里的首次出现顺序（后端已按业务排序）。
 */
export function groupCostsByKey(costs: StatsOverviewCost[]): StatsOverviewCost[] {
  const groups = new Map<string, StatsOverviewCost[]>();
  for (const cost of costs) {
    const key = cost.key ?? '';
    const bucket = groups.get(key);
    if (bucket) bucket.push(cost);
    else groups.set(key, [cost]);
  }
  const ordered: StatsOverviewCost[] = [];
  for (const bucket of groups.values()) {
    ordered.push(
      ...[...bucket].sort((a, b) =>
        String(a.currency ?? '').localeCompare(String(b.currency ?? '')),
      ),
    );
  }
  return ordered;
}

/** 成本行的口径说明（悬浮）：不可用原因 / 起始日期 / 缺金额条数。 */
export function costAvailabilityNotes(cost: StatsOverviewCost): string[] {
  const notes: string[] = [];
  if (!cost.available) notes.push(COST_UNAVAILABLE_NOTE);
  if (cost.availableSince) notes.push(`自 ${cost.availableSince} 起才有记录`);
  const unpriced = cost.unpricedCount ?? 0;
  if (unpriced > 0) notes.push(`${unpriced} 条缺金额，未计入`);
  return notes;
}

/** 金额单元格的悬浮文案（无说明时返回 undefined，避免空 Tooltip）。 */
export function costAmountTooltip(cost: StatsOverviewCost): string | undefined {
  const notes = costAvailabilityNotes(cost);
  return notes.length ? notes.join('；') : undefined;
}

/** 金额单元格文本：不可用或缺失一律 `—`，绝不显示 0。 */
export function costAmountText(cost: StatsOverviewCost, which: 'total' | 'today'): string {
  if (!cost.available) return '—';
  return formatMinor(which === 'total' ? cost.totalMinor : cost.todayMinor, cost.currency);
}

/** 缺金额的可见提示文案（unpricedCount>0 时显示在口径列）。 */
export function costUnpricedNote(cost: StatsOverviewCost): string | undefined {
  const unpriced = cost.unpricedCount ?? 0;
  return unpriced > 0 ? `${unpriced} 条缺金额，未计入` : undefined;
}

/** 口径列的悬浮文案：口径说明 + 可用性说明。 */
export function costBasisTooltip(cost: StatsOverviewCost): string | undefined {
  const parts = [cost.basis?.trim(), ...costAvailabilityNotes(cost)].filter(Boolean) as string[];
  return parts.length ? parts.join('；') : undefined;
}

/** 待处理的强调色：需人工/异常类用 error，其余有量用 warning，0 用弱化色。 */
export function pendingTone(key: string, count: number | null | undefined): AdminTone {
  if ((count ?? 0) <= 0) return 'default';
  return /(manual|uncertain|unmatched|review|fail|error)/.test(String(key ?? '')) ? 'error' : 'warning';
}

/** 计数展示：缺失 `—`，其余千分位；绝不把缺失当成 0。 */
export function formatCount(value: number | null | undefined): string {
  if (value == null) return '—';
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric)) return '—';
  return numeric.toLocaleString('en-US');
}

/**
 * 契约校验：结构不完整就抛错，让页面走「失败 + 重试」而不是展示一张全是 0 的表。
 */
export function parseStatsOverview(raw: unknown): StatsOverview {
  const data = raw as Partial<StatsOverview> | null | undefined;
  const cards = data?.cards;
  if (
    !data ||
    typeof data !== 'object' ||
    !cards ||
    !Array.isArray(cards.modules) ||
    !Array.isArray(data.costs) ||
    !Array.isArray(data.pending)
  ) {
    throw new Error('统计接口返回结构不完整，无法展示台账；请重试或检查后端版本。');
  }
  return {
    businessTimeZone: data.businessTimeZone || '',
    generatedAt: data.generatedAt || '',
    cards: { modules: cards.modules, totals: cards.totals ?? null },
    costs: data.costs,
    pending: data.pending,
  };
}

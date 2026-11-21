import { resolveLabel } from './zh-label';
import type { StatusTone } from './zh-maps';

export const PRODUCT_KIND_LABEL: Record<string, string> = {
  SMS_REDEEM: '接码',
  CODEX_INVITE_REDEEM: 'Codex 邀请',
};

/**
 * 运行记录的类型。账号工厂的补号必须和人工「单次注册」分开显示 ——
 * 否则开了自动补号之后，运行记录里一排「注册」分不清哪条是自动跑的。
 */
export const RUN_KIND_LABEL: Record<string, string> = {
  register: '单次注册',
  register_pool: '自动补号',
  codex_auth: '邀请授权',
};
export function runKindLabel(kind?: string): string {
  if (!kind) return '—';
  return resolveLabel(RUN_KIND_LABEL, kind);
}

/**
 * 卡密状态（邀请卡密列表 / 卡密台账 / 使用记录）。
 *
 * 运营口径：卡密的终态一律显示「已核销」而不是「已完成」——后端把卡密被兑换
 * 也叫「卡密核销完成」，四个模块统一用这个词。
 *
 * ⚠️ 这张表**只服务卡密**。运行记录 / 作业的同一组枚举请用
 * `RUN_JOB_STATUS_LABEL`（`done` 仍是「已完成」），持久任务与定时任务的
 * 状态在 `zh-maps.ts`（`JOB_STATUS_LABEL` / `SCHEDULER_STATUS_LABEL`），
 * 都**不**受这里的改动影响。
 */
export const RUN_STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  done: '已核销',
  failed: '失败',
  unused: '未使用',
  bound: '已绑定',
  void: '已作废',
};

/**
 * 运行 / 作业状态：与卡密状态同枚举，但 `done` 是「任务跑完」而不是
 * 「卡密被核销」，所以单独一张表保留「已完成」。
 */
export const RUN_JOB_STATUS_LABEL: Record<string, string> = {
  ...RUN_STATUS_LABEL,
  done: '已完成',
};

/** 卡密状态配色：未使用=橘黄（四个卡密模块一致），终态=绿，失败=红。 */
export const CDK_STATUS_TONE: Record<string, StatusTone> = {
  unused: 'warning',
  bound: 'default',
  queued: 'processing',
  running: 'processing',
  done: 'success',
  failed: 'error',
  void: 'default',
  review: 'warning',
};

/** 卡密状态文案（终态「已核销」，未命中走「未知（原始值）」）。 */
export function cdkStatusLabel(status?: string | null): string {
  return resolveLabel(RUN_STATUS_LABEL, status);
}

/** 卡密状态配色（未使用=橘黄）。 */
export function cdkStatusTone(status?: string | null): StatusTone {
  return CDK_STATUS_TONE[String(status ?? '').trim()] || 'default';
}

/** 运行 / 作业状态文案（`done` = 已完成，任务类状态不受卡密口径影响）。 */
export function runStatusLabel(status?: string | null): string {
  return resolveLabel(RUN_JOB_STATUS_LABEL, status);
}

/** 任务类型的标签色：运行记录里一眼区分"补号 / 兑换授权 / 手动注册"。 */
export const RUN_KIND_TONES: Record<string, string> = {
  register_pool: 'cyan',
  codex_auth: 'purple',
  register: 'blue',
};

export function runKindTone(kind?: string): string {
  return RUN_KIND_TONES[String(kind || '')] || 'default';
}

/** 运行记录「任务类型」筛选可选项（'' = 全部）。 */
export function runKindOptions(): { value: string; label: string }[] {
  return [
    { value: '', label: '全部类型' },
    ...Object.keys(RUN_KIND_LABEL).map((value) => ({ value, label: RUN_KIND_LABEL[value] })),
  ];
}


/** 账号池「是否接码」：注册阶段顺手验手机的结果。 */
export function phoneBoundLabel(value: unknown): string {
  if (value === 1 || value === '1' || value === true) return '已接码';
  if (value === 0 || value === '0') return '接码失败';
  return '未接码（兑换时到 add-phone）';
}

/**
 * 列表用的短标签：只有「已接 / 未接」两个词，原因放 Tooltip。
 * 长文案（`phoneBoundLabel`）留给详情抽屉，列表里三个字会把列撑开。
 */
export function phoneBoundShort(value: unknown): {
  label: string;
  tone: 'success' | 'error' | 'default';
  hint: string;
} {
  if (value === 1 || value === '1' || value === true)
    return { label: '已接', tone: 'success', hint: '已接码：注册时验过手机号' };
  if (value === 0 || value === '0')
    return { label: '未接', tone: 'error', hint: '接码失败：注册时验手机没通过' };
  return { label: '未接', tone: 'default', hint: '未接码：兑换时到 add-phone 页面验证' };
}

export function phoneBoundTone(value: unknown): 'success' | 'error' | 'warning' {
  if (value === 1 || value === '1' || value === true) return 'success';
  if (value === 0 || value === '0') return 'error';
  return 'warning';
}


/** 账号行是否真的走过代理：-1 未记录（历史）/ 1 有代理 / 0 直连。 */
export function proxyUsageLabel(used: unknown): string {
  if (used === 1 || used === '1') return '已用代理';
  if (used === 0 || used === '0') return '直连';
  return '未记录';
}

export function proxyUsageTone(used: unknown): 'success' | 'default' | 'warning' {
  if (used === 1 || used === '1') return 'success';
  if (used === 0 || used === '0') return 'default';
  return 'warning';
}

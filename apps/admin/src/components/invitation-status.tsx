import { Tooltip } from 'antd';
import { AdminStatusTag, toneFromStatus, type AdminTone } from './admin-feedback';
import { resolveLabel } from '../lib/zh-label';
import { cdkStatusLabel, cdkStatusTone } from '../lib/invitation';
/**
 * 通用状态文案（运行记录 / 代理可用性 / Plus 检测 / 邮箱…）。
 *
 * ⚠️ 卡密列表**不**用这张表：`context="cdk"` 走 `lib/invitation.ts` 的卡密口径
 * （终态「已核销」、未使用橘黄）。这里的 `done: '已完成'` 是「任务/运行跑完」，
 * `consumed` / `issued` 等也是通用枚举，不是卡密终态，所以保持原样。
 */
const LABELS: Record<string, string> = {
  available: '可用',
  in_use: '使用中',
  registered: '待兑换',
  done: '已完成',
  failed: '失败',
  unused: '未使用',
  bound: '已绑定',
  queued: '排队中',
  running: '运行中',
  review: '待复核',
  void: '已作废',
  ready: '可用',
  frozen: '冻结中',
  dead: '永久不可用',
  disabled: '已禁用',
  success: '成功',
  partial: '部分完成',
  cancelled: '已取消',
  expired: '已过期',
  locked: '锁定中',
  consumed: '已使用',
  issued: '已发行',
  invalid: '已作废',
  pending: '等待中',
};
/**
 * 邮箱管理里的三段身份，按**账号走到哪一步**命名，而不是按任务状态：
 *   邮箱    只有邮箱，还没注册 GPT
 *   GPT    已注册 GPT、绑了 2FA，但还没做 Codex 授权 ← 唯一可兑换的号
 *   Codex  已经完成 Codex 授权（有 RT），邀请流程已跑完
 *
 * 运营明确要求这一列只有这三个词（列表一屏能看几十行，长标签会把列撑开）。
 * 「使用中 / 失败」是任务状态而不是身份，改成颜色 + 悬浮说明；这两态的账号
 * 真实身份（是邮箱还是 GPT）库里并没有记录，所以统一按「邮箱」显示并在
 * 悬浮里说清不确定性，不猜一个可能是假的身份。
 */
const MAIL_ACCOUNT_LABELS: Record<string, string> = {
  available: '邮箱',
  in_use: '邮箱',
  failed: '邮箱',
  registered: 'GPT',
  done: 'Codex',
};

const MAIL_ACCOUNT_HINTS: Record<string, string> = {
  available: '邮箱：只有邮箱，还没注册 GPT',
  in_use: '使用中：任务正在执行，完成后会变成 GPT 或 Codex（当前未记录它的真实身份）',
  failed: '上次任务失败：点「详情」看失败原因（当前未记录它的真实身份）',
  registered: 'GPT：已注册并绑好 2FA，可兑换（Codex 授权后会变成 Codex）',
  done: 'Codex：已完成 Codex 授权（有 RT）',
};
/**
 * 三段身份**必须一眼分得开**，所以这里显式指定颜色，不走通用 toneFromStatus ——
 * 通用规则把 available 和 done 都归成绿色，结果「邮箱」和「Codex」同色，
 * 看列表根本分不出走到哪一步了。
 *   邮箱 灰 / 使用中 橙 / GPT 蓝（可兑换）/ Codex 绿 / 上次失败 红
 */
const MAIL_ACCOUNT_TONES: Record<string, AdminTone> = {
  available: 'default',
  in_use: 'warning',
  registered: 'processing',
  done: 'success',
  failed: 'error',
};
export function InvitationStatus({
  status,
  context,
}: {
  status?: string;
  /**
   * `mail-account` 用邮箱管理的三段身份词；`cdk` 用卡密口径
   * （终态「已核销」、未使用橘黄）。不传就是运行/作业等通用状态。
   */
  context?: 'mail-account' | 'cdk';
}) {
  const key = String(status || '').toLowerCase();
  const tone =
    context === 'mail-account'
      ? MAIL_ACCOUNT_TONES[key] || 'default'
      : context === 'cdk'
        ? cdkStatusTone(key)
        : key === 'ready'
          ? 'success'
          : key === 'registered'
            ? 'processing'
            : key === 'frozen' || key === 'partial'
              ? 'warning'
              : key === 'dead'
                ? 'error'
                : toneFromStatus(key);
  // 未知状态不再回退成原始英文，统一显示「未知（原始值）」。
  const label =
    context === 'mail-account'
      ? resolveLabel({ ...LABELS, ...MAIL_ACCOUNT_LABELS }, key)
      : context === 'cdk'
        ? // 卡密列表：终态「已完成」按运营口径统一成「已核销」，且未使用橘黄。
          cdkStatusLabel(key)
        : resolveLabel(LABELS, key);
  const tag = <AdminStatusTag tone={tone}>{label}</AdminStatusTag>;
  if (context !== 'mail-account') return tag;
  // 列表只保留三个身份词，任务状态（使用中/失败）与含义放悬浮说明。
  const hint = MAIL_ACCOUNT_HINTS[key];
  return hint ? <Tooltip title={hint}>{tag}</Tooltip> : tag;
}

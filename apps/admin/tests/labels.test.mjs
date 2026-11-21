/**
 * 中文标签回归。
 *
 * 起因：列表里的「错误 / 结果」等列会冒出英文——所有映射在未命中时都
 * `map[key] || key`，数据库出现新枚举就直接显示英文。这里钉住三件事：
 *   1. 未命中一律返回「未知（原始值）」，绝不返回裸英文；
 *   2. 已确诊的枚举全部有中文；
 *   3. 曾经会抛异常的两处标签映射（卡密同步、接码自检）有兜底分支。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

function compile(url) {
  return ts.transpileModule(readFileSync(url, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
}

function load(url, dependencies = {}) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compile(url))(
    (name) => {
      if (name in dependencies) return dependencies[name];
      throw new Error(`unexpected require(${name}) from ${url.pathname}`);
    },
    module,
    module.exports,
  );
  return module.exports;
}

const zhLabel = load(new URL('../src/lib/zh-label.ts', import.meta.url));
const zhMaps = load(new URL('../src/lib/zh-maps.ts', import.meta.url), {
  './zh-label': zhLabel,
});
const zhText = load(new URL('../src/lib/zh-text.ts', import.meta.url), {
  './zh-maps': zhMaps,
});
const labels = load(new URL('../src/lib/labels.ts', import.meta.url), {
  './zh-label': zhLabel,
});
const invitation = load(new URL('../src/lib/invitation.ts', import.meta.url), {
  './zh-label': zhLabel,
  // 允许这些模块自由引用共用映射/文案工具，不要因为测试加载器的严格性
  // 而限制生产代码的依赖方向。
  './zh-maps': zhMaps,
  './zh-text': zhText,
});

const CJK = /[\u4e00-\u9fff]/;
/** 品牌名与产品名允许保留原文（不是英文文案，是专有名词）。 */
const ASCII_ALLOWED = new Set([
  'Go',
  'Plus',
  'Telegram',
  'Webhook',
  // 邮箱来源是产品名：Outlook / iCloud / IMAP 没有可读的中文译名。
  'Outlook',
  'iCloud',
  'IMAP',
]);

test('未知枚举显示为「未知（原始值）」，不是裸英文', () => {
  assert.equal(zhLabel.unknownLabel('WEIRD_STATE'), '未知（WEIRD_STATE）');
  assert.equal(zhLabel.unknownLabel('  '), '—');
  assert.equal(zhLabel.unknownLabel(null), '—');
  assert.equal(zhLabel.resolveLabel({ A: '甲' }, 'B'), '未知（B）');
  assert.equal(zhLabel.resolveLabel({ A: '甲' }, 'A'), '甲');
  assert.equal(zhLabel.resolveLabel({ A: '甲' }, '', '空值占位'), '空值占位');
  assert.equal(
    zhLabel.resolveLabelInsensitive({ Abandoned: '已放弃' }, 'ABANDONED'),
    '已放弃',
  );
  assert.equal(zhLabel.resolveLabelInsensitive({ Abandoned: '已放弃' }, 'other'), '未知（other）');
});

test('原本用原始英文兜底的调用点不再漏英文', () => {
  // labels.ts 的 labelOf：调用方仍会传原始值当 fallback，必须被忽略。
  assert.equal(labels.labelOf({ A: '甲' }, 'B', 'B'), '未知（B）');
  assert.equal(labels.labelOf({ A: '甲' }, '', '未执行'), '未执行');
  assert.equal(labels.labelOf({ A: '甲' }, 'A'), '甲');
  assert.equal(invitation.runKindLabel('weird_kind'), '未知（weird_kind）');
  assert.equal(invitation.runKindLabel(''), '—');
  assert.equal(invitation.runStatusLabel('running'), '运行中');
  assert.equal(invitation.runStatusLabel('weird'), '未知（weird）');
  // X 的失败步骤与状态。
});

test('卡密状态有中文', () => {
  assert.equal(labels.labelOf(labels.SMS_KEY_STATUS_LABEL, 'UNUSED'), '未使用');
  assert.equal(labels.smsKeyStatusLabel('USED'), '已核销');
  assert.equal(invitation.cdkStatusLabel('unused'), '未使用');
});

test('错误码映射成中文，未知错误码不显示裸英文', () => {
  assert.equal(zhMaps.moduleErrorLabel('SMS_CDK_NOT_FOUND'), '卡密无效或不存在');
  assert.equal(zhMaps.moduleErrorLabel('TOTALLY_UNKNOWN_CODE'), '未知（TOTALLY_UNKNOWN_CODE）');
  assert.equal(zhMaps.moduleErrorLabel(''), '—');
  assert.equal(zhMaps.moduleErrorLabel('INVITATION_RUNNING'), '有执行中的任务，不能修改');
  assert.equal(zhMaps.moduleErrorLabel(null), '—');
  // 额外表优先于模块表。
  assert.equal(
    zhMaps.moduleErrorLabel('SMS_CDK_NOT_FOUND', { SMS_CDK_NOT_FOUND: '自定义说明' }),
    '自定义说明',
  );
});

test('审计、通知、定时任务、持久任务与历史字段都有中文', () => {  assert.equal(zhMaps.auditActionLabel('inventory.generate'), '生成卡密');
  assert.equal(zhMaps.auditResourceLabel('inventory_batch'), '卡密批次');
  assert.equal(zhMaps.notificationStatusLabel('SENT'), '已发送');
  assert.equal(zhMaps.notificationChannelLabel('EMAIL'), '邮件');
  assert.equal(zhMaps.jobStatusLabel('FAILED'), '已失败');
  assert.equal(zhMaps.schedulerStatusLabel('STALE'), '可能停滞');
  assert.equal(zhMaps.staffRoleLabel('SUPER_ADMIN'), '超级管理员');
  assert.equal(zhMaps.legacyFieldLabel('verified_at'), '核验时间');
  assert.equal(zhMaps.cdkSyncLabel('failed'), '同步失败');
  assert.equal(zhMaps.holdReasonLabel('ADMIN_STOPPED'), '管理员手动停止');
  assert.equal(zhMaps.auditActionLabel('never.heard.of.it'), '未知（never.heard.of.it）');
});

test('每个导出的标签映射都不含纯英文文案', () => {
  const groups = [
    ['zh-maps', zhMaps],
    ['labels', labels],
    ['invitation', invitation],
  ];
  const offenders = [];
  for (const [group, module] of groups) {
    for (const [name, value] of Object.entries(module)) {
      if (!name.endsWith('_LABEL') || !value || typeof value !== 'object') continue;
      for (const [key, text] of Object.entries(value)) {
        if (typeof text !== 'string' || !text) continue;
        if (CJK.test(text) || ASCII_ALLOWED.has(text)) continue;
        offenders.push(`${group}.${name}[${key}] = ${text}`);
      }
    }
  }
  assert.deepEqual(offenders, []);
});

test('曾经会抛异常的标签映射现在有兜底分支', () => {
  const sync = readFileSync(
    new URL('../src/pages/invitation/runs/cdk-sync.tsx', import.meta.url),
    'utf8',
  );
  assert.match(sync, /resolveLabel|unknownLabel|SYNC_STATUS\[status\]\s*\|\|/);
  const diagnostics = readFileSync(
    new URL('../src/pages/invitation/settings/diagnostics.tsx', import.meta.url),
    'utf8',
  );
  assert.match(diagnostics, /resolveLabel|unknownLabel|\?\?|BALANCE_STATUS\[status\]\?\./);
  // 状态标签组件不能再把原始英文状态当文案。
  const status = readFileSync(
    new URL('../src/components/invitation-status.tsx', import.meta.url),
    'utf8',
  );
  assert.doesNotMatch(status, /label \|\| status/);
});

test('后端原文既可能是错误码也可能是中文说明时，两种都不漏英文', () => {
  // 已经是中文说明：原样保留，不能包成「未知（…）」。
  assert.equal(zhText.localizedText('代理认证未通过，请检查认证信息'), '代理认证未通过，请检查认证信息');
  // 是错误码：翻成中文。
  assert.equal(zhText.localizedText('SMS_CDK_NOT_FOUND'), '卡密无效或不存在');
  assert.equal(zhText.localizedText('ADMIN_CANCELLED'), '管理员取消');
  // 未确诊的纯英文：中文兜底，不输出裸英文。
  assert.equal(zhText.localizedText('SOME_NEW_CODE'), '未知（SOME_NEW_CODE）');
  // 空值。
  assert.equal(zhText.localizedText(''), '—');
  assert.equal(zhText.localizedText(null), '—');
  assert.equal(zhText.localizedText('   ', '未执行'), '未执行');
});

test('接码与邀请的卡密终态统一为「已核销」，任务类状态保持「已完成」', () => {
  // 运营口径：卡密「已完成 / 已使用」都改成「已核销」。
  assert.equal(invitation.RUN_STATUS_LABEL.done, '已核销');
  assert.equal(invitation.cdkStatusLabel('done'), '已核销');
  assert.equal(labels.SMS_KEY_STATUS_LABEL.USED, '已核销');
  assert.equal(labels.smsKeyStatusLabel('USED'), '已核销');
  assert.equal(zhMaps.inventoryStatusLabel('CONSUMED'), '已核销');
  // 卡密列表里不再出现旧的终态文案。
  for (const text of ['已开通', '已使用'])
    for (const map of [labels.SMS_KEY_STATUS_LABEL, invitation.RUN_STATUS_LABEL])
      assert.ok(!Object.values(map).includes(text), `${text} 不应再出现在卡密状态映射里`);

  // 故意没改：运行 / 作业的设备状态仍是「已完成」（`done` 是任务跑完，不是卡密核销）。
  assert.equal(invitation.runStatusLabel('done'), '已完成');
  // 故意没改：持久任务与定时任务的状态。
  assert.equal(zhMaps.jobStatusLabel('SUCCEEDED'), '已完成');
  assert.equal(zhMaps.schedulerStatusLabel('RUNNING'), '正在执行');
  // 故意没改：邀请卡密的 `bound`（已绑定账号）不是终态，保留可区分的信息。
  assert.equal(invitation.cdkStatusLabel('bound'), '已绑定');
});

test('接码与邀请的卡密「未使用」都是橘黄色 tone', () => {
  assert.equal(labels.SMS_KEY_STATUS_TONE.UNUSED, 'warning');
  assert.equal(labels.smsKeyStatusTone('UNUSED'), 'warning');
  assert.equal(invitation.CDK_STATUS_TONE.unused, 'warning');
  assert.equal(invitation.cdkStatusTone('unused'), 'warning');
  assert.equal(zhMaps.INVENTORY_STATUS_TONE.ISSUED, 'warning');
  assert.equal(zhMaps.inventoryStatusTone('ISSUED'), 'warning');
  // 终态仍然是绿的，别把「已核销」也染成橘黄。
  for (const tone of [
    labels.smsKeyStatusTone('USED'),
    invitation.cdkStatusTone('done'),
    zhMaps.inventoryStatusTone('CONSUMED'),
  ])
    assert.equal(tone, 'success');
});

test('未确诊的库存状态不显示英文裸值', () => {
  assert.equal(zhMaps.inventoryStatusLabel('INVENTORY_NEW'), '未知（INVENTORY_NEW）');
  assert.equal(zhMaps.inventoryStatusLabel(''), '—');
  assert.equal(zhMaps.inventoryStatusTone('INVENTORY_NEW'), 'default');
});

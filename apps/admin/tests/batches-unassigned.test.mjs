/**
 * 「卡密批次」页必须覆盖全部卡密，且说明文案里的三个数字能对上。
 *
 * 事故：批次页只列 `inventory_import_batches` 的行（CODEX 套餐 10 张），
 * 「卡密管理」页列全部（21 张），运营看到两个互相矛盾的数字。后端为此补了保留 id 的
 * 「未登记批次」行并提供 `meta.counts`；这里钉住前端口径：已登记 + 未登记 = 全部。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const read = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');

function load(relative) {
  const path = new URL(relative, import.meta.url);
  const require = createRequire(path);
  const module = { exports: {} };
  const code = ts.transpileModule(readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  new Function('require', 'module', 'exports', code)(require, module, module.exports);
  return module.exports;
}

const model = load('../src/pages/batches/model.ts');

/** 真实实况（只读查库得到）：CODEX 套餐 10 张已登记 + 11 张未登记 = 21 张。 */
const REGISTERED = [
  { id: 'c5388f7f-e836-49d8-b267-b267a0906a15', cardCount: 10, successCount: 10 },
];
const UNASSIGNED = {
  id: 'unassigned',
  skuId: '0e753535-58aa-4a17-a224-d4115443bd67',
  cardCount: 11,
  successCount: null,
  unassigned: true,
  batchNo: '',
  originBatches: [
    { batch: '20261007-014828', total: 10 },
    { batch: 'manual', total: 1 },
  ],
};
const META = {
  total: 2,
  counts: { registeredCards: 10, unassignedCards: 11, totalCards: 21 },
};

test('未登记批次行是只读伪批次，张数等于没有批次归属的卡密数', () => {
  assert.equal(model.UNASSIGNED_BATCH_ID, 'unassigned');
  assert.equal(model.UNASSIGNED_BATCH_LABEL, '未登记批次');
  assert.equal(model.isUnassignedBatch(UNASSIGNED), true);
  assert.equal(model.isUnassignedBatch(REGISTERED[0]), false);
  assert.equal(model.isUnassignedBatch(null), false);
  // 后端只把这个 id 当保留值；前端也不能把它当成批次号导出。
  assert.equal(model.canExportBatch(UNASSIGNED), false);
  assert.equal(model.canExportBatch(REGISTERED[0]), true);
  assert.equal(model.cardCountOf(UNASSIGNED), 11);
});

test('批次列表张数合计 + 未登记张数 = 全部卡密数（页面与卡密管理页对得上）', () => {
  const rows = [...REGISTERED, UNASSIGNED];
  const counts = model.batchCounts(rows, META);
  assert.deepEqual(counts, { registered: 10, unassigned: 11, total: 21 });
  // 未登记张数必须等于「全部 - 已登记」，这正是运营核对的算式。
  assert.equal(counts.unassigned, counts.total - counts.registered);
  const listed = rows.reduce((sum, row) => sum + model.cardCountOf(row), 0);
  assert.equal(listed, counts.total);
});

test('说明文案里三个数字同时出现且能对上，并写明与卡密管理页同口径', () => {
  const text = model.batchCountsText(model.batchCounts([...REGISTERED, UNASSIGNED], META));
  const matched = text.match(
    /已登记批次 (\d+) 张[^+]*\+ 未登记批次 (\d+) 张[^=]*= 全部 (\d+) 张/,
  );
  assert.ok(matched, `说明文案缺少三组数字的关系：${text}`);
  const [, registered, unassigned, total] = matched.map(Number);
  assert.equal(registered + unassigned, total);
  assert.deepEqual([registered, unassigned, total], [10, 11, 21]);
  assert.match(text, /「卡密管理」页的「总数」同一口径/);
  assert.match(text, /只读/);
  assert.match(text, /不提供批次导出与作废/);
});

test('分页兜底：后端 counts 缺列时按当前页行求和，关系仍然成立', () => {
  const counts = model.batchCounts([...REGISTERED, UNASSIGNED], { total: 2 });
  assert.deepEqual(counts, { registered: 10, unassigned: 11, total: 21 });
  // 后端整表口径优先于当前页：翻到第二页时数字不能变小。
  const pageTwo = model.batchCounts([UNASSIGNED], META);
  assert.deepEqual(pageTwo, { registered: 10, unassigned: 11, total: 21 });
  assert.equal(model.sameBatchCounts(pageTwo, counts), true);
  assert.equal(model.sameBatchCounts(pageTwo, { ...counts, total: 22 }), false);
  // 没数据时说明口径，而不是显示 0/0/0 这种会误导的合计。
  const empty = model.batchCountsText({ registered: 0, unassigned: 0, total: 0 });
  assert.doesNotMatch(empty, /0 张/);
  assert.match(empty, /未登记批次/);
});

test('未登记批次如实显示原始批次名，不伪造批次号', () => {
  assert.equal(model.originBatchText(UNASSIGNED), '20261007-014828（10 张）、manual（1 张）');
  assert.equal(model.originBatchText({ originBatches: [] }), '');
  assert.equal(model.originBatchText({}), '');
  assert.equal(
    model.originBatchText({ originBatches: [{ batch: '', total: 3 }] }),
    '未填写批次名（3 张）',
  );
});

test('批次页渲染未登记批次行、按只读处理，并把套餐带进卡密查询', () => {
  const page = read('../src/pages/batches/page.tsx');
  assert.match(page, /from '\.\/model'/, '页面必须复用批次口径模型');
  assert.match(page, /UNASSIGNED_BATCH_LABEL/, '未登记批次行必须用统一的名称');
  assert.match(page, /isUnassignedBatch\(row\)/, '批次号列必须区分未登记批次行');
  assert.match(page, /canExportBatch\(row\)/, '未登记批次行的操作列必须只留只读入口');
  assert.match(page, /batchCountsText\(counts\)/, '页面说明必须由三组数字渲染出来');
  assert.match(page, /batchCounts\(r\.data \|\| \[\], r\.meta\)/, '三组数字必须取后端整表口径');
  // 没选套餐时不能留着上一个套餐的数字。
  assert.match(page, /sameBatchCounts\(prev, EMPTY_BATCH_COUNTS\)/, '未选套餐时必须清空三组数字');
  assert.match(page, /if \(isUnassignedBatch\(row\) && row\.skuId\) q\.set\('skuId'/, '未登记批次卡密查询必须带套餐');
  assert.match(page, /!detailReadOnly && row\.status === 'ISSUED'/, '未登记批次不得提供逐卡作废');
  assert.match(page, /const detailReadOnly = isUnassignedBatch\(detail\)/);
  // 保留 id 只能出现在 model 里，页面不得再硬编码一份。
  assert.doesNotMatch(page, /'unassigned'/, '保留 id 必须只有一处定义');
  // 不得写死某个套餐的张数（换套餐就会撒谎）。
  assert.doesNotMatch(page, /全部 21 张|12 张/);
});

test('卡密管理页写清「总数」与「可兑换账号」的口径', () => {
  const page = read('../src/pages/invitation/cdk/page.tsx');
  assert.match(page, /「总数」是本模块全部卡密/);
  assert.match(page, /口径/);
  assert.match(page, /「可兑换账号」统计的是邮箱池里当前可用的账号/);
  assert.match(page, /不是卡密数/);
});

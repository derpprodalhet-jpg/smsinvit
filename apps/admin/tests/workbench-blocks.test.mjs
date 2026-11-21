/**
 * 「兑换工作台」结构回归：三块（卡密台账 / 成本 / 待处理）。
 *
 * 缘起：工作台原来是 12 张同级扁平卡片，把「卡密生成 / 兑换成败 / 成本 /
 * 例外量」四种性质混在一起，还只有「今日」没有「累计」、金额单位混用。
 * 这里用假接口按**冻结契约**渲染三块，钉住：
 *   1. 五指标 × 两模块 + 合计行，列顺序固定；
 *   2. expiredApplicable=false 显示「不适用」而不是 0；
 *   3. 多币种分行、绝不跨币种相加；
 *   4. available:false → `—` + 悬浮原因；availableSince → 悬浮含日期；unpricedCount → 提示；
 *   5. 失败态给「重试」且不显示任何 0；
 *   6. 守卫：工作台不得退化回卡片墙（page.tsx 不 map 任何指标）。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const realAntd = require('antd');

const read = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');

function compile(url) {
  return ts.transpileModule(readFileSync(url, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
}

/**
 * antd Tooltip 的悬浮内容是 hover 时才挂到 portal 上的，静态渲染里取不到；
 * 用一个只把 title 落到 DOM 的替身，让「悬浮说明」在测试里可断言。
 */
function StubTooltip({ title, children }) {
  const text = typeof title === 'string' ? title : title?.props?.children;
  return React.createElement(
    'span',
    { 'data-tooltip': typeof text === 'string' ? text : '' },
    children,
  );
}
const antdStub = { ...realAntd, Tooltip: StubTooltip };

/**
 * 递归加载 TSX：所有相对依赖走源码，'antd' 换成带 Tooltip 替身的版本，
 * 'react' 的 useState/useEffect 可注入（页面组件的状态直接喂进去，不跑副作用）。
 */
function screen({ state = [] } = {}) {
  const cache = new Map();
  const load = (url) => {
    const key = String(url);
    if (cache.has(key)) return cache.get(key).exports;
    const module = { exports: {} };
    cache.set(key, module);
    const fallback = createRequire(url);
    let index = 0;
    new Function('require', 'module', 'exports', compile(url))(
      (name) => {
        if (name === 'react') {
          return {
            ...React,
            useEffect: () => {},
            useState: (initial) => [index < state.length ? state[index++] : initial, () => {}],
          };
        }
        if (name === 'antd') return antdStub;
        if (name.endsWith('/admin-page')) {
          return {
            AdminPage: ({ title, children }) =>
              React.createElement('main', null, React.createElement('h1', null, title), children),
          };
        }
        // 源码里的相对导入不带扩展名，Node 的 CJS 解析器不认 .ts；这里自己补。
        if (name.startsWith('.')) {
          const base = new URL(name, url);
          for (const ext of ['', '.ts', '.tsx', '/index.ts', '/index.tsx']) {
            const candidate = new URL(base.href.replace(/\.(ts|tsx)$/, '') + ext);
            if (existsSync(fileURLToPath(candidate))) return load(candidate);
          }
        }
        return fallback(name);
      },
      module,
      module.exports,
    );
    return module.exports;
  };
  return load;
}

function render(component, props) {
  return renderToStaticMarkup(React.createElement(component, props));
}

const headers = (html) => {
  // 只看表头：antd 在 tbody 里还有一行 aria-hidden 的 measure row（全是空 th）。
  const thead = html.match(/<thead[\s\S]*?<\/thead>/)?.[0] ?? '';
  return [...thead.matchAll(/<th[^>]*>([\s\S]*?)<\/th>/g)].map((match) =>
    match[1].replace(/<[^>]*>/g, '').trim(),
  );
};
const rowHtml = (html, key) => {
  const match = html.match(new RegExp(`<tr[^>]*data-row-key="${key}"[\\s\\S]*?</tr>`));
  return match ? match[0] : '';
};
const cells = (row) =>
  [...row.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((match) =>
    match[1]
      .replace(/<[^>]*>/g, '')
      .replace(/\s+/g, ' ')
      .trim(),
  );
const rowKeys = (html) => [...html.matchAll(/data-row-key="([^"]*)"/g)].map((match) => match[1]);
const text = (html) => html.replace(/<[^>]*>/g, '');

const LEDGER_URL = new URL('../src/components/stats-cards-ledger.tsx', import.meta.url);
const COSTS_URL = new URL('../src/components/stats-costs-table.tsx', import.meta.url);
const PAGE_URL = new URL('../src/pages/page.tsx', import.meta.url);

/** 契约样例：模块顺序故意打乱，验证前端自己按 sms/invitation 排。 */
function ledgerCards() {
  return {
    modules: [
      {
        module: 'invitation',
        label: 'Codex 邀请',
        generatedTotal: 3234,
        generatedToday: 3235,
        redeemedTotal: 3236,
        redeemedToday: 3237,
        expired: 0,
        expiredApplicable: false,
      },
      {
        module: 'sms',
        label: '接码',
        generatedTotal: 1234,
        generatedToday: 1235,
        redeemedTotal: 1236,
        redeemedToday: 1237,
        expired: 1238,
        expiredApplicable: true,
      },
    ],
    totals: {
      generatedTotal: 4468,
      generatedToday: 4470,
      redeemedTotal: 4472,
      redeemedToday: 4474,
      expired: 1238,
    },
  };
}

function overviewFixture() {
  return {
    businessTimeZone: 'Asia/Shanghai',
    generatedAt: '2026-10-11T08:00:00Z',
    cards: ledgerCards(),
    costs: [
      {
        key: 'sms',
        label: '接码（美区）',
        currency: 'USD',
        totalMinor: 15,
        todayMinor: 7,
        basis: '成功会话已确认成本',
        available: true,
        availableSince: null,
        unpricedCount: 0,
        detailPath: '/sms/redeems',
      },
      {
        key: 'sms',
        label: '接码（欧区）',
        currency: 'BDT',
        totalMinor: 30000,
        todayMinor: 1250,
        basis: '成功会话已确认成本',
        available: true,
        availableSince: null,
        unpricedCount: 0,
        detailPath: '/sms/redeems',
      },
    ],
    pending: [
      { key: 'sms_uncertain', label: '接码待确认', count: 2, detailPath: '/sms/redeems' },
      { key: 'codex_settlement_review', label: '结算待复核', count: 0 },
    ],
  };
}

// ── 0. formatMinor：minor 整数 + 币种 ────────────────────────────────────────

test('formatMinor：2 位小数 + 币种后缀，未知币种保底，缺失一律 —', () => {
  const { formatMinor } = screen()(new URL('../src/lib/money.ts', import.meta.url));
  assert.equal(formatMinor(15, 'USD'), '$0.15');
  assert.equal(formatMinor(30000, 'BDT'), '300.00 BDT');
  assert.equal(formatMinor(0, 'BDT'), '0.00 BDT');
  assert.equal(formatMinor(1234, 'ZZZ'), '12.34 ZZZ');
  assert.equal(formatMinor(-15, 'USD'), '-$0.15');
  assert.equal(formatMinor(15, null), '0.15');
  assert.equal(formatMinor(null, 'USD'), '—');
  assert.equal(formatMinor(undefined, 'USD'), '—');
  assert.equal(formatMinor('abc', 'USD'), '—');
});

// ── 1. 五指标 × 两模块 + 合计行，列顺序固定 ─────────────────────────────────

test('块① 五指标 × 两模块 + 合计行都在，列顺序固定，行按契约顺序排列', () => {
  const { StatsCardsLedger } = screen()(LEDGER_URL);
  const html = render(StatsCardsLedger, { cards: ledgerCards() });
  assert.deepEqual(headers(html), ['模块', '累计生成', '今日生成', '累计核销', '今日核销', '已过期']);
  assert.deepEqual(rowKeys(html), ['sms', 'invitation', '__total']);
  const expected = {
    sms: ['接码', '1,234', '1,235', '1,236', '1,237', '1,238'],
    invitation: ['Codex 邀请', '3,234', '3,235', '3,236', '3,237', '不适用'],
    __total: ['合计', '4,468', '4,470', '4,472', '4,474', '1,238'],
  };
  for (const [key, want] of Object.entries(expected)) {
    assert.deepEqual(cells(rowHtml(html, key)), want, `${key} 行内容`);
  }
  // 合计行加粗由样式钩子提供，且不是下钻链接。
  assert.match(rowHtml(html, '__total'), /admin-stat-total-row/);
  assert.doesNotMatch(rowHtml(html, '__total'), /<a /);
  // 数字列右对齐 + 等宽字体钩子。
  assert.match(html, /text-align:right/);
  assert.match(html, /admin-stat-number/);
  // 口径悬浮：累计核销与已过期都要有说明。
  assert.match(html, /data-tooltip="该模块卡密进入已核销终态的数量。"/);
  assert.match(html, /data-tooltip="已过期 = expires_at &lt; 现在 且未核销。"/);
});

// ── 2. expiredApplicable=false 显示「不适用」而不是 0 ──────────────────────

test('块① expiredApplicable=false 显示「不适用」并说明，不显示 0', () => {
  const { StatsCardsLedger } = screen()(LEDGER_URL);
  const html = render(StatsCardsLedger, { cards: ledgerCards() });
  const row = rowHtml(html, 'invitation');
  assert.deepEqual(cells(row), ['Codex 邀请', '3,234', '3,235', '3,236', '3,237', '不适用']);
  assert.match(row, /data-tooltip="该模块卡密没有有效期概念"/);
  assert.equal(cells(row).at(-1), '不适用');
  assert.notEqual(cells(row).at(-1), '0');
});

// ── 3. 多币种分行、不跨币种相加 ────────────────────────────────────────────

test('块② 同 key 多币种分行（币种升序），不同币种绝不相加', () => {
  const overview = screen()(new URL('../src/lib/stats-overview.ts', import.meta.url));
  const { StatsCostsTable } = screen()(COSTS_URL);
  const rows = overview.groupCostsByKey([
    overviewFixture().costs[0],
    overviewFixture().costs[1],
  ]);
  assert.deepEqual(
    rows.map((row) => row.currency),
    ['BDT', 'USD'],
  );
  const html = render(StatsCostsTable, {
    costs: [overviewFixture().costs[0], overviewFixture().costs[1]],
  });
  assert.deepEqual(rowKeys(html), ['sms:BDT', 'sms:USD']);
  const bdt = cells(rowHtml(html, 'sms:BDT'));
  const usd = cells(rowHtml(html, 'sms:USD'));
  assert.equal(bdt[1], 'BDT');
  assert.equal(bdt[2], '300.00 BDT');
  assert.equal(bdt[3], '12.50 BDT');
  assert.equal(usd[1], 'USD');
  assert.equal(usd[2], '$0.15');
  // 30000 + 15 若被跨币种求和会得到 300.15；必须不存在。
  assert.doesNotMatch(text(html), /300\.15/);
  // 成本表没有合计行。
  assert.equal(rowKeys(html).includes('__total'), false);
  assert.equal(headers(html).includes('合计'), false);
});

// ── 4. available / availableSince / unpricedCount 三种说明 ─────────────────

test('块② available:false 显示 — 且悬浮有原因；availableSince 悬浮含日期；unpricedCount 有提示', () => {
  const lib = screen()(new URL('../src/lib/stats-overview.ts', import.meta.url));
  const { StatsCostsTable } = screen()(COSTS_URL);
  const unavailable = {
    key: 'sms_provider_fee',
    label: '接码供应商费用',
    currency: 'USD',
    totalMinor: 999,
    todayMinor: 99,
    basis: '供应商已确认成本',
    available: false,
    availableSince: null,
    unpricedCount: 0,
    detailPath: null,
  };
  const since = {
    key: 'codex_phone',
    label: 'codex 邮箱接码',
    currency: 'USD',
    totalMinor: 15,
    todayMinor: 15,
    basis: '成功租号已确认成本',
    available: true,
    availableSince: '2026-10-11',
    unpricedCount: 0,
    detailPath: '/invitation/runs',
  };
  const unpriced = {
    key: 'sms_provider_charge',
    label: '接码供应商扣费',
    currency: 'PHP',
    totalMinor: 25000,
    todayMinor: 25000,
    basis: '实际扣费已确认金额',
    available: true,
    availableSince: null,
    unpricedCount: 3,
    detailPath: null,
  };
  // 纯函数口径（页面用同一份）。
  assert.deepEqual(lib.costAvailabilityNotes(unavailable), [lib.COST_UNAVAILABLE_NOTE]);
  assert.match(lib.costAmountTooltip(unavailable), /暂不可用/);
  assert.equal(lib.costAmountText(unavailable, 'total'), '—');
  assert.equal(lib.costAmountText(unavailable, 'today'), '—');
  assert.match(lib.costAmountTooltip(since), /自 2026-10-11 起才有记录/);
  assert.match(lib.costAmountTooltip(unpriced), /3 条缺金额，未计入/);
  assert.equal(lib.costUnpricedNote(unpriced), '3 条缺金额，未计入');

  const html = render(StatsCostsTable, { costs: [unavailable, since, unpriced] });
  const unavailableRow = rowHtml(html, 'sms_provider_fee:USD');
  assert.deepEqual(cells(unavailableRow).slice(2, 4), ['—', '—']);
  assert.match(unavailableRow, /data-tooltip="[^"]*暂不可用[^"]*"/);
  assert.doesNotMatch(unavailableRow, /\$9\.99/);
  assert.match(rowHtml(html, 'codex_phone:USD'), /data-tooltip="成功租号已确认成本；自 2026-10-11 起才有记录"/);
  const unpricedRow = rowHtml(html, 'sms_provider_charge:PHP');
  assert.match(unpricedRow, /3 条缺金额，未计入/);
  assert.match(unpricedRow, /data-tooltip="实际扣费已确认金额；3 条缺金额，未计入"/);
  assert.equal(cells(unpricedRow)[2], '250.00 PHP');
});

// ── 5. 失败态：显示重试，不显示 0 ─────────────────────────────────────────

test('工作台失败态显示「重试」且不显示任何 0（含残留数据）', () => {
  const load = screen({ state: [null, '统计接口返回结构不完整，无法展示台账；请重试或检查后端版本。', false] });
  const Page = load(PAGE_URL).default;
  const html = render(Page, {});
  const plain = text(html).replace(/\s+/g, '');
  assert.match(plain, /重试/);
  assert.match(plain, /统计加载失败/);
  assert.doesNotMatch(plain, /0/);
  assert.doesNotMatch(html, /ant-table/);
  assert.doesNotMatch(plain, /合计/);
  assert.doesNotMatch(plain, /数据截至/);

  // 即使上一次成功的数据还在内存里，只要这次失败就不许显示数字。
  const stale = screen({ state: [overviewFixture(), '网络错误', false] })(PAGE_URL).default;
  const staleHtml = text(render(stale, {})).replace(/\s+/g, '');
  assert.match(staleHtml, /重试/);
  assert.doesNotMatch(staleHtml, /300\.00BDT|\$0\.15|1,234/);
  assert.doesNotMatch(staleHtml, /数据截至/);
});

test('工作台成功态：页头给业务时区与数据截至，三块都在', () => {
  const Page = screen({ state: [overviewFixture(), '', false] })(PAGE_URL).default;
  const html = render(Page, {});
  const plain = text(html);
  assert.match(plain, /兑换工作台/);
  assert.match(plain, /业务时区：Asia\/Shanghai/);
  assert.match(plain, /数据截至/);
  assert.match(plain, /2026-10-11 16:00:00/);
  assert.match(plain, /① 卡密台账/);
  assert.match(plain, /② 成本/);
  assert.match(plain, /③ 待处理/);
  assert.match(plain, /生成 \/ 复制卡密/);
  // antd 会在两个中文字之间插空格（刷新 → 刷 新）。
  assert.match(plain, /刷\s*新/);
  // 待处理小卡：有量高亮、0 弱化、有 detailPath 可点。
  assert.match(html, /admin-pending-card--idle/);
  assert.match(plain, /结算待复核/);
  assert.match(plain, /接码待确认/);
  const pendingBlock = html.slice(html.indexOf('③ 待处理'));
  assert.ok(pendingBlock, '工作台缺少「③ 待处理」块');
  assert.match(pendingBlock, /class="admin-metric-card__link" href="\/admin\/sms\/redeems"/);
  // 直充业务已下线：工作台不允许再出现直充的下钻入口（正则 gpt[-]direct 见导航守卫同款说明）。
  assert.doesNotMatch(html, /\/gpt[-]direct/);
});

test('parseStatsOverview 拒绝残缺结构，避免渲染成一张全 0 的表', () => {
  const lib = screen()(new URL('../src/lib/stats-overview.ts', import.meta.url));
  assert.throws(() => lib.parseStatsOverview({}), /结构不完整/);
  assert.throws(() => lib.parseStatsOverview({ cards: { modules: [] } }), /结构不完整/);
  assert.throws(() => lib.parseStatsOverview(null), /结构不完整/);
  const parsed = lib.parseStatsOverview(overviewFixture());
  assert.equal(parsed.cards.modules.length, 2);
});

// ── 6. 守卫：工作台不得退化回卡片墙 ────────────────────────────────────────

test('守卫：工作台 page.tsx 不 map 指标、不直接用 AdminMetricCard，指标必须来自三块组件', () => {
  const page = read('../src/pages/page.tsx');
  assert.doesNotMatch(page, /AdminMetricCard/, '工作台不允许再直接摆卡片');
  assert.doesNotMatch(page, /\bmetrics\b/, '工作台不允许再定义扁平 metrics 数组');
  assert.doesNotMatch(page, /\.map\(/, '工作台不允许 map 出指标（这正是卡片墙的写法）');
  assert.doesNotMatch(page, /<Col\b|<Row\b/, '工作台不允许自己排卡片栅格');
  const blocks = [...page.matchAll(/<(StatsCardsLedger|StatsCostsTable|StatsPendingCards)\b/g)].map(
    (match) => match[0],
  );
  assert.deepEqual(
    [...new Set(blocks)].sort(),
    ['<StatsCardsLedger', '<StatsCostsTable', '<StatsPendingCards'],
    '工作台必须正好由三块组件构成',
  );
  assert.equal(blocks.length, 3, '三块组件各渲染一次，不许再加同权卡片块');
  // 卡片只允许出现在「待处理」块里，且数量严格跟随接口返回的 pending[]。
  const ledger = read('../src/components/stats-cards-ledger.tsx');
  const costs = read('../src/components/stats-costs-table.tsx');
  const pending = read('../src/components/stats-pending-cards.tsx');
  assert.doesNotMatch(ledger, /AdminMetricCard/);
  assert.doesNotMatch(costs, /AdminMetricCard/);
  assert.equal((pending.match(/<AdminMetricCard/g) || []).length, 1);
  assert.match(pending, /items\.map\(/);
  assert.match(pending, /pending/);
});

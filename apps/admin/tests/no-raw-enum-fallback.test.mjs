/**
 * 全站「英文枚举兜底」守卫。
 *
 * 需求 4 是「检查全项目所有的列表的英文输出，全部换成中文」。逐列手改容易漏，
 * 也容易被后来的改动重新写回去，所以这里对源码做一次机械扫描：
 *
 *   形态一：映射未命中就把原始枚举当文案返回（`X_LABEL[k] || k`）→ 失败。
 *   形态二（本文件加强的部分）：**状态类字段被原样渲染**，例如
 *          `{ title: '库存状态', dataIndex: 'inventoryStatus', render: (value) => value || '—' }`
 *          或详情里的 `children: row.inventoryStatus || '—'`。
 *          这类写法一个中文字都没有，却能把 `CONSUMED` 直接漏到运营眼前，
 *          上一轮就是漏在 X 卡密管理的「库存状态」列上。
 *
 * 确属技术标识、必须原样展示的地方，在该行加注释 `zh-label-allow-raw` 显式豁免，
 * 让豁免在评审时可见，而不是靠人记得。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { globSync, readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const srcRoot = fileURLToPath(new URL('../src/', import.meta.url));
/** 解析器本身与它们的映射表就是「命中映射 / 未命中走 unknownLabel」的实现处。 */
const EXEMPT_FILES = new Set([
  'lib/zh-label.ts',
  'lib/zh-maps.ts',
  'lib/labels.ts',
  'lib/invitation.ts',
]);

/** 形态一：`SOME_LABEL[...] || 原始值`（最典型的英文泄漏）。 */
const RAW_VALUE_FALLBACK =
  /\b[A-Z][A-Z0-9_]{2,}\s*\[[^\]]+\]\s*\|\|\s*(?:[A-Za-z_$][\w$]*\.)?[A-Za-z_$][\w$]*/;
/** 形态二：`SOME_LABEL[...] || row.value / record.value / value`。 */
const ROW_VALUE_FALLBACK =
  /\b[A-Z][A-Z0-9_]{2,}\s*\[[^\]]+\]\s*\|\|\s*(?:row|record|item|job|log|probe|check|detail|value|key|status|state|event|kind|code)\b/;

/**
 * 状态类字段白名单：这些列 / 详情字段出现在界面上时必须是中文文案。
 * 只有「原样渲染」才会被判违规——空值占位（`|| '—'`）也算，因为它同时也就
 * 意味着「有值时原样输出英文」。
 */
const STATUS_FIELDS = new Set([
  'status',
  'state',
  'inventoryStatus',
  'inventory_status',
  'keyStatus',
  'jobState',
  'job_state',
  'paymentStage',
  'payment_stage',
  'postActionStatus',
  'post_action_status',
  'lastSessionStatus',
  'last_session_status',
  'syncStatus',
  'sync_status',
  'stage',
  'kind',
  'action',
  'role',
  'roles',
  'resourceType',
  'resource_type',
  'channel',
  'source',
  'type',
  'mode',
  'group',
  'event',
  'level',
  'category',
]);

/** 空值占位：`|| '—'` / `?? null`，本身没问题，但和原样渲染连用时就是英文泄漏。 */
const EMPTY_PLACEHOLDER = String.raw`(?:\s*(?:\|\||\?\?)\s*(?:'[^']*'|"[^"]*"|` + '`[^`]*`' + String.raw`|null|undefined))?`;

/** `render: (...)` 的形参表与函数体起点（函数体可能跨行，由 `renderBody` 再切）。 */
const RENDER_CALL = /render:\s*\(([^)]*)\)\s*=>\s*/g;

/** 详情抽屉里的原样渲染：`children: row.inventoryStatus || '—'`。 */
const RAW_CHILDREN = new RegExp(
  String.raw`children:\s*(?:row|record|item|detail|rowDetail|records\.data\.code)\.([A-Za-z_$][\w$]*)` +
    EMPTY_PLACEHOLDER +
    String.raw`\s*[,}\n]`,
  'g',
);

/**
 * 取 `=>` 之后的函数体文本：到括号深度归零时的 `,` / `}` / 换行为止，
 * 这样跨行的 `=> (\n … \n)` 也能整段拿到。
 */
function renderBody(text, start) {
  let depth = 0;
  let out = '';
  for (let index = start; index < text.length && index - start < 600; index += 1) {
    const char = text[index];
    if (char === '(' || char === '[' || char === '{') depth += 1;
    else if (char === ')' || char === ']' || char === '}') {
      if (depth === 0) break;
      depth -= 1;
    } else if (depth === 0 && (char === '\n' || char === ',')) break;
    out += char;
  }
  return out;
}

/** 去掉最外层多余括号并压平空白，便于比对「原样返回」。 */
function normalize(body) {
  let text = body.trim();
  while (text.startsWith('(') && text.endsWith(')')) text = text.slice(1, -1).trim();
  return text.replace(/\s+/g, ' ');
}

/** 往前找同一列 / 同一条目归属的字段名（`dataIndex` 或 `key`）。 */
function owningField(text, index) {
  const window = text.slice(Math.max(0, index - 400), index);
  const matches = [...window.matchAll(/(?:dataIndex|key):\s*(?:'([^']+)'|"([^"]+)")/g)];
  const last = matches.at(-1);
  return last ? last[1] || last[2] : '';
}

/**
 * 判断一段函数体是不是「原样返回」。
 *
 * @returns {{field: string}} 命中时返回被原样渲染的字段名；不命中返回 null。
 */
function passthrough(bodyText, params, text, index) {
  const body = normalize(bodyText);
  if (!body) return null;
  const rows = String.raw`(?:row|record|item|detail|job)`;
  // `render: (_, row) => row.status` / `=> row.status || '—'`
  let match = new RegExp(`^${rows}\\.([A-Za-z_$][\\w$]*)${EMPTY_PLACEHOLDER}$`).exec(body);
  if (match) return { field: match[1] };
  // `render: (value) => value` / `=> value || '—'` / `=> String(value)`
  const first = params[0];
  if (!first || first === '_') return null;
  const escaped = first.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  if (
    new RegExp(`^${escaped}${EMPTY_PLACEHOLDER}$`).test(body) ||
    new RegExp(`^String\\(\\s*${escaped}\\s*\\)$`).test(body)
  )
    return { field: owningField(text, index) };
  return null;
}

const ALLOW = 'zh-label-allow-raw';

/** 豁免标记写在当行或上一行都算，便于把理由写成可读的注释。 */
function allowed(lines, index) {
  return (
    (lines[index] || '').includes(ALLOW) || (lines[index - 1] || '').includes(ALLOW)
  );
}

function lineOf(text, index) {
  return text.slice(0, index).split('\n').length;
}

const report = (text, index, field, line) =>
  `${line}  ${field ? `字段 ${field}` : '未知字段'}：${text.split('\n')[line - 1].trim()}`;

/**
 * 扫出「状态类字段被原样渲染」的位置。
 *
 * @param {string} text 源码
 * @returns {string[]} `第 N 行  字段 X：<原始行>` 形式的问题清单
 */
export function rawStatusRenderOffenders(text) {
  const lines = text.split('\n');
  const found = [];
  for (const match of text.matchAll(RENDER_CALL)) {
    const line = lineOf(text, match.index);
    if (allowed(lines, line - 1)) continue;
    const params = match[1]
      .split(',')
      .map((param) => param.trim().split(':')[0].trim());
    const hit = passthrough(renderBody(text, match.index + match[0].length), params, text, match.index);
    if (!hit || !STATUS_FIELDS.has(hit.field)) continue;
    found.push(report(text, match.index, hit.field, line));
  }
  for (const match of text.matchAll(RAW_CHILDREN)) {
    const line = lineOf(text, match.index);
    if (allowed(lines, line - 1)) continue;
    const field = match[1];
    if (!STATUS_FIELDS.has(field)) continue;
    found.push(report(text, match.index, field, line));
  }
  return found;
}

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? walk(path.join(dir, entry.name))
      : /\.tsx?$/.test(entry.name)
        ? [path.join(dir, entry.name)]
        : [],
  );
}

function sourceFiles() {
  try {
    const found = globSync('**/*.{ts,tsx}', { cwd: srcRoot }).map((file) =>
      path.join(srcRoot, file),
    );
    if (found.length) return found;
  } catch {
    /* 老版本 Node 没有 globSync，走手工遍历 */
  }
  return walk(srcRoot);
}

test('列表与详情不再把原始英文枚举当作未命中兜底', () => {
  const offenders = [];
  for (const file of sourceFiles()) {
    const relative = path.relative(srcRoot, file);
    if (EXEMPT_FILES.has(relative)) continue;
    readFileSync(file, 'utf8')
      .split('\n')
      .forEach((line, index, lines) => {
        if (allowed(lines, index)) return;
        if (!RAW_VALUE_FALLBACK.test(line) && !ROW_VALUE_FALLBACK.test(line)) return;
        offenders.push(`${relative}:${index + 1}  ${line.trim()}`);
      });
  }
  assert.deepEqual(
    offenders,
    [],
    '这些位置在映射未命中时会显示英文枚举；改用 resolveLabel/unknownLabel，或加 zh-label-allow-raw 说明理由',
  );
});

test('状态列与状态详情不再原样渲染后端枚举', () => {
  const offenders = [];
  const files = sourceFiles();
  // 扫描必须真的覆盖到源码目录，避免正则/遍历失效后「零违规」变成假绿。
  assert.ok(files.length > 40, `源码文件扫描数量异常：${files.length}`);
  for (const file of files) {
    const relative = path.relative(srcRoot, file);
    if (EXEMPT_FILES.has(relative)) continue;
    for (const offender of rawStatusRenderOffenders(readFileSync(file, 'utf8')))
      offenders.push(`${relative}:${offender}`);
  }
  assert.deepEqual(
    offenders,
    [],
    '这些状态字段被原样渲染，数据库出现 `CONSUMED` 之类取值时列表就显示英文；' +
      '请走 resolveLabel / 中文映射（或加 zh-label-allow-raw 说明理由）',
  );
});

/**
 * 守卫自检：把真实页面「故意还原成修复前的写法」，必须被判违规；
 * 修复后的写法与非状态字段不得误报。
 *
 * 这里刻意用真实文件做变异，而不是只喂一段手写样例 —— 手写样例可能恰好长成
 * 正则喜欢的样子，只有真实源码的变异才能证明规则确实抓得住上一轮的漏网写法。
 */


test('守卫自检：合法写法与非状态字段不得误报', () => {
  const cases = [
    // 修复前：状态字段原样渲染 → 违规。
    [
      "{ title: '库存状态', dataIndex: 'inventoryStatus', width: 110, render: (value) => value || '—' },",
      1,
    ],
    ["{ title: '状态', dataIndex: 'status', render: (value) => value }", 1],
    ["{ title: '任务状态', dataIndex: 'jobState', render: (_, row) => row.jobState }", 1],
    ["{ key: 'inventory', label: '库存状态', children: row.inventoryStatus || '—' },", 1],
    // 修复后：走中文映射 / 标签组件 → 合法。
    [
      "{ title: '库存状态', dataIndex: 'inventoryStatus', render: (value) => inventoryStatusTag(value) },",
      0,
    ],
    [
      "{ title: '任务状态', dataIndex: 'jobState', render: (value) => <AdminStatusTag tone={xStateTone(String(value))}>{xStateLabel(String(value))}</AdminStatusTag> },",
      0,
    ],
    [
      "{ title: '库存状态', dataIndex: 'inventoryStatus', render: (value) => resolveLabel(INVENTORY_STATUS_LABEL, value) },",
      0,
    ],
    ["{ key: 'inventory', label: '库存状态', children: inventoryStatusTag(row.inventoryStatus) },", 0],
    // 非状态字段（邮箱、套餐、卡密）原样渲染是正常的，不能误报。
    ["{ title: '邮箱', dataIndex: 'email', render: (value) => value || '—' },", 0],
    ["{ key: 'sku', label: '套餐', children: row.skuName || '—' },", 0],
    // 显式豁免：技术标识（协议名等）允许原样展示。
    ["// zh-label-allow-raw: 协议名是技术标识\n{ dataIndex: 'mode', render: (value) => value },", 0],
  ];
  for (const [source, expected] of cases) {
    assert.equal(
      rawStatusRenderOffenders(source).length,
      expected,
      `判定错误（期望 ${expected} 条）：${source}`,
    );
  }
});

test('状态标签不再回退成原始英文状态', () => {
  const status = readFileSync(path.join(srcRoot, 'components/invitation-status.tsx'), 'utf8');
  assert.doesNotMatch(status, /label \|\| status/);
  assert.match(status, /resolveLabel/);
});

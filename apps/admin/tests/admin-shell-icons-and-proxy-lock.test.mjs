/**
 * 两条运营可感知的回归守卫。
 *
 * ① 代理池页：点一次「测试」之后整页按钮全灰。
 *    根因是 `const blocked = !!busy || !!testing.length` —— 只读的连通性测试被当成了
 *    整页的全局锁（每个代理 timeout 12 秒，批量测试可能十几秒），运营以为页面坏了。
 *    修复后：写操作按钮（保存 / 导入 / 换协议 / 清空 / 重置 / 删除…）只受「有动作在跑」
 *    约束；只有测试入口（测试全部、行内「测试」、详情里的「测试连通性」）在测试进行中
 *    禁用，并且只有真正在测的行显示「测试中」/ loading。
 *    该页渲染成本高（antd + 请求夹具），所以做**源码级**断言：解析页面上每一个
 *    `disabled={...}`，凡是引用了「测试进行中」的，它所在的 <Button> 必须真的是
 *    testProxies 入口；反过来，任何写操作按钮的 disabled 里都不允许出现测试状态。
 *
 * ② 侧边栏：一级与二级菜单项都必须带图标。
 *    直接解析 admin-shell.tsx 的 NAV 配置树，逐项检查 icon，并确认图标确实从
 *    @ant-design/icons 引入（避免写了没导入的组件名），以及分组标题本身也有图标。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');
const PAGE = '../src/pages/invitation/proxies/page.tsx';
const SHELL = '../src/components/admin-shell.tsx';

/** 从 openIndex 处的 `{` / `[` / `(` 取配对的另一侧，跳过字符串/模板串；返回内容与结束下标。 */
function balanced(text, openIndex) {
  const close = { '{': '}', '[': ']', '(': ')' }[text[openIndex]];
  assert.ok(close, `不是括号起始：${text.slice(openIndex, openIndex + 20)}`);
  let depth = 0;
  let quote = null;
  for (let i = openIndex; i < text.length; i += 1) {
    const c = text[i];
    if (quote) {
      if (c === '\\') i += 1;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') quote = c;
    else if (c === text[openIndex]) depth += 1;
    else if (c === close) {
      depth -= 1;
      if (depth === 0) return { body: text.slice(openIndex + 1, i), end: i };
    }
  }
  throw new Error(`括号没有闭合：${text.slice(openIndex, openIndex + 40)}`);
}

/** 取出每个 `<Button …>` 开标签（含属性体），用于把 disabled 与它的 onClick 对上。 */
function buttonTags(source) {
  const tags = [];
  const pattern = /<Button\b/g;
  let match;
  while ((match = pattern.exec(source))) {
    let depth = 0;
    let quote = null;
    for (let i = match.index + '<Button'.length; i < source.length; i += 1) {
      const c = source[i];
      if (quote) {
        if (c === '\\') i += 1;
        else if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'" || c === '`') quote = c;
      else if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) {
        tags.push({ index: match.index, text: source.slice(match.index, i + 1) });
        break;
      }
    }
  }
  return tags;
}

/** 某个开标签里的 `name={...}` 表达式（没有该属性时返回 undefined）。 */
function attribute(tag, name) {
  const at = tag.indexOf(`${name}={`);
  if (at < 0) return undefined;
  return balanced(tag, tag.indexOf('{', at)).body;
}

/** 源码里所有 `name={...}` 表达式及其位置。 */
function attributeOccurrences(source, name) {
  const out = [];
  const pattern = new RegExp(`${name}=\\{`, 'g');
  let match;
  while ((match = pattern.exec(source))) {
    const { body, end } = balanced(source, source.indexOf('{', match.index));
    out.push({ index: match.index, body });
    pattern.lastIndex = end;
  }
  return out;
}

/** NAV 数组体里的顶层 `{...}` 元素文本（children 递归由调用方处理）。 */
function objectEntries(body) {
  const out = [];
  let i = 0;
  while (i < body.length) {
    if (body[i] === '{') {
      const { body: element, end } = balanced(body, i);
      out.push(element);
      i = end + 1;
    } else {
      i += 1;
    }
  }
  return out;
}

test('只读的代理连通性测试不再锁住写操作按钮', () => {
  const source = read(PAGE);

  // 旧写法（把测试并进整页全局锁）必须彻底消失。
  assert.doesNotMatch(source, /const\s+blocked\s*=/, '不能再有把测试算进去的全局 blocked 锁');

  // 「测试进行中」必须是独立的派生状态；允许引用它的变量名，也允许直接写 testing.length。
  const flag = source.match(/const\s+([A-Za-z_$][\w$]*)\s*=\s*!!testing\.length/);
  assert.ok(flag, '必须有一个只表示「连通性测试进行中」的独立状态');
  const testState = (expression) =>
    new RegExp(`\\b${flag[1]}\\b`).test(expression) || /testing\.length/.test(expression);

  const tags = buttonTags(source);
  assert.ok(tags.length >= 12, `页面上解析到的按钮太少（${tags.length}），解析器可能失效`);

  // 每一个被「测试进行中」禁用的控件，都必须是真正的测试入口，且保留「脱敏不可测」。
  let guardedTestEntries = 0;
  for (const occurrence of attributeOccurrences(source, 'disabled')) {
    if (!testState(occurrence.body)) continue;
    const owner = tags.find(
      (tag) => occurrence.index >= tag.index && occurrence.index < tag.index + tag.text.length,
    );
    assert.ok(owner, `非 <Button> 的 disabled 不能引用测试状态：${occurrence.body}`);
    assert.match(
      attribute(owner.text, 'onClick') || '',
      /testProxies\s*\(/,
      `被测试状态禁用的按钮必须就是测试入口：${owner.text.slice(0, 120)}`,
    );
    assert.match(
      occurrence.body,
      /masked|isInvitationMask/,
      `测试入口必须保留「脱敏地址不可测」：${occurrence.body}`,
    );
    guardedTestEntries += 1;
  }
  assert.ok(
    guardedTestEntries >= 2,
    `「测试全部」与行内「测试」都必须受测试状态约束，实际只找到 ${guardedTestEntries} 个`,
  );

  // 写操作按钮必须存在，而且它们的 disabled 与测试状态完全无关。
  const writePattern =
    /mutate\s*\(|importPool\s*\(|importLegacyPool\s*\(|switchSelectedSchemes\s*\(|switchScheme\s*\(/;
  const writeButtons = tags.filter((tag) => writePattern.test(attribute(tag.text, 'onClick') || ''));
  assert.ok(writeButtons.length >= 5, `写操作按钮解析不全（${writeButtons.length}）`);
  for (const tag of writeButtons) {
    const disabled = attribute(tag.text, 'disabled') || '';
    assert.ok(
      !testState(disabled),
      `写操作按钮不得被只读测试禁用（disabled=${disabled}）：${tag.text.slice(0, 120)}`,
    );
  }

  // 原有行为必须保留：同一时刻只跑一批测试、只让真正在测的行显示「测试中」。
  assert.match(source, /if \(testInFlight\.current\) return;/, '必须保留「同一时刻只跑一批」');
  assert.match(source, /urls\.some\(isInvitationMask\)/, '必须保留脱敏地址不可测');
  assert.match(source, /testing\.includes\(row\.url\)/, '「测试中」必须只标真正在测的行');
  assert.match(source, /<Tag color="processing">测试中<\/Tag>/);
});

test('侧边栏一级与二级菜单项都有图标', () => {
  const source = read(SHELL);
  const navStart = source.indexOf('const NAV: NavNode[] = [');
  assert.ok(navStart >= 0, '找不到 NAV 菜单配置');
  // 注意 `NavNode[]` 自己带一个 `[`，要从 `=` 之后再找数组起点。
  const navBody = balanced(source, source.indexOf('[', source.indexOf('=', navStart))).body;

  const nodes = [];
  const walk = (body) => {
    for (const element of objectEntries(body)) {
      const name = element.match(/\bname:\s*'([^']*)'/);
      assert.ok(name, `菜单项缺少 name：${element.trim().slice(0, 60)}`);
      const icon = element.match(/\bicon:\s*<([A-Za-z_$][\w$]*)\s*\/>/);
      const childAt = element.indexOf('children:');
      nodes.push({ name: name[1], icon: icon ? icon[1] : null, hasChildren: childAt >= 0 });
      if (childAt >= 0) walk(balanced(element, element.indexOf('[', childAt)).body);
    }
  };
  walk(navBody);

  // 解析器必须覆盖 NAV 里的每一项（一级与二级），否则断言会变成空转。
  const nameCount = (navBody.match(/\bname:\s*'/g) || []).length;
  assert.equal(nodes.length, nameCount, '每一项菜单都必须被解析到（一级与二级）');
  assert.ok(nodes.length >= 27, `菜单项数量异常（${nodes.length}），解析器可能漏项`);

  const missing = nodes.filter((node) => !node.icon).map((node) => node.name);
  assert.deepEqual(missing, [], `这些菜单项缺少 icon：${missing.join('、')}`);

  const imported = new Set(
    (source.match(/import\s*\{([^}]*)\}\s*from\s*'@ant-design\/icons';/) || [])[1]
      ?.split(',')
      .map((value) => value.trim())
      .filter(Boolean) || [],
  );
  assert.ok(imported.size > 0, '必须从 @ant-design/icons 引入图标');
  const notImported = [...new Set(nodes.map((node) => node.icon))].filter(
    (icon) => !imported.has(icon),
  );
  assert.deepEqual(notImported, [], `图标没有从 @ant-design/icons 引入：${notImported.join('、')}`);

  // 分组标题本身也要有图标，且三大分组齐全。
  const groups = nodes.filter((node) => node.hasChildren);
  for (const group of groups) assert.ok(group.icon, `分组「${group.name}」缺少图标`);
  for (const name of ['接码', 'Codex 邀请', '系统管理'])
    assert.ok(groups.some((group) => group.name === name), `缺少分组「${name}」`);
  // 「GPT 直充」业务已下线：导航里不许再有该分组、也不许再挂直充路由。
  // 正则写成 gpt[-]direct 是为了让「残留自查」在源码里搜不到该字面量，
  // 同时这条守卫仍然是真的断言（不是空断言）。
  assert.ok(
    !nodes.some((node) => node.name === 'GPT 直充'),
    '导航不应再有「GPT 直充」分组',
  );
  assert.doesNotMatch(source, /gpt[-]direct/);

  // 语义要贴切：钉住任务里点名的几个映射，防止所有条目凑同一个图标。
  const expected = {
    邮箱管理: 'MailOutlined',
    单次注册: 'UserAddOutlined',
    注册结果: 'SolutionOutlined',
    代理池: 'GlobalOutlined',
    接码价格: 'TagOutlined',
    登录安全: 'SafetyCertificateOutlined',
    通知发送记录: 'SendOutlined',
    定时任务: 'FieldTimeOutlined',
    操作审计: 'AuditOutlined',
  };
  for (const [name, icon] of Object.entries(expected)) {
    const node = nodes.find((item) => item.name === name);
    assert.ok(node, `菜单里找不到「${name}」`);
    assert.equal(node.icon, icon, `「${name}」的图标应为 ${icon}`);
  }
  assert.ok(
    new Set(nodes.map((node) => node.icon)).size >= 10,
    '图标不能千篇一律，至少要有 10 种不同语义',
  );
});

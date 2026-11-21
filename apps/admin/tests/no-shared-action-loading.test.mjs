/**
 * 回归守卫：一个 loading 状态不能被「多个真正驱动它的动作」共用。
 *
 * 运营实测到的 bug：点「保存当前」，页面上其它语义不同的按钮（清除 API Key、
 * 轮换 Token、刷新、批量删除…）跟着一起转圈；邮箱管理里点**一行**的「重置」，
 * 所有行的按钮都转圈。根因都是「一个布尔当成了整页的忙闲标志」。
 *
 * 判定逻辑（不是简单的文本匹配，避免误报）：
 *   对每个 loading/disabled 绑定表达式里出现的状态 S，
 *   找出绑定处最近的 onClick 动作函数名；只有当 **两个以上不同的动作函数都真的调用
 *   了 setS(...)** 时才算违规。
 *   这样就可以允许 `loading={loading || saving !== null}` 这种「页面加载中或正在保存」
 *   的合理写法 —— 那个 Switch 的动作只 setSaving，并不 setLoading。
 *
 * 需要特例时在被判定行或其上一行写 `shared-loading-allow: 原因`。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = fileURLToPath(new URL('../src', import.meta.url));

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...walk(path));
    else if (path.endsWith('.tsx')) out.push(path);
  }
  return out;
}

/** 从 `{` 开始取配对的 `{...}`（跳过字符串/模板串），返回内容与结束位置。 */
function balanced(text, openIndex) {
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
    else if (c === '{') depth += 1;
    else if (c === '}') {
      depth -= 1;
      if (depth === 0) return { body: text.slice(openIndex + 1, i), end: i };
    }
  }
  return { body: '', end: text.length };
}

/** 收集所有具名函数的函数体（用来判断某个动作是否真的调用了 setX）。 */
function functionBodies(text) {
  const bodies = {};
  const patterns = [
    /(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/g,
    /const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{/g,
    /const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?[A-Za-z_$][\w$]*\s*=>\s*\{/g,
  ];
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      const open = match.index + match[0].length - 1;
      const { body } = balanced(text, open);
      // 连同函数头（含参数默认值）一起存：`load(key = 'load')` 这种把 key 当参数
      // 传进来的写法，字面量在参数里而不在函数体里。
      bodies[match[1]] = (bodies[match[1]] || '') + match[0] + body;
    }
  }
  return bodies;
}

/** 逐个找 loading/disabled 绑定，返回 {state, action, index}。 */
function bindings(text) {
  const clicks = [...text.matchAll(/onClick=\{/g)].map((m) => ({
    index: m.index,
    body: balanced(text, m.index + 'onClick={'.length - 1).body.replace(/\s+/g, ''),
  }));
  const out = [];
  for (const match of text.matchAll(/(?:loading|disabled)=\{/g)) {
    const open = match.index + match[0].length - 1;
    const { body } = balanced(text, open);
    const nearest = clicks.length
      ? clicks.reduce((a, b) => (Math.abs(b.index - match.index) < Math.abs(a.index - match.index) ? b : a))
      : null;
    out.push({
      expr: body.replace(/\s+/g, ''),
      // 绑定表达式里出现的所有状态名（含 `a || b` 这种组合）
      states: [...body.matchAll(/\b([A-Za-z_$][\w$]*)\b/g)].map((m) => m[1]),
      action: nearest?.body || '',
      index: match.index,
    });
  }
  return out;
}

/** 顶层逗号切分实参（跳过括号与字符串）。 */
function splitArgs(text) {
  const args = [];
  let depth = 0;
  let quote = null;
  let current = '';
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quote) {
      current += c;
      if (c === '\\') {
        current += text[i + 1] ?? '';
        i += 1;
      } else if (c === quote) quote = null;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') quote = c;
    else if ('([{'.includes(c)) depth += 1;
    else if (')]}'.includes(c)) depth -= 1;
    if (c === ',' && depth === 0) {
      args.push(current);
      current = '';
      continue;
    }
    current += c;
  }
  if (current.trim()) args.push(current);
  return args.map((a) => a.trim());
}

/** 单个实参是不是「布尔开关」（true/false/取反/属性链）——用于把「开启/关闭」合成同一个动作。 */
function isBooleanish(arg) {
  return /^!*(?:true|false|[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*)$/.test(arg.trim());
}

/**
 * 动作身份。只按**函数名**判断会把 `confirm(文案, 回调)` 这种「同名不同参数」的
 * 多个按钮误判成同一个动作（每个按钮的 onClick 都是 `() => confirm(...)`），
 * 所以这里带上实参；同时把「只有一个布尔实参」的情况折叠成 `f(*)`，
 * 这样「开启/关闭」这对同一个动作不会被误报。
 */
function actionIdentity(expr) {
  const match = expr.match(/^(?:async)?\([^)]*\)=>(?:void)?([A-Za-z_$][\w$.]*)\(([\s\S]*)\)$/);
  if (!match) return expr;
  const [, name, rawArgs] = match;
  const args = splitArgs(rawArgs);
  if (args.length === 1 && isBooleanish(args[0])) return `${name}(*)`;
  return `${name}(${args.map((a) => a.replace(/\b(true|false)\b/g, 'B')).join(',')})`;
}

export function violations(text) {
  const declared = new Set(
    [...text.matchAll(/const\s+\[(\w+),\s*(\w+)\]\s*=\s*useState(?:<[^>]*>)?\((?:false|true)\)/g)].map(
      (m) => `${m[1]}|${m[2]}`,
    ),
  );
  if (!declared.size) return [];
  const bodies = functionBodies(text);
  const sites = bindings(text);
  const found = [];
  for (const entry of declared) {
    const [state, setter] = entry.split('|');
    // 绑定表达式里必须**以该状态为主要判据**（单独出现或出现在组合里）
    const related = sites.filter((site) => site.expr.includes(state));
    if (related.length < 2) continue;
    // 哪些动作真的会驱动这个状态？
    const drivers = related.filter((site) => {
      // 动作体形如 `()=>voidonSave({...})`（已去掉空白）：先跳掉箭头签名再取函数名。
      const called = site.action.match(/^(?:async)?\([^)]*\)=>(?:void)?([A-Za-z_$][\w$]*)/)?.[1];
      if (!called) return false;
      const body = bodies[called];
      if (!body) return false;
      return new RegExp(`\\b${setter}\\s*\\(`).test(body);
    });
    const actions = new Set(drivers.map((site) => actionIdentity(site.action)));
    if (drivers.length >= 2 && actions.size >= 2) {
      const line = text.slice(0, related[0].index).split('\n').length;
      if (/shared-loading-allow:/.test(text.slice(Math.max(0, related[0].index - 400), related[0].index + 200)))
        continue;
      found.push({ state, sites: drivers.length, actions: [...actions], line });
    }
  }
  return found;
}

test('守卫自身有效：这段「修复前」的代码必须被判定为违规', () => {
  const before = `
    function Page() {
      const [saving, setSaving] = useState(false);
      async function onSave(opts) {
        setSaving(true);
        try { await api(opts); } finally { setSaving(false); }
      }
      return (<>
        <Button loading={saving} onClick={() => void onSave({ clearApiKey: true })}>清除 API Key</Button>
        <Button loading={saving} onClick={() => void onSave({ rotateToken: true })}>轮换 Token</Button>
        <Button loading={saving} onClick={() => void onSave()}>保存当前页</Button>
      </>);
    }`;
  const found = violations(before);
  assert.equal(found.length, 1, '守卫必须能识别「一个 loading 被多个动作共用」');
  assert.equal(found[0].state, 'saving');
});

test('守卫自检：同名函数不同参数的按钮必须被判为不同动作', () => {
  const bulk = `
    function Page() {
      const [busy, setBusy] = useState(false);
      function confirmRun(message, run) { setBusy(true); try { run(); } finally { setBusy(false); } }
      return (<>
        <Button loading={busy} onClick={() => confirmRun('重置全部失败邮箱？', () => mutate('/accounts/reset_failed'))}>重置失败邮箱</Button>
        <Button loading={busy} onClick={() => confirmRun('释放卡死邮箱？', () => mutate('/accounts/release_stale'))}>释放卡死邮箱</Button>
      </>);
    }`;
  assert.equal(violations(bulk).length, 1, '同名函数不同回调必须判为不同动作');
});

test('守卫自检：开启/关闭这对同一个动作不能被误报', () => {
  const toggle = `
    function Page() {
      const [saving, setSaving] = useState(false);
      async function save(next) { setSaving(true); try { await api(next); } finally { setSaving(false); } }
      return (<>
        <Switch loading={saving} onClick={() => void save(true)} />
        <Button loading={saving} onClick={() => void save(!status?.enabled)}>开启/关闭</Button>
      </>);
    }`;
  assert.deepEqual(violations(toggle), [], '同一个动作的开启/关闭不能被误报');
});

test('守卫不误报：同一个动作驱动一个状态是正常的', () => {
  const fine = `
    function Page() {
      const [loading, setLoading] = useState(false);
      async function load() { setLoading(true); try { await api(); } finally { setLoading(false); } }
      return (<>
        <Card loading={loading} />
        <Button loading={loading} onClick={() => void load()}>刷新</Button>
      </>);
    }`;
  assert.deepEqual(violations(fine), []);
});

test('守卫不误报：组合表达式里「只是被读到」的状态不算被该动作驱动', () => {
  const composed = `
    function Page() {
      const [loading, setLoading] = useState(false);
      const [saving, setSaving] = useState(false);
      async function load() { setLoading(true); try { await api(); } finally { setLoading(false); } }
      async function save(v) { setSaving(true); try { await api(v); } finally { setSaving(false); } }
      return (<>
        <Switch loading={loading || saving} onClick={() => void save(true)} />
        <Button loading={loading} onClick={() => void load()}>刷新</Button>
      </>);
    }`;
  assert.deepEqual(violations(composed), []);
});

test('没有任何页面把「多个真正驱动它的动作」共用一个 loading 状态', () => {
  const offenders = [];
  for (const file of walk(SRC)) {
    const text = readFileSync(file, 'utf8');
    for (const hit of violations(text)) {
      offenders.push(`${file.replace(SRC, 'src')}:${hit.line} 状态 ${hit.state} 被 ${hit.sites} 个不同动作共用 → ${hit.actions.join(' / ')}`);
    }
  }
  assert.deepEqual(offenders, [], `一个 loading 只能服务一个动作：\n${offenders.join('\n')}`);
});

/**
 * 第二类检查：key 形式（`loading={busy === 'K'}`）下的错配。
 *
 * 布尔形态由上面的检查覆盖；一旦页面按本次修复改成 `useState<string | null>`，
 * 布尔那条就扫不到它了。所以这里补一条：**按钮自己**的 loading key，必须由它自己的
 * onClick 动作设置。只查 `loading`（按钮转圈），不查 `disabled` ——
 * 「某个动作进行中把别的控件禁用」是合理用法。
 */
export function keyedLoadingMismatches(text) {
  const setters = [...text.matchAll(/const\s+\[\w+,\s*(\w+)\]\s*=\s*useState<string\s*\|\s*null>?\(/g)].map(
    (m) => m[1],
  );
  if (!setters.length) return [];
  const bodies = functionBodies(text);
  const clicks = [...text.matchAll(/onClick=\{/g)].map((m) => ({
    index: m.index,
    body: balanced(text, m.index + 'onClick={'.length - 1).body.replace(/\s+/g, ''),
  }));
  // 只检查 <Button> 自身的 loading：<Table loading> / <Card loading> 是整块加载，
  // 与「哪个按钮在转圈」无关（第一版把 Table 也算进去，直接产生假阳性）。
  const buttonWindows = [];
  for (const start of [...text.matchAll(/<Button\b/g)].map((m) => m.index)) {
    const rest = text.slice(start);
    const nextButton = rest.slice(1).search(/<Button\b/);
    const closeTag = rest.search(/<\/Button>|\/>/);
    const end = Math.min(
      start + (nextButton === -1 ? rest.length : nextButton + 1),
      closeTag === -1 ? rest.length : start + closeTag + 2,
      start + 2000,
    );
    buttonWindows.push({ start, end, text: text.slice(start, end) });
  }
  const out = [];
  for (const window of buttonWindows) {
    for (const match of window.text.matchAll(/loading=\{\s*(\w+)\s*===\s*'([^']+)'\s*\}/g)) {
    const [, state, key] = match;
    const setter = setters.find((name) => text.includes(`[${state}, ${name}]`) || text.includes(`[${state},${name}]`));
    if (!setter) continue;
    const absolute = window.start + match.index;
    const inWindow = clicks.filter((c) => c.index >= window.start && c.index <= window.end);
    if (!inWindow.length) continue;
    const nearest = inWindow.reduce(
      (a, b) => (Math.abs(b.index - absolute) < Math.abs(a.index - absolute) ? b : a),
      inWindow[0],
    );
    if (!nearest) continue;
    const called = nearest.body.match(/^(?:async)?\([^)]*\)=>(?:void)?([A-Za-z_$][\w$.]*)/)?.[1];
    const body = called ? bodies[called] || '' : '';
    // 动作自己没设置这个 key，却让按钮转圈 → 转圈的不是这次点击的动作。
    // 允许「计算 key」的写法（如 `const action = opts?.x ? 'a' : 'b'; setSaving(action)`）：
    // 只要动作里出现了该 key 字面量、并且确实调用了 setter，就认为它是驱动方。
    const direct = new RegExp(`${setter}\\s*\\(\\s*'${key}'`).test(body);
    const computed = body.includes(`'${key}'`) && new RegExp(`${setter}\\s*\\(`).test(body);
    if (body && !direct && !computed) {
      const line = text.slice(0, absolute).split('\n').length;
      const around = text.slice(Math.max(0, absolute - 400), absolute + 200);
      if (/shared-loading-allow:/.test(around)) continue;
      out.push({ state, key, line, action: called });
    }
    }
  }
  return out;
}

test('guard 自检：key 形式下「按钮转圈不由自己动作驱动」必须被判违规', () => {
  const bad = `
    function Page() {
      const [busy, setBusy] = useState<string | null>(null);
      async function save() { setBusy('save'); try { await api(); } finally { setBusy(null); } }
      async function load() { setBusy('load'); try { await api(); } finally { setBusy(null); } }
      return (<Button loading={busy === 'save'} onClick={() => void load()}>刷新</Button>);
    }`;
  assert.equal(keyedLoadingMismatches(bad).length, 1);
});

test('guard 自检：<Table loading> 这类整块加载不算「按钮转圈错配」', () => {
  const table = `
    function Page() {
      const [loading, setLoading] = useState<string | null>(null);
      async function load() { setLoading('load'); try { await api(); } finally { setLoading(null); } }
      async function testSaved() { await api(); }
      return (<Card extra={<Button onClick={() => void testSaved()}>测试</Button>}><Table loading={loading === 'load'} /></Card>);
    }`;
  assert.deepEqual(keyedLoadingMismatches(table), []);
});

test('没有任何按钮的 loading key 由别的动作设置', () => {
  const offenders = [];
  for (const file of walk(SRC)) {
    for (const hit of keyedLoadingMismatches(readFileSync(file, 'utf8'))) {
      offenders.push(`${file.replace(SRC, 'src')}:${hit.line} 按钮 loading={${hit.state} === '${hit.key}'}，但它的动作 ${hit.action}() 不设置这个 key`);
    }
  }
  assert.deepEqual(offenders, [], `按钮的转圈必须由它自己触发的动作驱动：\n${offenders.join('\n')}`);
});

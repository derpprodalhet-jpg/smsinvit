import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const source = new URL('../src/pages/invitation/settings/page.tsx', import.meta.url);
const require = createRequire(source);
const compile = (file) =>
  ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
const compiled = compile(source);
const POOL_PATH = '/api/v1/admin/invitation/pool';
const state = (enabled = false, overrides = {}) => ({
  enabled,
  running: enabled,
  target: 10,
  available: 3,
  mailboxAvailable: 12,
  concurrency: 2,
  checkSeconds: 60,
  rounds: 5,
  lastError: '',
  ...overrides,
});
const flush = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};

/** Mirrors the executor harness so the pool control is held to the same rules. */
function harness(handler = () => state()) {
  const slots = [],
    effects = [],
    calls = [],
    notices = [];
  let cursor = 0,
    tree;
  const equal = (left, right) =>
    left &&
    right &&
    left.length === right.length &&
    left.every((value, index) => Object.is(value, right[index]));
  const hooks = {
    ...React,
    useState(initial) {
      const index = cursor++;
      slots[index] ||= { value: initial };
      return [
        slots[index].value,
        (next) => {
          slots[index].value = typeof next === 'function' ? next(slots[index].value) : next;
        },
      ];
    },
    useRef(initial) {
      return (slots[cursor++] ||= { current: initial });
    },
    useCallback(callback, dependencies) {
      const index = cursor++;
      if (!equal(slots[index]?.dependencies, dependencies))
        slots[index] = { callback, dependencies };
      return slots[index].callback;
    },
    useEffect(callback, dependencies) {
      const index = cursor++;
      if (!equal(slots[index]?.dependencies, dependencies)) {
        const previous = slots[index];
        slots[index] = { dependencies, cleanup: previous?.cleanup };
        effects.push(() => {
          previous?.cleanup?.();
          slots[index].cleanup = callback();
        });
      }
    },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(
    (name) => {
      if (name === 'react') return hooks;
      if (name === 'antd')
        return {
          ...Object.fromEntries(
            ['Alert', 'Button', 'Card', 'Space', 'Switch', 'Tabs', 'Tag'].map((value) => [
              value,
              value,
            ]),
          ),
          Typography: { Text: 'Text' },
          message: { success: (value) => notices.push(value) },
        };
      if (name.endsWith('/admin-api'))
        return {
          adminApi: async (path, init) => {
            const call = { path, ...init, payload: init?.body ? JSON.parse(init.body) : undefined };
            calls.push(call);
            return { data: await handler(call, calls) };
          },
        };
      if (name.endsWith('/invitation-api'))
        return { invitationErrorMessage: (error) => error.message };
      if (name.endsWith('/admin-page')) return { AdminPage: 'AdminPage' };
      if (name.endsWith('/navigation')) return { Link: 'Link' };
      if (name === './config-editor') return { ConfigEditor: 'ConfigEditor' };
      return require(name);
    },
    module,
    module.exports,
  );
  const nodes = (node) =>
    !node || typeof node !== 'object'
      ? []
      : Array.isArray(node)
        ? node.flatMap((child) => nodes(child))
        : [node, ...nodes(node.props?.children)];
  const render = () => {
    cursor = 0;
    tree = module.exports.InvitationPoolControl();
    effects.splice(0).forEach((effect) => effect());
    return tree;
  };
  render();
  const flatten = (value) => (Array.isArray(value) ? value.flat(Infinity).join('') : value);
  return {
    calls,
    notices,
    render,
    switch: () => nodes(tree).find((node) => node.type === 'Switch'),
    card: () => nodes(tree).find((node) => node.type === 'Card'),
    button: (label) =>
      nodes(tree).find((node) => node.type === 'Button' && node.props.children === label),
    error: () => nodes(tree).find((node) => node.type === 'Alert')?.props.message,
    tags: () =>
      nodes(tree)
        .filter((node) => node.type === 'Tag')
        .map((node) => flatten(node.props.children)),
    texts: () =>
      nodes(tree)
        .filter((node) => node.type === 'Text')
        .map((node) => flatten(node.props.children)),
    settle: async () => {
      await flush();
      render();
    },
    unmount: () => slots.forEach((slot) => slot.cleanup?.()),
  };
}

test('pool control reads the authoritative snapshot on mount and never mutates it', async () => {
  const ui = harness();
  assert.equal(ui.switch().props.disabled, true);
  await ui.settle();
  assert.deepEqual(
    ui.calls.map(({ path, method }) => ({ path, method })),
    [{ path: POOL_PATH, method: undefined }],
  );
  assert.equal(ui.switch().props.checked, false);
  assert.equal(ui.button('开启账号池自动补号').props.disabled, false);
  ui.unmount();
});

test('pool card separates its own numbers from the invitation concurrency and states the cost', async () => {
  const ui = harness();
  await ui.settle();
  assert.equal(ui.card().props.title, '账号池自动补号');
  for (const text of [
    '可兑换账号 3',
    '补号目标 10',
    '可注册邮箱 12',
    '补号并发 2',
    '巡检间隔 60 秒',
    '已完成补号轮次 5',
  ])
    assert.ok(ui.tags().includes(text), text);
  const copy = ui.texts().join('\n');
  assert.match(copy, /与邀请执行各自独立|不影响邀请执行/);
  assert.match(copy, /邮箱库存/);
  assert.match(copy, /接码余额/);
  assert.match(copy, /不做 Codex\s*授权/);
  ui.unmount();
});

test('enabled but idle pool shows its own status and a backend error without faking a run', async () => {
  const reason = '代理池暂无可用代理';
  const ui = harness(() => state(true, { running: false, lastError: reason }));
  await ui.settle();
  assert.equal(ui.switch().props.checked, true);
  assert.equal(ui.button('关闭账号池自动补号').props.disabled, false);
  assert.ok(ui.tags().includes('已开启，暂未运行'));
  assert.ok(ui.texts().some((text) => text.includes(reason)));
  ui.unmount();
});

test('pool switch submits exactly one enabled flag and confirms it with an authoritative reread', async () => {
  let enabled = false;
  const ui = harness((call) => {
    if (call.method === 'PUT') enabled = call.payload.enabled;
    return state(enabled);
  });
  await ui.settle();
  ui.switch().props.onChange(true);
  ui.switch().props.onChange(true);
  await ui.settle();
  assert.deepEqual(
    ui.calls.map(({ method }) => method || 'GET'),
    ['GET', 'PUT', 'GET'],
  );
  assert.deepEqual(ui.calls[1].payload, { enabled: true });
  assert.equal(ui.switch().props.checked, true);
  assert.deepEqual(ui.notices, ['账号池自动补号已开启']);
  ui.unmount();
});

test('a pool save that the reread contradicts is reported instead of showing success', async () => {
  const ui = harness(() => state(false));
  await ui.settle();
  ui.switch().props.onChange(true);
  await ui.settle();
  assert.equal(ui.switch().props.checked, false);
  assert.equal(ui.switch().props.disabled, true);
  assert.match(ui.error(), /尚未确认生效/);
  assert.deepEqual(ui.notices, []);
  ui.unmount();
});

test('a permission refusal cannot pretend to switch the pool and refresh restores the controls', async () => {
  const ui = harness((call) => {
    if (call.method === 'PUT') throw new Error('没有管理权限');
    return state();
  });
  await ui.settle();
  ui.switch().props.onChange(true);
  await ui.settle();
  assert.equal(ui.switch().props.checked, false);
  assert.equal(ui.switch().props.disabled, true);
  assert.match(ui.error(), /保存失败.*没有管理权限/);
  assert.deepEqual(ui.notices, []);
  ui.button('刷新状态').props.onClick();
  await ui.settle();
  assert.equal(ui.error(), undefined);
  assert.equal(ui.switch().props.disabled, false);
  ui.unmount();
});

test('malformed or contradictory pool snapshots keep every mutation disabled', async () => {
  for (const response of [
    null,
    { ...state(), enabled: 'false' },
    { ...state(), running: 'yes' },
    { ...state(), concurrency: '2' },
    { ...state(), rounds: null },
    { ...state(), lastError: 500 },
  ]) {
    const ui = harness(() => response);
    await ui.settle();
    assert.equal(ui.switch().props.disabled, true);
    ui.switch().props.onChange(true);
    await ui.settle();
    assert.equal(ui.calls.length, 1, 'no write may follow an unreadable snapshot');
    assert.match(ui.error(), /无法读取/);
    ui.unmount();
  }
});

test('unmount aborts an in-flight pool save without claiming success or starting another read', async () => {
  const write = deferred();
  const ui = harness((call) => (call.method === 'PUT' ? write.promise : state()));
  await ui.settle();
  ui.switch().props.onChange(true);
  const request = ui.calls.at(-1);
  ui.unmount();
  assert.equal(request.signal.aborted, true);
  write.resolve(state(true));
  await flush();
  assert.equal(ui.calls.length, 2);
  assert.deepEqual(ui.notices, []);
});

// ──────────────────────── 设置字段与状态展示 ────────────────────────

function loadModule(source) {
  const cache = loadModule.cache || (loadModule.cache = new Map());
  if (cache.has(source)) return cache.get(source).exports;
  const module = { exports: {} };
  cache.set(source, module);
  const require = createRequire(source);
  new Function('require', 'module', 'exports', compile(source))(
    (name) => {
      if (name.startsWith('.')) {
        for (const extension of ['.ts', '.tsx']) {
          const target = resolve(dirname(source), name + extension);
          if (existsSync(target)) return loadModule(target);
        }
      }
      return require(name);
    },
    module,
    module.exports,
  );
  return module.exports;
}
function loadHelpers(relative) {
  return loadModule(fileURLToPath(new URL(relative, import.meta.url)));
}

test('account pool settings expose exactly the four pool keys inside their documented bounds', () => {
  const { POOL_GROUPS } = loadHelpers('../src/pages/invitation/settings/config-values.ts');
  assert.equal(POOL_GROUPS.length, 1);
  assert.equal(POOL_GROUPS[0].title, '账号池自动补号');
  const fields = POOL_GROUPS[0].fields;
  assert.deepEqual(
    fields.map((field) => field.key),
    ['pool_min_available', 'pool_register_concurrency', 'pool_check_seconds', 'pool_otp_timeout'],
  );
  assert.deepEqual(
    fields.map((field) => [field.type, field.min, field.max]),
    [
      ['number', 0, 1000],
      ['number', 1, 10],
      ['number', 10, 3600],
      ['number', 10, 3600],
    ],
  );
  const concurrency = fields.find((field) => field.key === 'pool_register_concurrency');
  assert.match(concurrency.help, /独立/);
  assert.match(concurrency.help, /全局并发上限/);
  assert.match(concurrency.help, /17-32/);
  assert.match(concurrency.help, /1-16/);
  assert.match(concurrency.help, /互不影响|不会互相压制/);
  const otp = fields.find((field) => field.key === 'pool_otp_timeout');
  assert.match(otp.help, /账号工厂/);
  assert.match(otp.help, /邮箱验证码等待/);
  assert.match(otp.help, /互不影响/);
  const minimum = fields.find((field) => field.key === 'pool_min_available');
  assert.match(minimum.help, /账号工厂自动补号/);
});

test('mailbox accounts show three identity words and keep task state in colour plus hover', () => {
  const status = loadHelpers('../src/components/invitation-status.tsx');
  // 账号类型只有三个词（邮箱 / GPT / Codex）。「使用中 / 失败」是任务状态而不是身份，
  // 按运营要求改成颜色 + 悬浮说明；这两态的真实身份库里没有记录，所以统一显示「邮箱」，
  // 悬浮里写明这一点而不是猜一个可能为假的身份。
  const stages = [
    ['available', '邮箱', 'default', /只有邮箱/],
    ['in_use', '邮箱', 'warning', /使用中/],
    ['registered', 'GPT', 'processing', /已注册并绑好 2FA/],
    ['done', 'Codex', 'success', /已完成 Codex 授权/],
    ['failed', '邮箱', 'error', /上次任务失败/],
  ];
  for (const [value, label, tone, hint] of stages) {
    const outer = status.InvitationStatus({ status: value, context: 'mail-account' });
    // 外层是 Tooltip（悬浮说明），内层才是标签本体。
    assert.match(String(outer.props.title), hint, `${value} 的悬浮说明`);
    const tag = outer.props.children;
    assert.equal(tag.props.children, label, `${value} 只显示身份词`);
    assert.equal(tag.props.tone, tone, `${value} 的颜色必须是 ${tone}`);
  }
  // 五个状态必须两两不同色，否则「使用中 / 失败」这类任务状态就看不出来了。
  assert.equal(new Set(stages.map(([, , tone]) => tone)).size, stages.length);
  // 全局（非邮箱管理）标签不受影响，仍按运行状态显示。
  const global = status.InvitationStatus({ status: 'done' });
  assert.equal(global.props.children, '已完成');
  // 卡密列表上下文按运营口径走卡密文案：终态「已核销」（不再是「已完成」），
  // 未使用是橘黄色（tone=warning），与接码 / GPT / X 三个模块一致。
  // 运行记录里的 `done` 是任务跑完，仍走上面的通用标签（已完成），所以这里必须
  // 用 context="cdk" 显式区分，不能改全局 LABELS。
  const cdkDone = status.InvitationStatus({ status: 'done', context: 'cdk' });
  assert.equal(cdkDone.props.children, '已核销');
  assert.equal(cdkDone.props.tone, 'success');
  const cdkUnused = status.InvitationStatus({ status: 'unused', context: 'cdk' });
  assert.equal(cdkUnused.props.children, '未使用');
  assert.equal(cdkUnused.props.tone, 'warning', '未使用必须是橘黄色');
  assert.equal(
    status.InvitationStatus({ status: 'bound', context: 'cdk' }).props.children,
    '已绑定',
  );
});

test('single registration can target the redeemable account pool and locks the two token rules', () => {
  const path = new URL('../src/pages/invitation/register/page.tsx', import.meta.url);
  const render = (preferences) => {
    const module = { exports: {} };
    new Function('require', 'module', 'exports', compile(fileURLToPath(path)))(
      (name) => {
        if (name === './preferences')
          return { useRegistrationPreferences: () => [preferences, () => {}], registrationPayload: () => ({}) };
        if (name.endsWith('/admin-page'))
          return {
            AdminPage: ({ title, children }) =>
              React.createElement('main', null, React.createElement('h1', null, title), children),
          };
        if (name.endsWith('/invitation-run-panel')) return { InvitationRunPanel: () => null };
        if (name.endsWith('/invitation-api'))
          return {
            copyInvitationText: async () => {},
            invitationAction: async () => ({}),
            invitationApi: async () => ({}),
            invitationErrorMessage: (error) => String(error?.message || error),
          };
        if (name.endsWith('/use-invitation-query'))
          return { useInvitationQuery: () => ({ data: undefined, loading: false, error: '', reload: async () => {} }) };
        if (name.endsWith('/invitation-runtime'))
          return {
            useInvitationRuntime: () => ({
              runId: null,
              run: null,
              log: '',
              events: [],
              running: false,
              watchRun: () => {},
              clearRun: () => {},
              refresh: async () => {},
            }),
          };
        if (name.endsWith('/navigation'))
          return { Link: ({ href, children }) => React.createElement('a', { href }, children) };
        // 页面现在统一走共用中文映射；这里补上同名分支，避免深层相对导入落到 Node 解析器。
        if (name.endsWith('/zh-label'))
          return {
            resolveLabel: (map, key) => (key ? map?.[key] || `未知（${key}）` : '—'),
            resolveLabelInsensitive: (map, key) => (key ? map?.[key] || `未知（${key}）` : '—'),
            unknownLabel: (value) => (value ? `未知（${value}）` : '—'),
            yesNoLabel: () => '—',
          };
        if (name.endsWith('/zh-text')) return { localizedText: (value, empty) => value || empty || '—' };
        return createRequire(fileURLToPath(path))(name);
      },
      module,
      module.exports,
    );
    return renderToStaticMarkup(React.createElement(module.exports.default));
  };
  const base = {
    proxy: '',
    otpTimeout: 10,
    want2fa: true,
    wantAccessToken: true,
    wantSessionToken: true,
    wantRefreshToken: true,
    allowExistingLogin: true,
    poolOnly: false,
  };
  const off = render(base);
  assert.ok(off.includes('加入待兑换账号池（注册后停在 2FA，不做 Codex 授权）'));
  assert.equal(off.includes('ant-switch-disabled'), false);
  assert.equal(off.includes('ant-checkbox-wrapper-disabled'), false);
  const on = render({ ...base, want2fa: true, wantRefreshToken: false, poolOnly: true });
  assert.ok(on.includes('账号池注册在 Codex 授权前停止，此时还没有 Refresh Token'));
  assert.equal(on.split('ant-switch-disabled').length - 1, 1, 'only the 2FA switch is locked');
  assert.equal(
    on.split('ant-checkbox-wrapper-disabled').length - 1,
    1,
    'only the refresh-token checkbox is locked',
  );
});

test('run kinds keep automatic top-ups apart from manual single registrations', () => {
  const { runKindLabel } = loadHelpers('../src/lib/invitation.ts');
  assert.equal(runKindLabel('register_pool'), '自动补号');
  assert.equal(runKindLabel('register'), '单次注册');
  assert.equal(runKindLabel('codex_auth'), '邀请授权');
  // 未知类型显示为「未知（原始值）」，绝不误标成"单次注册"，也不漏英文
  assert.equal(runKindLabel('something_else'), '未知（something_else）');
  assert.equal(runKindLabel(undefined), '—');
});

test('the mail tab can pick a separate account-factory source', () => {
  const source = readFileSync(
    new URL('../src/pages/invitation/settings/config-editor.tsx', import.meta.url),
    'utf8',
  );
  // 补号来源必须是独立的一个键，并且在 mail 页签的字段集合里（否则存不进去）
  assert.match(source, /pool_mail_source/);
  assert.match(source, /补号邮箱来源/);
  assert.match(source, /跟随上方/);
  assert.equal((source.match(/field\.key === 'pool_mail_source'/g) || []).length, 1);
});

test('the proxy page shows both locks and can reset them one at a time', () => {
  const source = readFileSync(
    new URL('../src/pages/invitation/proxies/page.tsx', import.meta.url),
    'utf8',
  );
  // 两个用途维度必须各自成标签，且汇总按维度分开
  assert.match(source, /GPT 注册锁/);
  assert.match(source, /Codex 激活锁/);
  assert.match(source, /summary\?\.locks\?\.\[purpose\]/);
  // 永久不可用必须能按维度单独复位（否则不可逆）
  assert.match(source, /\/proxy\/pool\/reset\?purpose=\$\{purpose\}/);
  assert.match(source, /重置\$\{label\}/);
});

test('proxy lock values come from the two independent counters', () => {
  const source = readFileSync(
    new URL('../src/pages/invitation/proxies/page.tsx', import.meta.url),
    'utf8',
  );
  assert.match(source, /locks\?: Record<string, ProxyLock>/);
  assert.match(source, /frozenRemaining/);
  assert.match(source, /permanentDead/);
});

test('the sms tab can switch verification-code logging', () => {
  const { SMS_GROUPS } = loadHelpers('../src/pages/invitation/settings/config-values.ts');
  const field = SMS_GROUPS.flatMap((group) => group.fields).find(
    (item) => item.key === 'sms_log_otp',
  );
  assert.ok(field, 'sms_log_otp 必须能在后台配置');
  assert.equal(field.type, 'boolean');
  assert.match(field.help, /号码/);
});

test('the proxy page surfaces proxy health and can reset it alone', () => {
  const source = readFileSync(
    new URL('../src/pages/invitation/proxies/page.tsx', import.meta.url),
    'utf8',
  );
  // 代理自身不可用必须单独可见、单独可重置（业务两把锁不受影响）
  assert.match(source, /代理可用性/);
  assert.match(source, /summary\?\.health\?\.ready/);
  assert.match(source, /\/proxy\/pool\/reset\?purpose=health/);
  assert.match(source, /重置可用性/);
});

test('the sms-binding label is shown in both account lists and the pool card', () => {
  const helper = loadHelpers('../src/lib/invitation.ts');
  assert.equal(helper.phoneBoundLabel(1), '已接码');
  assert.equal(helper.phoneBoundLabel(0), '接码失败');
  assert.match(helper.phoneBoundLabel(-1), /未接码/);
  assert.equal(helper.phoneBoundTone(1), 'success');
  assert.equal(helper.phoneBoundTone(0), 'error');
  assert.equal(helper.phoneBoundTone(-1), 'warning');

  for (const page of ['mail-accounts/page.tsx', 'registered/page.tsx']) {
    const source = readFileSync(new URL(`../src/pages/invitation/${page}`, import.meta.url), 'utf8');
    assert.match(source, /InvitationPhoneStatus/, `${page} 必须显示统一的接码状态与证据`);
    assert.match(source, /InvitationPhoneRecord/, `${page} 必须读取统一的接码字段`);
  }
  const pool = readFileSync(new URL('../src/pages/invitation/settings/page.tsx', import.meta.url), 'utf8');
  assert.match(pool, /已接码 \{status\.smsBound/);
  assert.match(pool, /未接码 \{status\.smsUnbound/);
});

test('the proxy column never claims "直连" for unrecorded rows', () => {
  const helper = loadHelpers('../src/lib/invitation.ts');
  assert.equal(helper.proxyUsageLabel(1), '已用代理');
  assert.equal(helper.proxyUsageLabel(0), '直连');
  assert.equal(helper.proxyUsageLabel(-1), '未记录');
  assert.equal(helper.proxyUsageLabel(undefined), '未记录');
  assert.equal(helper.proxyUsageTone(1), 'success');
  assert.equal(helper.proxyUsageTone(-1), 'warning');

  for (const page of ['mail-accounts/page.tsx', 'registered/page.tsx']) {
    const source = readFileSync(new URL(`../src/pages/invitation/${page}`, import.meta.url), 'utf8');
    assert.match(source, /proxyUsageLabel\(row\.used_proxy/, `${page} 必须按 used_proxy 渲染`);
    assert.match(source, /Tooltip/, `${page} 代理详情要能悬浮查看`);
    assert.doesNotMatch(source, /value \|\| '直连'/, `${page} 不能再把空值当直连`);
  }
});

test('run records show a coloured tag per task type and can be filtered by it', () => {
  const helper = loadHelpers('../src/lib/invitation.ts');
  const kinds = ['register_pool', 'codex_auth', 'register'];
  const tones = kinds.map((kind) => helper.runKindTone(kind));
  assert.equal(new Set(tones).size, kinds.length, `不同类型必须不同颜色: ${tones}`);
  assert.equal(helper.runKindTone('unknown-kind'), 'default');
  for (const kind of kinds) {
    assert.notEqual(helper.runKindLabel(kind), kind, `${kind} 必须有中文标签`);
  }
  const options = helper.runKindOptions();
  assert.equal(options[0].value, '');
  assert.deepEqual(
    options.slice(1).map((item) => item.value).sort(),
    [...kinds].sort(),
  );

  const source = readFileSync(new URL('../src/pages/invitation/runs/page.tsx', import.meta.url), 'utf8');
  assert.match(source, /runKindTone\(value\)/, '类型列必须是彩色标签');
  assert.match(source, /aria-label="任务类型筛选"/, '必须有任务类型筛选');
  // 类型筛选现在在**服务端**生效：以前是"先截断最近 N 条再在浏览器里筛"，
  // 更早的记录永远查不到。
  assert.match(source, /kind=\$\{encodeURIComponent\(kind\)\}/, '筛选必须把 kind 交给服务端');
  assert.match(source, /showTotal/, '分页必须显示服务端总数');
  const overview = readFileSync(new URL('../src/pages/invitation/page.tsx', import.meta.url), 'utf8');
  assert.match(overview, /runKindTone\(kind\)/, '概览页类型列也要彩色标签');
});

test('the review closure is reachable and never claims to mark a task successful', () => {
  const runs = readFileSync(new URL('../src/pages/invitation/runs/page.tsx', import.meta.url), 'utf8');
  // 结果待复核必须真的在这个页面里，且走受权限保护的复核接口。
  assert.match(runs, /\/api\/v1\/admin\/invitation\/task-holds/);
  assert.match(runs, /resolve`/);
  assert.match(runs, /confirmAllExecutorsStopped: true/);
  assert.match(runs, /closeInterruptedTask/);
  assert.match(runs, /结果待复核/);
  assert.match(runs, /绝不会把结果改写成["“]?成功/, '必须说明复核不会把结果判成成功');
  // 占位页的说明要与实际的自动回收行为一致。
  assert.match(runs, /automaticReclaim/);
  assert.match(runs, /启动新执行进程时会清空上一进程的泄漏占位/);
  assert.doesNotMatch(runs, /崩溃或结果不明的执行占位不会自动释放/);
});

test('validation failures and stale data are visible instead of silent', () => {
  const runs = readFileSync(new URL('../src/pages/invitation/runs/page.tsx', import.meta.url), 'utf8');
  assert.match(runs, /lastLoadedAt/, '查询失败时必须显示数据更新时间');
  assert.match(runs, /可能已经过期/);
  const hook = readFileSync(new URL('../src/lib/use-invitation-query.ts', import.meta.url), 'utf8');
  assert.match(hook, /setLastLoadedAt\(Date\.now\(\)\)/);
  // 状态详情缺失时不能再默认显示"运行中"。
  const panel = readFileSync(new URL('../src/components/invitation-run-panel.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(panel, /runtime\.run\?\.status \|\| 'running'/);
  assert.match(panel, /状态查询失败/);
  assert.match(panel, /加载中/);
});

test('the overview explains a silent pool and stops calling registration authorisation', () => {
  const page = readFileSync(new URL('../src/pages/invitation/page.tsx', import.meta.url), 'utf8');
  assert.match(page, /blockedReason/, '必须显示"为什么没补号"');
  assert.match(page, /当前未补号：/);
  assert.match(page, /smsBound/);
  assert.match(page, /smsUnbound/);
  assert.match(page, /巡检线程运行中/);
  assert.match(page, /authorization/);
  // 注册成功不等于 Codex 授权完成：标签不能再写"Codex（已授权）"。
  assert.doesNotMatch(page, /'Codex（已授权）'/);
  assert.match(page, /单次注册完成/);
});

test('the realtime monitor lives only in run records, not in card management', () => {
  const cdk = readFileSync(new URL('../src/pages/invitation/cdk/page.tsx', import.meta.url), 'utf8');
  // 卡密管理与运行记录原本用同一个 /cdk/monitor，作业表/事件流水/全局计数逐项重复。
  // 按计划把整张监控卡从卡密管理删掉，所以这里钉住"不再请求、不再出现"。
  // 只检查真正的代码，不看注释：页面上仍保留一条解释为什么删掉监控卡的注释，
  // 里面会提到端点名，那正是我们希望保留的说明。
  const code = cdk.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
  assert.doesNotMatch(code, /实时执行监控/, '卡密管理不得再出现实时执行监控');
  assert.doesNotMatch(code, /cdk\/monitor/, '卡密管理不得再请求监控端点');
  assert.match(cdk, /运行记录/, '卡密管理必须告诉运营执行明细去哪里看');

  const runs = readFileSync(new URL('../src/pages/invitation/runs/page.tsx', import.meta.url), 'utf8');
  // 全局计数并入运行记录：监控端点与完整计数都必须在运行记录里。
  assert.match(runs, /cdk\/monitor/);
  assert.match(runs, /proxy_available/, '可用代理计数');
  assert.match(runs, /mail_pool/, '可兑换账号计数');
});

import { progressComponents } from './components.mjs';
import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const source = new URL('../src/components/invitation-redeem-panel.tsx', import.meta.url);
const require = createRequire(source);
const compile = (url) =>
  ts.transpileModule(readFileSync(url, 'utf8'), {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
const helperModule = { exports: {} };
new Function('module', 'exports', compile(new URL('../src/lib/invitation.ts', import.meta.url)))(
  helperModule,
  helperModule.exports,
);
const helpers = helperModule.exports;
const recoveryModule = { exports: {} };
new Function(
  'module',
  'exports',
  compile(new URL('../src/lib/invitation-recovery.ts', import.meta.url)),
)(recoveryModule, recoveryModule.exports);
const recovery = recoveryModule.exports;
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
const componentSource = compile(source);
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
afterEach(() => {
  for (const [key, descriptor] of [
    ['document', originalDocument],
    ['navigator', originalNavigator],
    ['window', originalWindow],
  ]) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
  globalThis.setTimeout = originalSetTimeout;
  globalThis.clearTimeout = originalClearTimeout;
});

class TestApiError extends Error {
  constructor(code, message, requestId) {
    super(message ?? code);
    this.code = code;
    this.requestId = requestId;
  }
}
const flush = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
};
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// A deterministic hook harness runs the actual component and its event/effect logic.
// Requests and timers stay local; no browser storage or upstream service is used.
function harness(routes = {}, options = {}) {
  const values = options.values || new Map();
  const storage = options.storage || {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { sessionStorage: storage },
  });
  const doc = new EventTarget();
  doc.hidden = false;
  Object.defineProperty(globalThis, 'document', { configurable: true, value: doc });
  const copied = [];
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      clipboard: {
        writeText: async (value) => {
          copied.push(value);
        },
      },
    },
  });
  const timers = new Map();
  let timerId = 0;
  globalThis.setTimeout = (fn, ms) => {
    const id = ++timerId;
    timers.set(id, { fn, ms });
    return id;
  };
  globalThis.clearTimeout = (id) => timers.delete(id);
  const calls = [];
  const slots = [];
  let cursor = 0;
  let tree;
  let effects = [];
  const same = (a, b) =>
    a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
  const hooks = {
    ...React,
    useState(initial) {
      const index = cursor++;
      if (!(index in slots))
        slots[index] = { value: typeof initial === 'function' ? initial() : initial };
      return [
        slots[index].value,
        (next) => {
          slots[index].value = typeof next === 'function' ? next(slots[index].value) : next;
        },
      ];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = { current: initial };
      return slots[index];
    },
    useCallback(fn, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = { fn, deps };
      return slots[index].fn;
    },
    useEffect(fn, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) {
        const previous = slots[index];
        slots[index] = { deps, cleanup: previous?.cleanup };
        effects.push(() => {
          slots[index].cleanup?.();
          slots[index].cleanup = fn();
        });
      }
    },
  };
  const mod = { exports: {} };
  const load = (name) => {
    if (name === './numeric-progress') return progressComponents;
    if (name === '../lib/dialog-focus') return { useDialogFocus: () => {} };
    if (name === 'react') return hooks;
    if (name === '../lib/invitation') return helpers;
    if (name === '../lib/invitation-recovery') return recovery;
    if (name === '../lib/api')
      return {
        ApiClientError: TestApiError,
        pollRetryDelay: () => 0,
        async apiFetch(path, init) {
          calls.push({ path, ...init, payload: init?.body ? JSON.parse(init.body) : null });
          const handler =
            routes[path] ||
            (path.includes('/redemption-attempts/')
              ? routes['/api/v1/invitation/redemption-attempts/*']
              : undefined);
          if (handler) return { data: await handler(calls.at(-1), calls) };
          if (path.endsWith('/page-content'))
            return {
              data: {
                capacity: { concurrency: 3 },
                canRedeem: true,
                canStart: true,
                poolAvailable: 500,
              },
            };
          throw new Error(`Unexpected API request: ${path}`);
        },
      };
    if (name === './error-with-request-id')
      return {
        UserFacingError: ({ err }) => React.createElement('p', null, err?.message || '操作失败'),
      };
    // 键盘焦点行为靠 effect 实现，静态渲染下不执行；这里只要模块可加载。
    if (name === '../lib/dialog-focus') return { useDialogFocus: () => {} };
    return require(name);
  };
  new Function('require', 'module', 'exports', componentSource)(load, mod, mod.exports);
  function render() {
    cursor = 0;
    tree = mod.exports.InvitationRedeemPanel();
    const pendingEffects = effects;
    effects = [];
    pendingEffects.forEach((effect) => effect());
    return tree;
  }
  function nodes(type) {
    const output = [];
    function visit(node) {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node)) {
        node.forEach(visit);
        return;
      }
      if (typeof node.type === 'function') { visit(node.type(node.props)); return; }
      if (node.type === type) output.push(node);
      React.Children.forEach(node.props?.children, visit);
    }
    visit(tree);
    return output;
  }
  const text = (node) => renderToStaticMarkup(node).replace(/<[^>]*>/g, '');
  render();
  return {
    calls,
    routes,
    copied,
    timers,
    doc,
    render,
    nodes,
    values,
    storage,
    html: () => renderToStaticMarkup(tree),
    button: (name) => nodes('button').find((node) => text(node).startsWith(name)),
    async redeem(value) {
      await flush();
      render();
      nodes('textarea')[0].props.onChange({ target: { value } });
      render();
      nodes('form')[0].props.onSubmit({ preventDefault() {} });
      await flush();
      render();
    },
    async click(name) {
      this.button(name).props.onClick();
      await flush();
      render();
    },
    async tick(ms) {
      const timer = [...timers.entries()].find(([, value]) => value.ms === ms);
      assert.ok(timer, `Missing ${ms}ms timer`);
      timers.delete(timer[0]);
      timer[1].fn();
      await flush();
      render();
    },
    async visibility(hidden) {
      doc.hidden = hidden;
      doc.dispatchEvent(new Event('visibilitychange'));
      await flush();
      render();
    },
    unmount() {
      slots.forEach((slot) => slot?.cleanup?.());
    },
  };
}

const fixtureIndex = (id) =>
  String(id).includes('-') ? Number.parseInt(String(id).split('-').at(-1), 16) - 1 : Number(id);
const fixtureId = (id) =>
  `00000000-0000-4000-8000-${(fixtureIndex(id) + 1).toString(16).padStart(12, '0')}`;
const fixtureSecret = (id) => (fixtureIndex(id) + 1).toString(16).padStart(64, '0');
const item = (id, overrides = {}) => ({
  line: fixtureIndex(id) + 1,
  id: fixtureId(id),
  email: `invite-${fixtureIndex(id)}@example.test`,
  sessionSecret: fixtureSecret(id),
  progressPercent: 0,
  displayResult: null,
  allowedActions: ['START', 'POLL'],
  ...overrides,
});
const batch = (items, concurrency = 3) => ({
  attemptStatus: 'completed',
  items,
  capacity: { concurrency, running: 0, queued: 0 },
});

test('fresh page stays empty after metadata load and never reads session history', async () => {
  const ui = harness();
  await flush();
  ui.render();
  assert.equal(ui.nodes('textarea')[0].props.value, '');
  assert.equal(ui.nodes('button').length, 1);
  assert.equal(ui.button('获取邮箱').props.disabled, true);
  assert.doesNotMatch(ui.html(), /progressbar|同时处理|邀请邮箱|全选|指南|待核验/);
  // "刷新不会自动显示历史记录"是输入区的一句说明；这里要求的是**没有历史行**。
  assert.equal(ui.nodes('li').length, 0);
  assert.doesNotMatch(ui.html(), /邀请进度|已领取/);
  assert.deepEqual(
    ui.calls.map(({ path }) => path),
    ['/api/v1/invitation/page-content'],
  );
  ui.unmount();
});

test('an empty page hides unavailable-start notices until the user explicitly retrieves a mailbox', async () => {
  const ui = harness(
    {
      '/api/v1/invitation/page-content': () => ({ canRedeem: true, canStart: false }),
      '/api/v1/invitation/redeem': () => batch([item('0', { status: 'bound' })]),
    },
    { values: recoveryValues([storedSession(0)]) },
  );
  await flush();
  ui.render();
  assert.doesNotMatch(ui.html(), /请稍候重试|已有邮箱仍可复制|invite-/);
  assert.equal(
    ui.calls.some(({ path }) => /status|redemption-attempts|redeem$/.test(path)),
    false,
  );
  await ui.redeem('CORRECT-CDK');
  assert.match(ui.html(), /invite-0@example.test/);
  assert.match(ui.html(), /请稍候重试。/);
  assert.equal(ui.button('开始选中').props.disabled, true);
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/start')),
    false,
  );
  ui.unmount();
});

const EXPECTED_REFUSAL = {
  INVITATION_MAIL_CONFIGURATION: '邮箱服务配置异常',
  INVITATION_MAIL_CONNECTION: '邮箱服务暂时连接不上',
  INVITATION_MAIL_UNAVAILABLE: '邮箱服务暂时不可用',
  INVITATION_MAIL_INSUFFICIENT: '当前可用账号不足',
  INVITATION_BUSY: '当前任务较多',
  INVITATION_START_DENIED: '这条记录现在无法开始',
  SERVICE_UNAVAILABLE: '服务暂时不可用',
};

test('service refusals expose only the unified retry prompt and keep the original CDK line', async () => {
  for (const code of [
    'INVITATION_MAIL_CONFIGURATION',
    'INVITATION_MAIL_CONNECTION',
    'INVITATION_MAIL_UNAVAILABLE',
    'INVITATION_MAIL_INSUFFICIENT',
    'INVITATION_BUSY',
    'INVITATION_START_DENIED',
    'SERVICE_UNAVAILABLE',
  ]) {
    const ui = harness({
      '/api/v1/invitation/redeem': () => ({
        ...batch([{ line: 1, error: { code, message: '执行未开启，代理池为空，需要管理员配置' } }]),
        attemptStatus: 'rejected',
      }),
    });
    await ui.redeem('\nCORRECT-CDK');
    // 文案要给出**下一步**，不能再都糊成"请稍候重试"。
    assert.match(ui.html(), new RegExp(`第 2 行：.*${EXPECTED_REFUSAL[code]}`));
    assert.doesNotMatch(ui.html(), /执行未开启|代理池|管理员|暂时无法开始|暂时无法获取邮箱/);
    ui.unmount();
  }
});

test('atomic batch rejection keeps line mapping and a later valid batch strips capabilities from rows', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () => ({
      ...batch([{ line: 2, error: { code: 'INVITATION_CDK_NOT_FOUND', message: '兑换码无效' } }]),
      attemptStatus: 'rejected',
    }),
  });
  await ui.redeem('  first-cdk\r\n\r\n bad-cdk \r\n third-cdk ');
  assert.deepEqual(ui.calls.at(-1).payload.codes, ['FIRST-CDK', 'BAD-CDK', 'THIRD-CDK']);
  assert.equal(ui.nodes('li').length, 1);
  assert.match(ui.html(), /第 3 行：.*卡密无效或不存在/);
  assert.equal(ui.nodes('div').filter((node) => node.props.role === 'progressbar').length, 0);
  ui.routes['/api/v1/invitation/redeem'] = () => batch([item('0'), item('2', { line: 2 })], 7);
  await ui.redeem('first-cdk\nthird-cdk');
  assert.doesNotMatch(ui.html(), /最多同时处理|CLI|重新检查服务|查询已有结果/);
  assert.doesNotMatch(ui.html(), new RegExp(fixtureSecret('0')));
  await ui.click('复制');
  assert.deepEqual(ui.copied, ['invite-0@example.test']);
  ui.unmount();
});

test('batch start submits every selected session once even when all server slots are occupied', async () => {
  const start = deferred();
  const initial = Array.from({ length: 8 }, (_, index) => item(String(index)));
  const ui = harness({
    '/api/v1/invitation/redeem': () => ({
      ...batch(initial, 2),
      capacity: { concurrency: 2, running: 2, queued: 40 },
    }),
    '/api/v1/invitation/start': () => start.promise,
  });
  await ui.redeem('a\nb\nc\nd\ne\nf\ng\nh');
  const button = ui.button('开始选中');
  button.props.onClick();
  button.props.onClick();
  ui.render();
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/start')).length, 1);
  assert.deepEqual(
    ui.calls.at(-1).payload.sessions,
    initial.map(({ id, sessionSecret }) => ({ id, sessionSecret })),
  );
  assert.equal(ui.button('开始选中').props.disabled, true);
  start.resolve(
    batch(
      initial.map((value) => ({ ...value, allowedActions: ['POLL'], progressPercent: 10 })),
      2,
    ),
  );
  await flush();
  ui.render();
  assert.equal(ui.button('开始').props.disabled, true);
  assert.ok([...ui.timers.values()].some(({ ms }) => ms === 2000));
  ui.unmount();
});

test('completed invitee workflow reaches100 before reward verification; uncertain states stay95', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () =>
      batch([item('0', { allowedActions: ['POLL'], progressPercent: 100 })]),
    '/api/v1/invitation/status': () =>
      batch([
        item('0', {
          allowedActions: [],
          progressPercent: 100,
          displayResult: 'SUCCESS',
          completionBasis: 'INVITEE_FLOW_CLI',
          cliCompleted: true,
          invitationStatus: 'pending',
          rewardVerified: false,
        }),
      ]),
  });
  await ui.redeem('a');
  assert.match(ui.html(), /aria-valuenow="95"/);
  assert.doesNotMatch(ui.html(), />成功<|待核验|阶段|日志|详情/);
  await ui.tick(15000);
  assert.match(ui.html(), /aria-valuenow="100"/);
  assert.match(ui.html(), />成功</);
  assert.match(ui.html(), /成功表示受邀流程完成，邀请奖励另行核验/);
  assert.equal(
    ui.nodes('button').filter((node) => node.props['aria-label']?.startsWith('开始邀请')).length,
    0,
  );
  assert.equal(ui.timers.size, 0);
  ui.unmount();
});

test('ten CDKs submit once and eleven reject locally without disturbing existing rows', async () => {
  const initial = Array.from({ length: 10 }, (_, index) => item(String(index)));
  const ui = harness({ '/api/v1/invitation/redeem': () => batch(initial) });
  await ui.redeem(initial.map((_, index) => `CDK-${index}`).join('\n'));
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.equal(ui.calls.at(-1).payload.codes.length, 10);
  assert.equal(ui.nodes('li').length, 10);
  assert.equal(ui.nodes('div').filter((node) => node.props.role === 'progressbar').length, 0);
  await ui.redeem(Array.from({ length: 11 }, (_, index) => `CDK-${index}`).join('\n'));
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.match(ui.html(), /每次最多兑换 10 份 CDK/);
  assert.equal(ui.nodes('li').length, 10);
  assert.equal(ui.nodes('div').filter((node) => node.props.role === 'progressbar').length, 0);
  ui.unmount();
});

test('confirmation carries only the selected capability and enables start after server acknowledgement', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () =>
      batch([
        item('0', { allowedActions: ['CONFIRM_INVITE'] }),
        item('1', { allowedActions: ['CONFIRM_INVITE'] }),
      ]),
    '/api/v1/invitation/invite-sent': ({ payload }) =>
      batch(payload.sessions.map(({ id }) => item(id))),
  });
  await ui.redeem('a\nb');
  assert.equal(ui.button('开始选中').props.disabled, true);
  await ui.click('我已发送邀请信');
  assert.equal(ui.calls.at(-1).path, '/api/v1/invitation/invite-sent');
  assert.deepEqual(ui.calls.at(-1).payload, {
    sessions: [{ id: fixtureId('0'), sessionSecret: fixtureSecret('0') }],
  });
  assert.equal(ui.button('开始选中').props.disabled, false);
  assert.doesNotMatch(ui.html(), /secret-0|secret-1/);
  ui.unmount();
});

test('source retry remains available only when the server grants START', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () =>
      batch([
        item('0', {
          displayResult: 'FAILED',
          allowedActions: ['START', 'POLL'],
          progressPercent: 90,
        }),
      ]),
    '/api/v1/invitation/start': () =>
      batch([item('0', { allowedActions: ['POLL'], progressPercent: 10 })]),
  });
  await ui.redeem('a');
  assert.equal(ui.button('重试').props.disabled, false);
  await ui.click('重试');
  assert.deepEqual(ui.calls.at(-1).payload.sessions, [
    { id: fixtureId('0'), sessionSecret: fixtureSecret('0') },
  ]);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/start')).length, 1);
  assert.equal(ui.button('开始').props.disabled, true);
  ui.unmount();
});

test('failed polls retain data, retry, pause while hidden and refetch on return', async () => {
  let polls = 0;
  const ui = harness({
    '/api/v1/invitation/redeem': () =>
      batch([item('0', { allowedActions: ['POLL'], progressPercent: 40 })]),
    '/api/v1/invitation/status': () => {
      if (++polls === 1) throw new TestApiError('NETWORK_ERROR', '网络异常', 'request-id');
      return batch([item('0', { allowedActions: ['POLL'], progressPercent: 60 })]);
    },
  });
  await ui.redeem('a');
  await ui.tick(2000);
  assert.match(ui.html(), /invite-0@example.test/);
  assert.match(ui.html(), /aria-valuenow="40"/);
  assert.match(ui.html(), /网络连接失败/);
  assert.ok([...ui.timers.values()].some(({ ms }) => ms === 2000));
  await ui.visibility(true);
  assert.equal(ui.timers.size, 0);
  assert.equal(polls, 1);
  await ui.visibility(false);
  assert.equal(polls, 2);
  assert.match(ui.html(), /aria-valuenow="60"/);
  ui.unmount();
});

test('a lost start response is reconciled before another start is allowed', async () => {
  const status = deferred();
  const ui = harness({
    '/api/v1/invitation/redeem': () => batch([item('0')]),
    '/api/v1/invitation/start': () => {
      throw new TestApiError('NETWORK_ERROR', '网络异常');
    },
    '/api/v1/invitation/status': () => status.promise,
  });
  await ui.redeem('a');
  await ui.click('开始选中');
  assert.equal(ui.button('开始').props.disabled, true);
  assert.equal(ui.calls.at(-1).path, '/api/v1/invitation/status');
  status.resolve(batch([item('0', { allowedActions: ['POLL'], progressPercent: 20 })]));
  await flush();
  ui.render();
  assert.equal(ui.button('开始').props.disabled, true);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/start')).length, 1);
  ui.unmount();
});

test('a stale visibility status response cannot restore START after a batch is accepted', async () => {
  const status = deferred();
  const ui = harness({
    '/api/v1/invitation/redeem': () => batch([item('0')]),
    '/api/v1/invitation/status': () => status.promise,
    '/api/v1/invitation/start': () =>
      batch([item('0', { allowedActions: ['POLL'], progressPercent: 10 })]),
  });
  await ui.redeem('a');
  await ui.visibility(true);
  await ui.visibility(false);
  const signal = ui.calls.findLast(({ path }) => path.endsWith('/status')).signal;
  await ui.click('开始选中');
  assert.equal(signal.aborted, true);
  status.resolve(batch([item('0')]));
  await flush();
  ui.render();
  assert.equal(ui.button('开始').props.disabled, true);
  assert.match(ui.html(), /aria-valuenow="10"/);
  ui.unmount();
});

test('per-item capability loss disables only the affected row and never invents a failure result', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () =>
      batch([item('0', { allowedActions: ['POLL'], progressPercent: 30 }), item('1')]),
    '/api/v1/invitation/status': () =>
      batch([
        {
          id: fixtureId('0'),
          error: { code: 'SESSION_SECRET_INVALID', message: '会话密钥无效，请重新兑换' },
          allowedActions: [],
        },
        item('1'),
      ]),
    '/api/v1/invitation/start': ({ payload }) =>
      batch(payload.sessions.map(({ id }) => item(id, { allowedActions: ['POLL'] }))),
  });
  await ui.redeem('a\nb');
  await ui.tick(2000);
  assert.match(ui.html(), /领取记录暂时无法使用/);
  assert.doesNotMatch(ui.html(), />失败</);
  await ui.click('开始选中');
  assert.deepEqual(ui.calls.at(-1).payload.sessions, [
    { id: fixtureId('1'), sessionSecret: fixtureSecret('1') },
  ]);
  ui.unmount();
});

test('explicit capability invalidation clears submitted capabilities; unmount aborts active requests', async () => {
  const pending = deferred();
  const ui = harness({
    '/api/v1/invitation/redeem': () => batch([item('0')]),
    '/api/v1/invitation/start': () => {
      const error = new TestApiError('SESSION_SECRET_INVALID', '会话失效');
      error.httpStatus = 401;
      throw error;
    },
  });
  await ui.redeem('a');
  await ui.click('开始选中');
  assert.equal(ui.button('开始').props.disabled, true);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/status')).length, 0);
  ui.routes['/api/v1/invitation/redeem'] = () => pending.promise;
  ui.nodes('form')[0].props.onSubmit({ preventDefault() {} });
  const signal = ui.calls.at(-1).signal;
  ui.unmount();
  assert.equal(signal.aborted, true);
  pending.resolve(batch([item('0')]));
  await flush();
  const fresh = harness();
  assert.equal(fresh.nodes('textarea')[0].props.value, '');
  assert.doesNotMatch(fresh.html(), /invite-0@example.test/);
  fresh.unmount();
});

test('row sanitization ignores unsupported actions, secrets, CDKs and failed100 percent', () => {
  assert.deepEqual(
    helpers.invitationRow({ ...item('0'), code: 'cdk', allowedActions: ['START', 'DELETE'] }),
    {
      id: fixtureId('0'),
      email: 'invite-0@example.test',
      progressPercent: 0,
      displayResult: null,
      status: 'bound',
      allowedActions: ['START'],
    },
  );
  const failed = helpers.invitationRow(
    item('0', { displayResult: 'FAILED', progressPercent: 100 }),
  );
  assert.equal(failed.progressPercent, 95);
  assert.deepEqual(failed.allowedActions, ['START', 'POLL']);
  assert.equal(helpers.invitationPollDelay([failed], new Set()), null);
});

const storedSession = (index, createdAt = Date.now()) => ({
  id: fixtureId(index),
  sessionSecret: fixtureSecret(index),
  line: Number(index) + 1,
  createdAt,
  expiresAt: createdAt + recovery.INVITATION_RECOVERY_TTL,
});
const storedAttempt = () => recovery.createInvitationAttempt([1, 4]);
const recoveryValues = (sessions = [], attempts = []) =>
  new Map([[recovery.INVITATION_RECOVERY_KEY, JSON.stringify({ version: 1, sessions, attempts })]]);

test('canonical duplicate codes submit once and rejected errors map back to first original lines', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () => ({
      ...batch([{ line: 2, error: { code: 'INVITATION_CDK_NOT_FOUND', message: '不存在' } }]),
      attemptStatus: 'rejected',
    }),
  });
  await ui.redeem(' first \n\n FIRST \n\n second ');
  assert.deepEqual(ui.calls.at(-1).payload.codes, ['FIRST', 'SECOND']);
  assert.match(ui.html(), /第 5 行：.*不存在/);
  assert.match(ui.html(), /第 3 行与第 1 行重复/);
  assert.equal(ui.values.size, 0);
  ui.unmount();
});

test('new valid batches merge by session ID and rejected batches retain every existing capability', async () => {
  const ui = harness({ '/api/v1/invitation/redeem': () => batch([item('0')]) });
  await ui.redeem('PRIVATE-CDK-FIRST');
  ui.routes['/api/v1/invitation/redeem'] = () => batch([item('1', { line: 1 })]);
  await ui.redeem('PRIVATE-CDK-SECOND');
  assert.equal(ui.nodes('li').length, 2);
  assert.equal(ui.nodes('div').filter((node) => node.props.role === 'progressbar').length, 0);
  ui.routes['/api/v1/invitation/redeem'] = () => ({
    ...batch([]),
    attemptStatus: 'rejected',
    message: '邮箱配置暂不可用',
  });
  await ui.redeem('PRIVATE-CDK-BAD');
  assert.match(ui.html(), /请稍后重试。|请稍候重试。/);
  assert.match(ui.html(), /invite-0@example.test/);
  assert.match(ui.html(), /invite-1@example.test/);
  const stored = JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY));
  assert.equal(stored.sessions.length, 2);
  assert.equal(stored.attempts.length, 0);
  assert.doesNotMatch(
    JSON.stringify(stored),
    /PRIVATE-CDK|example.test|allowedActions|progressPercent/,
  );
  ui.unmount();
});

test('confirmation while executor disabled preserves capabilities and requires an explicit start after enabling', async () => {
  let canStart = false;
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart,
      unavailableMessage: '执行器已关闭',
    }),
    '/api/v1/invitation/redeem': () =>
      batch([
        item('0', {
          status: 'bound',
          stageLabel: '待开始',
          allowedActions: ['CONFIRM_INVITE', 'POLL'],
        }),
      ]),
    '/api/v1/invitation/invite-sent': () =>
      batch([item('0', { status: 'bound', stageLabel: '待开始' })]),
    '/api/v1/invitation/start': () =>
      batch([item('0', { status: 'processing', allowedActions: ['POLL'] })]),
  });
  await ui.redeem('fixture');
  assert.match(ui.html(), /请稍候重试。/);
  assert.doesNotMatch(ui.html(), /执行器|CLI|会话|凭据|并发|重新检查服务|查询已有结果/);
  assert.match(ui.html(), /等待邀请信/);
  assert.equal(ui.nodes('div').filter((node) => node.props.role === 'progressbar').length, 0);
  await ui.click('我已发送邀请信');
  assert.match(ui.html(), /请稍候重试。/);
  assert.doesNotMatch(ui.html(), /progressbar|0%|等待执行|>待开始</);
  assert.equal(ui.button('开始选中').props.disabled, true);
  await ui.click('复制');
  assert.deepEqual(ui.copied, ['invite-0@example.test']);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/start')).length, 0);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/status')).length, 0);
  assert.equal(
    [...ui.timers.values()].some(({ ms }) => ms === 2000),
    false,
  );
  const stored = JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY));
  assert.equal(stored.sessions[0].sessionSecret, fixtureSecret('0'));
  canStart = true;
  ui.routes['/api/v1/invitation/status'] = () => batch([item('0', { status: 'bound' })]);
  await ui.visibility(true);
  await ui.visibility(false);
  assert.match(ui.html(), /尚未开始执行，请点击“开始”。/);
  assert.equal(ui.button('开始选中').props.disabled, false);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/start')).length, 0);
  assert.equal(ui.nodes('div').filter((node) => node.props.role === 'progressbar').length, 0);
  await ui.click('开始选中');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/start')).length, 1);
  assert.deepEqual(ui.calls.at(-1).payload.sessions, [
    { id: fixtureId('0'), sessionSecret: fixtureSecret('0') },
  ]);
  assert.equal(ui.nodes('div').filter((node) => node.props.role === 'progressbar').length, 1);
  ui.unmount();
});

test('only queued, running, done and failed tasks show progress; bound and review tasks explain the next step', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () =>
      batch([
        item('0', {
          status: 'bound',
          stageLabel: '待开始',
          allowedActions: ['CONFIRM_INVITE', 'POLL'],
        }),
        item('1', { status: 'bound', stageLabel: '待开始' }),
        item('2', {
          status: 'review',
          stageLabel: '正在接受邀请',
          allowedActions: ['POLL'],
          progressPercent: 30,
        }),
        item('3', { status: 'processing', allowedActions: ['POLL'], progressPercent: 0 }),
        item('4', { status: 'processing', allowedActions: ['POLL'], progressPercent: 30 }),
        item('5', {
          status: 'done',
          displayResult: 'SUCCESS',
          allowedActions: [],
          progressPercent: 100,
        }),
        item('6', { status: 'failed', displayResult: 'FAILED', progressPercent: 55 }),
      ]),
  });
  await ui.redeem('a\nb\nc\nd\ne\nf\ng');
  const bars = ui.nodes('div').filter((node) => node.props.role === 'progressbar');
  assert.deepEqual(
    bars.map((node) => node.props['aria-label']),
    [3, 4, 5, 6].map((index) => `invite-${index}@example.test 邀请进度`),
  );
  assert.deepEqual(
    bars.map((node) => node.props['aria-valuenow']),
    [0, 30, 100, 55],
  );
  assert.match(ui.html(), /等待邀请信.*尚未开始执行/);
  assert.match(ui.html(), /尚未开始执行，请点击“开始”。/);
  assert.match(ui.html(), /结果待确认，请联系客服.*勿重复领取/);
  assert.doesNotMatch(ui.html(), /正在接受邀请|>待开始</);
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/start')),
    false,
  );
  ui.unmount();
});

test('allocation button retries failed page reads; later read failures disable starts without losing emails', async () => {
  let checks = 0;
  const ui = harness({
    '/api/v1/invitation/page-content': () => {
      if (++checks !== 2) throw new TestApiError('NETWORK_ERROR', '网络异常');
      return { canRedeem: true, canStart: true };
    },
    '/api/v1/invitation/redeem': () => batch([item('0')]),
    '/api/v1/invitation/status': () => batch([item('0')]),
  });
  await flush();
  ui.render();
  assert.match(ui.html(), /请稍候重试。/);
  assert.equal(ui.button('获取邮箱').props.disabled, true);
  await ui.redeem('fixture');
  assert.equal(ui.button('开始选中').props.disabled, false);
  await ui.visibility(true);
  await ui.visibility(false);
  assert.equal(ui.button('开始选中').props.disabled, true);
  assert.match(ui.html(), /invite-0@example.test/);
  ui.unmount();
});

test('confirmation holds its session lock and no visibility poll can regress its acknowledgement', async () => {
  const confirmation = deferred(),
    oldStatus = deferred();
  const initial = item('0', { allowedActions: ['CONFIRM_INVITE', 'POLL'], status: 'bound' });
  const ui = harness({
    '/api/v1/invitation/redeem': () => batch([initial]),
    '/api/v1/invitation/invite-sent': () => confirmation.promise,
    '/api/v1/invitation/status': () => oldStatus.promise,
  });
  await ui.redeem('fixture');
  await ui.visibility(true);
  await ui.visibility(false);
  const oldSignal = ui.calls.findLast(({ path }) => path.endsWith('/status')).signal;
  const button = ui.button('我已发送邀请信');
  button.props.onClick();
  button.props.onClick();
  await flush();
  ui.render();
  assert.equal(oldSignal.aborted, true);
  await ui.visibility(true);
  await ui.visibility(false);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/status')).length, 1);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/invite-sent')).length, 1);
  confirmation.resolve(batch([item('0')]));
  await flush();
  ui.render();
  oldStatus.resolve(batch([initial]));
  await flush();
  ui.render();
  assert.equal(ui.button('开始选中').props.disabled, false);
  assert.equal(ui.button('我已发送邀请信'), undefined);
  ui.unmount();
});

test('refresh with saved mailboxes and receipts stays blank; explicit CDK lookup shows only its mailbox', async () => {
  const session = storedSession(0, Date.now() - 1000);
  const attempt = storedAttempt();
  const ui = harness(
    {
      '/api/v1/invitation/redeem': () => batch([item('0')]),
    },
    { values: recoveryValues([session, storedSession(1)], [attempt]) },
  );
  await flush();
  ui.render();
  await ui.visibility(true);
  await ui.visibility(false);
  assert.doesNotMatch(ui.html(), /invite-[01]@example.test|正在核对上次|正在确认领取结果/);
  assert.equal(
    ui.calls.some(({ path }) => /status|redemption-attempts|redeem$/.test(path)),
    false,
  );
  assert.equal(ui.nodes('textarea')[0].props.value, '');
  await ui.redeem('CORRECT-CDK-0');
  assert.match(ui.html(), /invite-0@example.test/);
  assert.doesNotMatch(ui.html(), /invite-1@example.test/);
  assert.deepEqual(
    ui.calls.filter(({ method }) => method === 'POST').map(({ path }) => path),
    ['/api/v1/invitation/redeem'],
  );
  assert.equal(
    JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).sessions[0].expiresAt,
    session.expiresAt,
  );
  assert.deepEqual(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).attempts, [attempt]);
  ui.unmount();
});

test('temporary current-page status errors retain records; only explicit invalid sessions are removed', async () => {
  let polls = 0;
  const ui = harness(
    {
      '/api/v1/invitation/redeem': () => batch([item('0'), item('1')]),
      '/api/v1/invitation/status': () => {
        if (++polls === 1) throw new TestApiError('SERVICE_UNAVAILABLE', '暂时不可用');
        return batch([
          { id: fixtureId(0), error: { code: 'SESSION_SECRET_INVALID', message: '会话失效' } },
          item('1'),
        ]);
      },
    },
    { values: recoveryValues([storedSession(0), storedSession(1)]) },
  );
  await flush();
  ui.render();
  await ui.redeem('FIRST\nSECOND');
  await ui.visibility(true);
  await ui.visibility(false);
  assert.equal(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).sessions.length, 2);
  await ui.visibility(true);
  await ui.visibility(false);
  assert.equal(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).sessions.length, 1);
  assert.match(ui.html(), /invite-1@example.test/);
  ui.unmount();
});

test('submission persists a private recovery attempt before POST and a dropped response finds the original result', async () => {
  let attempt;
  const ui = harness({
    '/api/v1/invitation/redeem': ({ payload }) => {
      attempt = JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).attempts[0];
      assert.equal(attempt.id, payload.attemptId);
      assert.equal(attempt.recoverySecret, payload.recoverySecret);
      assert.deepEqual(attempt.lines, [1]);
      throw new TestApiError('NETWORK_ERROR', '响应丢失');
    },
    '/api/v1/invitation/redemption-attempts/*': (call) => {
      assert.equal(new Headers(call.headers).get('X-Session-Secret'), attempt.recoverySecret);
      assert.equal(call.method, 'GET');
      return { ...batch([item('0')]), attemptId: attempt.id, attemptStatus: 'completed' };
    },
  });
  await ui.redeem('PRIVATE-CDK-LOST');
  assert.match(ui.html(), /invite-0@example.test/);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.equal(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).attempts.length, 0);
  ui.unmount();
});

test('passive pending receipts never query or block another explicitly submitted CDK', async () => {
  const attempt = storedAttempt();
  const ui = harness(
    {
      '/api/v1/invitation/redeem': () => batch([item('1', { line: 1 })]),
    },
    { values: recoveryValues([], [attempt]) },
  );
  await flush();
  ui.render();
  assert.doesNotMatch(ui.html(), /正在确认领取结果|联系客服|invite-/);
  assert.equal(
    ui.calls.some(({ path }) => path.includes('/redemption-attempts/')),
    false,
  );
  await ui.redeem('DIFFERENT-CDK');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.match(ui.html(), /invite-1@example.test/);
  assert.equal(
    ui.calls.some(({ path }) => path.includes('/redemption-attempts/')),
    false,
  );
  assert.equal(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).attempts.length, 1);
  assert.equal(
    [...ui.timers.values()].some(({ ms }) => ms === 2000),
    false,
  );
  ui.unmount();
});

test('only the current-page pending attempt polls until complete and never replays POST', async () => {
  const passive = storedAttempt();
  let activeId,
    polls = 0;
  const ui = harness(
    {
      '/api/v1/invitation/redeem': ({ payload }) => {
        activeId = payload.attemptId;
        return { ...batch([]), attemptId: activeId, attemptStatus: 'pending' };
      },
      '/api/v1/invitation/redemption-attempts/*': ({ path }) => {
        assert.ok(path.endsWith(activeId));
        assert.ok(!path.endsWith(passive.id));
        return ++polls === 1
          ? { ...batch([]), attemptId: activeId, attemptStatus: 'pending' }
          : { ...batch([item('1', { line: 1 })]), attemptId: activeId };
      },
    },
    { values: recoveryValues([], [passive]) },
  );
  await ui.redeem('CURRENT-CDK');
  assert.match(ui.html(), /正在确认领取结果/);
  assert.doesNotMatch(ui.html(), /invite-/);
  await ui.tick(2000);
  assert.match(ui.html(), /invite-1@example.test/);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.equal(polls, 2);
  assert.deepEqual(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).attempts, [passive]);
  ui.unmount();
});

test('fifty passive receipts remain private and block another POST before exceeding the storage budget', async () => {
  const attempts = Array.from({ length: 50 }, () => storedAttempt());
  const ui = harness({}, { values: recoveryValues([], attempts) });
  await ui.redeem('NEW-CDK');
  assert.match(ui.html(), /请在新的标签页输入卡密查看/);
  assert.doesNotMatch(ui.html(), /invite-|正在确认领取结果/);
  assert.equal(
    ui.calls.some(({ path }) => /status|redemption-attempts|redeem$/.test(path)),
    false,
  );
  assert.deepEqual(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).attempts, attempts);
  ui.unmount();
});

test('a current-page lost response merges valid sessions and clears only explicitly reset sessions', async () => {
  const ui = harness(
    {
      '/api/v1/invitation/redeem': () => {
        throw new TestApiError('NETWORK_ERROR', 'lost');
      },
      '/api/v1/invitation/redemption-attempts/*': () => ({
        ...batch([
          {
            id: fixtureId(0),
            line: 1,
            error: { code: 'INVITATION_SESSION_NOT_FOUND', message: '会话已重置，请联系管理员' },
          },
          item('1', { line: 2 }),
        ]),
        attemptStatus: 'completed',
      }),
      '/api/v1/invitation/status': () => batch([item('1')]),
    },
    { values: recoveryValues([storedSession(0)]) },
  );
  await flush();
  ui.render();
  await ui.redeem('FIRST\n\n\nSECOND');
  const stored = JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY));
  assert.deepEqual(
    stored.sessions.map(({ id }) => id),
    [fixtureId(1)],
  );
  assert.equal(stored.attempts.length, 0);
  assert.match(ui.html(), /invite-1@example.test/);
  assert.match(ui.html(), /领取记录暂时无法使用/);
  ui.unmount();
});

test('storage write failure blocks new POST but leaves existing copy and queries available', async () => {
  const values = recoveryValues([storedSession(0)]);
  let failWrite = false;
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem(key, value) {
      if (failWrite) throw new Error('quota');
      values.set(key, value);
    },
    removeItem: (key) => values.delete(key),
  };
  const ui = harness(
    {
      '/api/v1/invitation/redeem': () => batch([item('0')]),
      '/api/v1/invitation/status': () => batch([item('0')]),
    },
    { values, storage },
  );
  await flush();
  ui.render();
  await ui.redeem('EXISTING-CDK');
  failWrite = true;
  await ui.redeem('new-fixture');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.match(ui.html(), /请允许网站保存数据/);
  assert.match(ui.html(), /invite-0@example.test/);
  await ui.click('复制');
  assert.deepEqual(ui.copied, ['invite-0@example.test']);
  ui.unmount();
});

test('local unsafe code checks never register an attempt or contact the server', async () => {
  const ui = harness();
  await ui.redeem('iv2$encrypted\n' + 'x'.repeat(161));
  assert.match(ui.html(), /第 1 行：.*格式或长度/);
  assert.match(ui.html(), /第 2 行：.*格式或长度/);
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/redeem')),
    false,
  );
  assert.equal(ui.values.size, 0);
  ui.unmount();
});

test('all explicitly submitted status and selected start requests stay at or below ten sessions', async () => {
  const limit = helpers.INVITATION_BATCH_LIMIT;
  assert.equal(limit, 10, '前端上限必须与服务端一致');
  // 页面自身最多保留 50 条记录，所以用满 50 条来测"按 10 切分"最准确。
  const total = 50;
  const sessions = Array.from({ length: total }, (_, index) => storedSession(index));
  const ui = harness(
    {
      '/api/v1/invitation/redeem': ({ payload }) =>
        batch(
          payload.codes.map((code, index) => item(code.replace('CDK-', ''), { line: index + 1 })),
        ),
      '/api/v1/invitation/status': ({ payload }) => {
        assert.ok(payload.sessions.length <= limit);
        return batch(payload.sessions.map(({ id }) => item(id)));
      },
      '/api/v1/invitation/start': ({ payload }) => {
        assert.ok(payload.sessions.length <= limit);
        return batch(
          payload.sessions.map(({ id }) =>
            item(id, {
              status: 'processing',
              allowedActions: ['POLL'],
              queuePosition: 3,
              stageLabel: '等待处理',
            }),
          ),
        );
      },
    },
    { values: recoveryValues(sessions) },
  );
  await flush();
  await flush();
  ui.render();
  for (let offset = 0; offset < total; offset += limit) {
    await ui.redeem(
      sessions
        .slice(offset, offset + limit)
        .map(({ id }) => `CDK-${fixtureIndex(id)}`)
        .join('\n'),
    );
  }
  await ui.visibility(true);
  await ui.visibility(false);
  // 每批最多 limit 条，且所有批次加起来正好覆盖全部记录（不丢不重）。
  const expected = [10, 10, 10, 10, 10];
  const chunksOf = (endpoint) =>
    ui.calls.filter(({ path }) => path.endsWith(endpoint)).map((call) => call.payload.sessions.length);
  assert.deepEqual(chunksOf('/status'), expected);
  assert.ok(chunksOf('/status').every((size) => size <= limit));
  assert.equal(chunksOf('/status').reduce((a, b) => a + b, 0), total);
  await ui.click('开始选中');
  await flush();
  ui.render();
  assert.deepEqual(chunksOf('/start'), expected);
  assert.ok(chunksOf('/start').every((size) => size <= limit));
  assert.equal(chunksOf('/start').reduce((a, b) => a + b, 0), total);
  assert.doesNotMatch(ui.html(), /排队位置|等待处理/);
  assert.match(ui.html(), /role="progressbar"/);
  assert.doesNotMatch(ui.html(), /等待处理/);
  ui.unmount();
});

test('a structured validation refusal releases only its attempt without a recovery GET', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () => {
      const error = new TestApiError('VALIDATION_ERROR', '参数无效');
      error.structuredResponse = true;
      throw error;
    },
  });
  await ui.redeem('fixture');
  assert.equal(
    ui.calls.some(({ path }) => path.includes('/redemption-attempts/')),
    false,
  );
  assert.equal(ui.values.size, 0);
  assert.equal(ui.button('获取邮箱').props.disabled, false);
  ui.unmount();
});

test('a confirmed pre-business rate refusal releases its receipt and allows an explicit retry after cooldown', async () => {
  const originalNow = Date.now;
  let now = originalNow();
  Date.now = () => now;
  let refusals = 1;
  const ui = harness({
    '/api/v1/invitation/redeem': () => {
      if (refusals-- > 0) {
        const error = new TestApiError('RATE_LIMIT_EXCEEDED', '操作过于频繁');
        Object.assign(error, { structuredResponse: true, httpStatus: 429, retryAfterMs: 17000 });
        throw error;
      }
      return batch([item('0')]);
    },
  });
  try {
    await ui.redeem('first-card');
    assert.equal(ui.calls.some(({ path }) => path.includes('/redemption-attempts/')), false);
    assert.equal(ui.values.size, 0);
    assert.equal(ui.button('获取邮箱').props.disabled, true);
    await ui.redeem('second-card');
    assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
    now += 17000;
    await ui.tick(17000);
    assert.equal(ui.button('获取邮箱').props.disabled, false);
    await ui.redeem('second-card');
    assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 2);
    assert.match(ui.html(), /invite-0@example.test/);
  } finally { ui.unmount(); Date.now = originalNow; }
});

test('an unstructured gateway 429 keeps the recovery receipt and never replays an uncertain redemption', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () => {
      const error = new TestApiError('SERVICE_UNAVAILABLE');
      Object.assign(error, { structuredResponse: false, httpStatus: 429, retryAfterMs: 17000 });
      throw error;
    },
    '/api/v1/invitation/redemption-attempts/*': () => { throw new TestApiError('NETWORK_ERROR'); },
  });
  await ui.redeem('first-card');
  assert.equal(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).attempts.length, 1);
  await ui.redeem('second-card');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.ok(ui.calls.filter(({ path }) => path.includes('/redemption-attempts/')).length >= 2);
  ui.unmount();
});

test('unknown attempt states, duplicate receipt IDs or lines and invalid line ranges stay under review', async () => {
  for (const receipt of [
    { ...batch([item('0')]), attemptStatus: 'unexpected' },
    batch([item('0', { line: 0 })]),
    batch([item('0', { line: 3 })]),
    batch([item('0'), item('0', { line: 2 })]),
    batch([item('0'), item('1', { line: 1 })]),
  ]) {
    const ui = harness({
      '/api/v1/invitation/redeem': () => receipt,
      '/api/v1/invitation/redemption-attempts/*': () => receipt,
    });
    await ui.redeem('one\ntwo');
    assert.match(ui.html(), /结果暂时无法核对/);
    assert.equal(ui.button('获取邮箱').props.disabled, false);
    assert.equal(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).attempts.length, 1);
    ui.unmount();
  }
});

test('completed recovery reads cannot regress a known session after a newer start', async () => {
  const read = deferred();
  const ui = harness({
    '/api/v1/invitation/redeem': () => batch([item('0')]),
    '/api/v1/invitation/start': () =>
      batch([item('0', { allowedActions: ['POLL'], status: 'processing' })]),
    '/api/v1/invitation/redemption-attempts/*': () => read.promise,
  });
  await ui.redeem('one');
  ui.routes['/api/v1/invitation/redeem'] = () => {
    throw new TestApiError('NETWORK_ERROR', '丢响应');
  };
  await ui.redeem('two');
  await ui.click('开始选中');
  read.resolve(batch([item('0')]));
  await flush();
  ui.render();
  assert.equal(ui.button('开始选中').props.disabled, true);
  assert.doesNotMatch(ui.html(), /排队中/);
  assert.match(ui.html(), /role="progressbar"/);
  ui.unmount();
});

test('current server availability from a recovered status disables a formerly enabled executor', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () => batch([item('0')]),
    '/api/v1/invitation/status': () => ({
      ...batch([item('0')]),
      canRedeem: true,
      canStart: false,
      unavailableMessage: '执行器正在维护',
    }),
  });
  await ui.redeem('fixture');
  await ui.visibility(true);
  await ui.visibility(false);
  assert.equal(ui.button('开始选中').props.disabled, true);
  assert.match(ui.html(), /请稍候重试。/);
  assert.doesNotMatch(ui.html(), /执行器正在维护/);
  ui.unmount();
});

test('reentering an already used CDK returns the same mailbox and live progress even while new mail is unavailable', async () => {
  let progress = 30;
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({ canRedeem: false, canStart: true }),
    '/api/v1/invitation/redeem': () =>
      batch([
        item('0', {
          status: 'processing',
          progressPercent: progress,
          allowedActions: ['POLL'],
          stageLabel: '正在接受邀请',
        }),
      ]),
  });
  await ui.redeem('SAME-CDK');
  const first = JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).sessions[0];
  progress = 70;
  await ui.redeem('SAME-CDK');
  assert.equal(ui.nodes('li').length, 1);
  assert.match(ui.html(), /invite-0@example.test/);
  assert.doesNotMatch(ui.html(), /处理中|正在接受邀请/);
  assert.match(ui.html(), /aria-valuenow="70"/);
  assert.deepEqual(JSON.parse(ui.values.get(recovery.INVITATION_RECOVERY_KEY)).sessions[0], first);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 2);
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/start')),
    false,
  );
  ui.unmount();
});

test('a late used-CDK response cannot overwrite the newer accepted start for the same session', async () => {
  const oldRead = deferred();
  const ui = harness({
    '/api/v1/invitation/redeem': () => batch([item('0')]),
    '/api/v1/invitation/start': () =>
      batch([item('0', { status: 'processing', allowedActions: ['POLL'] })]),
  });
  await ui.redeem('SAME-CDK');
  ui.routes['/api/v1/invitation/redeem'] = () => oldRead.promise;
  await ui.redeem('SAME-CDK');
  await ui.click('开始选中');
  oldRead.resolve(batch([item('0')]));
  await flush();
  ui.render();
  assert.doesNotMatch(ui.html(), /排队中/);
  assert.match(ui.html(), /role="progressbar"/);
  assert.equal(ui.button('开始选中').props.disabled, true);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/start')).length, 1);
  ui.unmount();
});

test('full tab records permit old-CDK lookup and preserve new receipt credentials beyond the session storage limit', async () => {
  const oldSessions = Array.from({ length: 200 }, (_, index) => storedSession(index));
  const values = recoveryValues(oldSessions);
  const status = ({ payload }) => batch(payload.sessions.map(({ id }) => item(id)));
  const ui = harness(
    {
      '/api/v1/invitation/status': status,
      '/api/v1/invitation/redeem': () => batch([item('0')]),
    },
    { values },
  );
  await flush();
  await flush();
  ui.render();
  await ui.redeem('OLD-CDK');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  const before = JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY));
  assert.equal(before.sessions.length, 200);
  assert.equal(before.attempts.length, 0);
  ui.routes['/api/v1/invitation/redeem'] = () => batch([item('200', { line: 1, status: 'bound' })]);
  await ui.redeem('NEW-CDK');
  const after = JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY));
  assert.deepEqual(after.sessions, before.sessions);
  assert.equal(after.attempts.length, 1);
  assert.match(ui.html(), /invite-200@example.test/);
  assert.match(ui.html(), /本页领取记录较多/);
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/start')),
    false,
  );
  ui.routes['/api/v1/invitation/redeem'] = () => batch([item('0')]);
  await ui.redeem('OLD-CDK-AFTER-OVERFLOW');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 3);
  assert.deepEqual(
    JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY)).sessions,
    before.sessions,
  );
  ui.unmount();
  const restored = harness(
    {
      '/api/v1/invitation/status': status,
      '/api/v1/invitation/redemption-attempts/*': () =>
        batch([item('200', { line: 1, status: 'bound' })]),
      '/api/v1/invitation/redeem': () => batch([item('0')]),
    },
    { values },
  );
  await flush();
  await flush();
  restored.render();
  assert.doesNotMatch(restored.html(), /invite-\d+@example.test/);
  const persisted = JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY));
  assert.deepEqual(persisted.sessions, before.sessions);
  assert.deepEqual(persisted.attempts, after.attempts);
  assert.equal(
    restored.calls.some(({ path }) => /status|redemption-attempts|redeem$|start$/.test(path)),
    false,
  );
  await restored.redeem('OLD-CDK-AFTER-REFRESH');
  assert.match(restored.html(), /invite-0@example.test/);
  assert.doesNotMatch(restored.html(), /invite-200@example.test/);
  assert.equal(restored.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.deepEqual(
    JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY)).sessions,
    before.sessions,
  );
  restored.unmount();
});

test('an explicit correct-CDK submission renews an expired record while a refresh preserves the new fixed deadline', async () => {
  const originalNow = Date.now;
  let now = originalNow();
  Date.now = () => now;
  try {
    const old = storedSession(0, now - recovery.INVITATION_RECOVERY_TTL + 10000);
    const values = recoveryValues([old]);
    const routes = {
      '/api/v1/invitation/status': () => batch([item('0')]),
      '/api/v1/invitation/redeem': () => batch([item('0')]),
    };
    const ui = harness(routes, { values });
    await flush();
    ui.render();
    now += 20000;
    await ui.redeem('SAME-CDK');
    const updated = JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY)).sessions[0];
    assert.equal(updated.createdAt, now);
    assert.equal(updated.expiresAt, now + recovery.INVITATION_RECOVERY_TTL);
    ui.unmount();
    now += 5000;
    const refreshed = harness(routes, { values });
    await flush();
    refreshed.render();
    assert.deepEqual(JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY)).sessions[0], updated);
    assert.equal(
      refreshed.calls.some(({ path }) => path.endsWith('/redeem') || path.endsWith('/start')),
      false,
    );
    refreshed.unmount();
  } finally {
    Date.now = originalNow;
  }
});

test('public errors and stage labels use business text without backend terms, URLs, hosts or paths', async () => {
  const leak =
    '执行失败 https://internal.example/secret 10.1.2.3 /var/data/private Traceback worker session';
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart: false,
      unavailableMessage: leak,
    }),
    '/api/v1/invitation/redeem': () =>
      batch([item('0', { status: 'processing', allowedActions: ['POLL'], stageLabel: leak })]),
  });
  await ui.redeem('FIRST');
  ui.routes['/api/v1/invitation/redeem'] = () => ({
    ...batch([{ line: 1, error: { code: 'INVITATION_CDK_NOT_FOUND', message: leak } }]),
    attemptStatus: 'rejected',
  });
  await ui.redeem('\nBAD');
  assert.match(ui.html(), /第 2 行：.*卡密无效或不存在/);
  ui.routes['/api/v1/invitation/redeem'] = () => ({
    ...batch([]),
    attemptStatus: 'review',
    message: leak,
  });
  await ui.redeem('REVIEW');
  assert.match(ui.html(), /领取结果待确认，请联系客服/);
  assert.doesNotMatch(
    ui.html(),
    /执行器|会话|凭据|凭证|后台|并发|worker|CLI|internal\.example|10\.1\.2\.3|\/var\/data|Traceback|重新检查服务|查询已有结果/,
  );
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/start')),
    false,
  );
  ui.unmount();
});

test('mixed receipts remain passive on refresh, including a dropped POST response', async () => {
  const originalNow = Date.now;
  const start = originalNow();
  let now = start;
  Date.now = () => now;
  try {
    for (const dropped of [false, true]) {
      now = start;
      const oldSessions = Array.from({ length: 200 }, (_, index) => storedSession(index, start));
      const values = recoveryValues(oldSessions);
      const delayedRead = deferred();
      const receipt = batch([
        item('0', { line: 1, status: 'bound', allowedActions: ['CONFIRM_INVITE', 'POLL'] }),
        item('200', { line: 2, status: 'bound', allowedActions: ['CONFIRM_INVITE', 'POLL'] }),
      ]);
      const ui = harness(
        {
          '/api/v1/invitation/status': ({ payload }) =>
            batch(payload.sessions.map(({ id }) => item(id))),
          '/api/v1/invitation/redeem': () => {
            if (dropped) throw new TestApiError('NETWORK_ERROR', 'lost response');
            return receipt;
          },
          '/api/v1/invitation/redemption-attempts/*': () => delayedRead.promise,
        },
        { values },
      );
      await flush();
      await flush();
      ui.render();
      now = start + 20 * 3600000;
      await ui.redeem('OLD-CDK\nNEW-CDK');
      const pending = JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY));
      assert.equal(pending.attempts.length, 1);
      if (dropped)
        assert.equal(
          pending.attempts[0].knownSessionExpirations[0].expiresAt,
          start + recovery.INVITATION_RECOVERY_TTL,
        );
      else assert.deepEqual(pending.attempts[0].restoreIds, [fixtureId(200)]);
      ui.unmount();
      now = start + 25 * 3600000;
      const restored = harness(
        { '/api/v1/invitation/redemption-attempts/*': () => receipt },
        { values },
      );
      await flush();
      await flush();
      restored.render();
      assert.doesNotMatch(restored.html(), /invite-\d+@example.test/);
      assert.equal(
        restored.nodes('button').filter((node) => node.props.children === '我已发送邀请信').length,
        0,
      );
      const saved = JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY));
      assert.deepEqual(saved.sessions, []);
      assert.deepEqual(saved.attempts, pending.attempts);
      assert.equal(
        restored.calls.some(({ path }) => /status|redemption-attempts|redeem$|start$/.test(path)),
        false,
      );
      restored.unmount();
    }
  } finally {
    Date.now = originalNow;
  }
});

test('compacted overflow receipts retain the original deadline of a known in-memory mailbox', async () => {
  const originalNow = Date.now;
  const start = originalNow();
  let now = start + 10 * 3600000;
  Date.now = () => now;
  try {
    const values = recoveryValues(
      Array.from({ length: 200 }, (_, index) => storedSession(index, now)),
    );
    const receipt = batch([
      item('200', { line: 1, status: 'bound', allowedActions: ['CONFIRM_INVITE', 'POLL'] }),
    ]);
    const ui = harness(
      {
        '/api/v1/invitation/status': ({ payload }) =>
          batch(payload.sessions.map(({ id }) => item(id))),
        '/api/v1/invitation/redeem': () => receipt,
      },
      { values },
    );
    await flush();
    await flush();
    ui.render();
    now = start + 20 * 3600000;
    await ui.redeem('OVERFLOW-CDK');
    now = start + 30 * 3600000;
    await ui.redeem('OVERFLOW-CDK');
    const stored = JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY));
    assert.equal(stored.attempts.length, 2);
    assert.deepEqual(stored.attempts[1].knownSessionExpirations, [
      { id: fixtureId(200), expiresAt: start + 44 * 3600000 },
    ]);
    ui.unmount();
    now = start + 45 * 3600000;
    const restored = harness(
      { '/api/v1/invitation/redemption-attempts/*': () => receipt },
      { values },
    );
    await flush();
    await flush();
    restored.render();
    assert.doesNotMatch(restored.html(), /invite-200@example.test/);
    assert.equal(
      restored.nodes('button').some((node) => node.props.children === '我已发送邀请信'),
      false,
    );
    const retained = JSON.parse(values.get(recovery.INVITATION_RECOVERY_KEY));
    assert.equal(retained.sessions.length, 0);
    assert.equal(retained.attempts.length, 1);
    assert.deepEqual(retained.attempts[0].knownSessionExpirations, [
      { id: fixtureId(200), expiresAt: start + 44 * 3600000 },
    ]);
    assert.equal(
      restored.calls.some(({ path }) => /status|redemption-attempts|redeem$|start$/.test(path)),
      false,
    );
    restored.unmount();
  } finally {
    Date.now = originalNow;
  }
});

test('a backward clock jump cannot silently discard an attempt and send an unprotected redemption', async () => {
  const originalNow = Date.now;
  let now = originalNow();
  Date.now = () => now;
  try {
    const values = recoveryValues([storedSession(0, now)]);
    const originalRecord = values.get(recovery.INVITATION_RECOVERY_KEY);
    const ui = harness({ '/api/v1/invitation/status': () => batch([item('0')]) }, { values });
    await flush();
    ui.render();
    now -= 6 * 60000;
    await ui.redeem('SAME-CDK');
    assert.equal(
      ui.calls.some(({ path }) => path.endsWith('/redeem')),
      false,
    );
    assert.equal(values.get(recovery.INVITATION_RECOVERY_KEY), originalRecord);
    assert.match(ui.html(), /当前浏览器无法保存领取记录/);
    assert.doesNotMatch(ui.html(), /invite-0@example.test/);
    assert.equal(ui.button('复制'), undefined);
    ui.unmount();
  } finally {
    Date.now = originalNow;
  }
});

test('the account count moved out of the panel into the page title row', async () => {
  // 「当前可用账号」按运营要求挪到标题行右侧（components/redeem-intro.tsx），
  // 面板不再重复渲染同一个数字。数字本身的语义（必须是实数、未知就隐藏、
  // 绝不显示"不限量"）由 tests/redeem-intro.test.mjs 覆盖。
  const limited = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart: true,
      poolAvailable: 3,
    }),
  });
  await flush();
  limited.render();
  assert.doesNotMatch(limited.html(), /当前可用账号/);
  limited.unmount();
  const unknown = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart: true,
      poolAvailable: -1,
    }),
  });
  await flush();
  unknown.render();
  assert.doesNotMatch(unknown.html(), /不限量/);
  assert.doesNotMatch(unknown.html(), /当前可用账号/);
  unknown.unmount();
});

test('an empty account pool no longer blocks the submission locally', async () => {
  // 回归：旧实现在区分新旧卡密**之前**就按库存 0 直接拒绝，于是"重新输入原 CDK
  // 查原邮箱/进度"也被挡住 —— 而后端对已绑定的卡密根本不需要库存。库存只做提示，
  // 权威判定交回后端。
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart: true,
      poolAvailable: 0,
    }),
    '/api/v1/invitation/redeem': () => batch([item('0')]),
  });
  await flush();
  ui.render();
  await ui.redeem('POOL-EMPTY');
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/redeem')),
    true,
  );
  assert.deepEqual(ui.calls.at(-1).payload.codes, ['POOL-EMPTY']);
  assert.match(ui.html(), /invite-0@example.test/);
  ui.unmount();
});

test('a pool smaller than the submitted batch confirms a subset with original line mapping', async () => {
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart: true,
      poolAvailable: 1,
    }),
    '/api/v1/invitation/redeem': () => ({
      ...batch([{ line: 1, error: { code: 'INVITATION_CDK_NOT_FOUND', message: '不存在' } }]),
      attemptStatus: 'rejected',
    }),
  });
  await flush();
  ui.render();
  await ui.redeem('\nFIRST\nSECOND');
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/redeem')),
    false,
  );
  assert.match(ui.html(), /可用账号不足/);
  assert.match(ui.html(), /当前可用账号 1 个，少于你提交的 2 份 CDK/);
  assert.match(ui.html(), /继续兑换前 1 份/);
  assert.equal(ui.values.size, 0);
  await ui.click('继续兑换前 1 份');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.deepEqual(ui.calls.at(-1).payload.codes, ['FIRST']);
  assert.match(ui.html(), /第 2 行：.*卡密无效或不存在/);
  assert.doesNotMatch(ui.html(), /可用账号不足/);
  ui.unmount();
});

test('cancelling the smaller batch dialog keeps the entered codes and posts nothing', async () => {
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart: true,
      poolAvailable: 1,
    }),
    '/api/v1/invitation/redeem': () => batch([item('0')]),
  });
  await flush();
  ui.render();
  await ui.redeem('FIRST\nSECOND');
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/redeem')),
    false,
  );
  await ui.click('取消');
  assert.equal(
    ui.calls.some(({ path }) => path.endsWith('/redeem')),
    false,
  );
  assert.equal(ui.nodes('textarea')[0].props.value, 'FIRST\nSECOND');
  assert.doesNotMatch(ui.html(), /可用账号不足/);
  assert.equal(ui.values.size, 0);
  ui.unmount();
});

test('a server pool rejection reopens the smaller batch dialog instead of a generic error', async () => {
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart: true,
      poolAvailable: 3,
    }),
    '/api/v1/invitation/redeem': () => ({
      ...batch([
        { line: 1, error: { code: 'INVITATION_POOL_INSUFFICIENT', message: '账号池不足' } },
        { line: 2, error: { code: 'INVITATION_POOL_INSUFFICIENT', message: '账号池不足' } },
      ]),
      attemptStatus: 'rejected',
      poolAvailable: 1,
      poolRequested: 2,
    }),
  });
  await flush();
  ui.render();
  await ui.redeem('FIRST\nSECOND');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 1);
  assert.match(ui.html(), /可用账号不足/);
  assert.match(ui.html(), /当前可用账号 1 个，少于你提交的 2 份 CDK/);
  assert.doesNotMatch(ui.html(), /请稍候重试/);
  assert.equal(ui.values.size, 0);
  const previous = ui.calls.at(-1).payload.attemptId;
  ui.routes['/api/v1/invitation/redeem'] = () => batch([item('0', { line: 1 })]);
  await ui.click('继续兑换前 1 份');
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 2);
  assert.deepEqual(ui.calls.at(-1).payload.codes, ['FIRST']);
  assert.notEqual(ui.calls.at(-1).payload.attemptId, previous);
  assert.match(ui.html(), /invite-0@example.test/);
  assert.equal(
    ui.calls.some(({ path }) => path.includes('/redemption-attempts/')),
    false,
  );
  ui.unmount();
});

test('repeated pool rejections stop prompting after three rounds and show the retry error', async () => {
  let pooled = 0;
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem: true,
      canStart: true,
      poolAvailable: 5,
    }),
    '/api/v1/invitation/redeem': ({ payload }) => {
      pooled += 1;
      return {
        ...batch(
          payload.codes.map((_, index) => ({
            line: index + 1,
            error: { code: 'INVITATION_POOL_INSUFFICIENT', message: '账号池不足' },
          })),
        ),
        attemptStatus: 'rejected',
        poolAvailable: payload.codes.length - 1,
      };
    },
  });
  await flush();
  ui.render();
  await ui.redeem('A\nB\nC\nD\nE');
  assert.match(ui.html(), /继续兑换前 4 份/);
  await ui.click('继续兑换前 4 份');
  assert.match(ui.html(), /继续兑换前 3 份/);
  await ui.click('继续兑换前 3 份');
  assert.match(ui.html(), /继续兑换前 2 份/);
  await ui.click('继续兑换前 2 份');
  assert.equal(pooled, 4);
  assert.equal(ui.calls.filter(({ path }) => path.endsWith('/redeem')).length, 4);
  assert.match(ui.html(), /可用账号不足，请稍候重试。/);
  assert.equal(ui.button('继续兑换前'), undefined);
  assert.equal(
    ui.calls.some(({ path }) => path.includes('/redemption-attempts/')),
    false,
  );
  assert.equal(ui.values.size, 0);
  ui.unmount();
});

test('a queued row explains its state instead of showing a dead start button', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () => ({
      attemptStatus: 'completed',
      items: [
        item('0', {
          status: 'processing',
          progressPercent: 15,
          queuePosition: 2,
          allowedActions: ['POLL'],
        }),
      ],
      capacity: { concurrency: 3, running: 1, queued: 1 },
    }),
  });
  await flush();
  ui.render();
  await ui.redeem('QUEUED-CDK');
  // 旧实现只把 action 按钮置灰，看起来像坏了；现在要说明"在排队、要等"。
  assert.match(ui.html(), /aria-valuenow="15"/);
  assert.doesNotMatch(ui.html(), /排队中|排队位置|第 2 位|处理中|执行中/);
  // 这一行不该再出现"开始邀请"按钮（旧实现会摆一个灰着的）。
  assert.equal(
    ui.nodes('button').filter((node) => String(node.props?.['aria-label'] || '').startsWith('开始邀请'))
      .length,
    0,
  );
  ui.unmount();
});

test('batch results report every failing row plus success and failure counts', async () => {
  const ui = harness({
    '/api/v1/invitation/redeem': () => batch([item('0'), item('1')]),
  });
  await flush();
  ui.render();
  await ui.redeem('\nFIRST\nSECOND');
  // 同一批里一行成功、一行失败：不能只把第一条错误抛成全局错误。
  ui.routes['/api/v1/invitation/start'] = () => ({
    attemptStatus: 'completed',
    items: [
      item('0', { status: 'processing', allowedActions: ['POLL'] }),
      { id: fixtureId('1'), email: 'invite-1@example.test', error: { code: 'INVITATION_BUSY', message: 'busy' } },
    ],
    capacity: { concurrency: 3, running: 1, queued: 0 },
  });
  // 领取成功后带 START 的行会自动勾选，直接点批量开始即可。
  await ui.click('开始选中（2）');
  assert.match(ui.html(), /成功 1 条，失败 1 条/);
  assert.match(ui.html(), /invite-1@example.test：/);
  assert.match(ui.html(), /当前任务较多，请稍后重试。/);
  ui.unmount();
});

test('an unavailable service schedules a read-only metadata refresh and starts nothing', async () => {
  let canRedeem = false;
  const ui = harness({
    '/api/v1/invitation/page-content': () => ({
      canRedeem,
      canStart: true,
      poolAvailable: 3,
      unavailableMessage: '请稍候重试。',
    }),
  });
  await flush();
  ui.render();
  // 空闲页面以前不会自愈；现在安排一次低频只读重取，且绝不触发开始。
  assert.ok([...ui.timers.values()].some(({ ms }) => ms === 30000), '必须安排一次只读重取');
  assert.equal(ui.calls.some(({ path }) => path.endsWith('/start')), false);
  assert.equal(ui.calls.some(({ path }) => path.endsWith('/invite-sent')), false);
  canRedeem = true;
  const timer = [...ui.timers.entries()].find(([, value]) => value.ms === 30000);
  ui.timers.delete(timer[0]);
  await timer[1].fn();
  await flush();
  ui.render();
  assert.doesNotMatch(ui.html(), /邮箱服务暂时不可用/);
  ui.unmount();
});

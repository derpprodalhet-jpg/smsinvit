import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const source = new URL('../src/pages/invitation/settings/page.tsx', import.meta.url);
const require = createRequire(source);
const React = require('react');
const compiled = ts.transpileModule(readFileSync(source, 'utf8'), {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.ReactJSX,
  },
}).outputText;
const PATH = '/api/v1/admin/invitation/executor';
const state = (enabled = false) => ({
  enabled,
  ready: enabled,
  state: enabled ? 'ready' : 'disabled',
  message: enabled ? '已开启' : '已关闭',
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
    tree = module.exports.InvitationExecutorControl();
    effects.splice(0).forEach((effect) => effect());
    return tree;
  };
  render();
  return {
    calls,
    notices,
    render,
    switch: () => nodes(tree).find((node) => node.type === 'Switch'),
    button: (label) =>
      nodes(tree).find((node) => node.type === 'Button' && node.props.children === label),
    error: () => nodes(tree).find((node) => node.type === 'Alert')?.props.message,
    link: () => nodes(tree).find((node) => node.type === 'Link'),
    texts: () =>
      nodes(tree)
        .filter((node) => node.type === 'Text')
        .map((node) => node.props.children),
    settle: async () => {
      await flush();
      render();
    },
    unmount: () => slots.forEach((slot) => slot.cleanup?.()),
    page: () => module.exports.default(),
    control: module.exports.InvitationExecutorControl,
  };
}

test('executor config reads authoritative status on mount without saving and remains above all existing tabs', async () => {
  const ui = harness();
  assert.equal(ui.switch().props.disabled, true);
  await ui.settle();
  assert.deepEqual(
    ui.calls.map(({ path, method }) => ({ path, method })),
    [{ path: PATH, method: undefined }],
  );
  assert.equal(ui.switch().props.checked, false);
  assert.equal(ui.button('开启邀请执行').props.disabled, false);
  const page = ui.page();
  assert.equal(page.props.children[0].type, ui.control);
  assert.equal(page.props.children[1].type.name, 'InvitationPoolControl');
  const tabs = page.props.children[2].props.items;
  assert.deepEqual(
    tabs.map(({ key }) => key),
    ['mail', 'sms', 'export', 'invite', 'pool'],
  );
  assert.equal(tabs[4].label, '账号池');
  assert.equal(tabs[4].children.props.section, 'pool');
  ui.unmount();
});

test('enabled but unavailable execution shows its specific backend reason and the existing proxy configuration link', async () => {
  const reason = '已开启，但代理池暂无可用代理';
  const ui = harness(() => ({
    enabled: true,
    ready: false,
    state: 'unavailable',
    message: reason,
  }));
  await ui.settle();
  assert.equal(ui.switch().props.checked, true);
  assert.equal(ui.button('关闭邀请执行').props.disabled, false);
  assert.ok(ui.texts().includes(reason));
  assert.equal(ui.link().props.href, '/invitation/proxies');
  assert.equal(ui.link().props.children, '配置代理池');
  assert.equal(ui.calls.length, 1);
  ui.unmount();
});

test('switch submits one PUT and only shows enabled after a successful authoritative reread', async () => {
  const write = deferred(),
    read = deferred();
  const ui = harness((call, calls) =>
    call.method === 'PUT' ? write.promise : calls.length === 1 ? state() : read.promise,
  );
  await ui.settle();
  const change = ui.switch().props.onChange;
  change(true);
  change(true);
  ui.render();
  assert.equal(ui.switch().props.checked, false);
  assert.equal(ui.switch().props.loading, true);
  assert.deepEqual(
    ui.calls.filter(({ method }) => method === 'PUT').map(({ payload }) => payload),
    [{ enabled: true }],
  );
  write.resolve(state(true));
  await ui.settle();
  assert.equal(ui.calls.length, 3);
  assert.equal(ui.switch().props.checked, false);
  assert.deepEqual(ui.notices, []);
  read.resolve(state(true));
  await ui.settle();
  assert.equal(ui.switch().props.checked, true);
  assert.equal(ui.button('关闭邀请执行').props.disabled, false);
  assert.deepEqual(ui.notices, ['邀请执行已开启']);
  ui.unmount();
});

test('closing waits for reread and changes only the executor enabled flag', async () => {
  let enabled = true;
  const ui = harness((call) => {
    if (call.method === 'PUT') enabled = call.payload.enabled;
    return state(enabled);
  });
  await ui.settle();
  ui.button('关闭邀请执行').props.onClick();
  await ui.settle();
  assert.equal(ui.switch().props.checked, false);
  assert.deepEqual(
    ui.calls.map(({ method }) => method || 'GET'),
    ['GET', 'PUT', 'GET'],
  );
  assert.deepEqual(ui.calls[1].payload, { enabled: false });
  assert.deepEqual(ui.notices, ['邀请执行已关闭']);
  ui.unmount();
});

test('a permission refusal cannot pretend to save; refresh restores the verified controls', async () => {
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

test('failed or contradictory post-save reads never show a success message', async () => {
  for (const failed of [true, false]) {
    const ui = harness((call, calls) => {
      if (calls.length > 2 && failed) throw new Error('状态暂时不可用');
      return state(call.method === 'PUT');
    });
    await ui.settle();
    ui.switch().props.onChange(true);
    await ui.settle();
    assert.equal(ui.switch().props.checked, false);
    assert.equal(ui.switch().props.disabled, true);
    assert.match(ui.error(), failed ? /状态暂时不可用/ : /尚未确认生效/);
    assert.deepEqual(ui.notices, []);
    ui.unmount();
  }
});

test('unreadable or malformed initial status keeps all mutations disabled', async () => {
  for (const response of [
    null,
    { enabled: 'false', ready: true, state: 'ready', message: 'bad' },
  ]) {
    const ui = harness(() => response);
    await ui.settle();
    assert.equal(ui.switch().props.disabled, true);
    ui.switch().props.onChange(true);
    await ui.settle();
    assert.equal(ui.calls.length, 1);
    assert.match(ui.error(), /无法读取/);
    ui.unmount();
  }
});

test('unmount aborts an in-flight save without claiming success or starting another read', async () => {
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

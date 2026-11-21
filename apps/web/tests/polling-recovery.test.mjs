import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';
import { progressComponents } from './components.mjs';

const flush = async () => { for (let n = 0; n < 16; n++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const compile = url => ts.transpileModule(readFileSync(url, 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;

// Run real event handlers and effects against a clock, storage and transport we can control.
// Only the SMS panel is driven here; 邀请页 has its own harness in invitation-recovery.test.mjs.
function harness(routes) {
  const source = new URL('../src/components/sms-redeem-panel.tsx', import.meta.url);
  const require = createRequire(source);
  const originals = Object.fromEntries(['window', 'document', 'sessionStorage'].map(key =>
    [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const originalTimers = [globalThis.setTimeout, globalThis.clearTimeout, globalThis.setInterval, globalThis.clearInterval];
  const originalNow = Date.now;
  let now = originalNow();
  Date.now = () => now;
  const timers = new Map();
  let timerId = 0;
  const addTimer = (fn, delay = 0, interval = 0) => {
    const id = ++timerId;
    timers.set(id, { fn, at: now + delay, interval });
    return id;
  };
  globalThis.setTimeout = (fn, ms) => addTimer(fn, ms);
  globalThis.clearTimeout = id => timers.delete(id);
  globalThis.setInterval = (fn, ms) => addTimer(fn, ms, ms);
  globalThis.clearInterval = id => timers.delete(id);
  const values = new Map();
  const storage = { getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  const doc = new EventTarget();
  doc.hidden = false;
  doc.visibilityState = 'visible';
  const win = Object.assign(new EventTarget(), { sessionStorage: storage,
    setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout,
    setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval });
  Object.defineProperty(globalThis, 'window', { configurable: true, value: win });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: doc });
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage });
  const calls = [], slots = [];
  let cursor = 0, pendingEffects = [], tree;
  const equal = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
  const hooks = {
    ...React,
    useState(initial) {
      const index = cursor++;
      if (!slots[index]) slots[index] = { value: typeof initial === 'function' ? initial() : initial };
      return [slots[index].value, next => {
        slots[index].value = typeof next === 'function' ? next(slots[index].value) : next;
      }];
    },
    useRef(value) { const index = cursor++; return slots[index] ||= { current: value }; },
    useCallback(fn, deps) {
      const index = cursor++;
      if (!equal(slots[index]?.deps, deps)) slots[index] = { value: fn, deps };
      return slots[index].value;
    },
    useMemo(fn, deps) {
      const index = cursor++;
      if (!equal(slots[index]?.deps, deps)) slots[index] = { value: fn(), deps };
      return slots[index].value;
    },
    useEffect(fn, deps) {
      const index = cursor++;
      if (!equal(slots[index]?.deps, deps)) {
        const cleanup = slots[index]?.cleanup;
        slots[index] = { deps, cleanup };
        pendingEffects.push(() => { cleanup?.(); slots[index].cleanup = fn(); });
      }
    },
  };
  const rules = { exports: {} };
  new Function('module', 'exports', compile(new URL('../src/lib/recovery-rules.ts', import.meta.url)))(rules, rules.exports);
  const mod = { exports: {} };
  new Function('require', 'module', 'exports', compile(source))(name => {
    if (name === 'react') return hooks;
    if (name === '../lib/recovery-rules') return rules.exports;
    if (name === '../lib/dialog-focus') return { useDialogFocus: () => {} };
    if (name === './numeric-progress') return progressComponents;
    if (name === './error-with-request-id') return { UserFacingError: ({ err }) => React.createElement('p', null, err.message) };
    if (name === '../lib/api') return {
      pollRetryDelay: () => 0,
      async apiFetch(path, init = {}) {
        const call = { path, ...init, payload: init.body ? JSON.parse(init.body) : null };
        calls.push(call);
        const route = routes[path];
        if (!route) throw new Error(`Unexpected request: ${path}`);
        return { data: await route(call, calls) };
      },
    };
    return require(name);
  }, mod, mod.exports);
  const Component = mod.exports.SmsRedeemPanel;
  const render = () => {
    cursor = 0;
    tree = Component({});
    const effects = pendingEffects;
    pendingEffects = [];
    effects.forEach(effect => effect());
    return tree;
  };
  const settle = async () => { for (let n = 0; n < 3; n++) { await flush(); render(); } };
  const nodes = type => {
    const found = [];
    const walk = node => {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node)) { node.forEach(walk); return; }
      if (node.type === type) found.push(node);
      React.Children.forEach(node.props?.children, walk);
    };
    walk(tree);
    return found;
  };
  const button = name => nodes('button').find(node => renderToStaticMarkup(node).includes(name));
  const advance = async ms => {
    const until = now + ms;
    for (let n = 0; n < 10000; n++) {
      const next = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      const [id, timer] = next;
      now = timer.at;
      if (timer.interval) timer.at += timer.interval;
      else timers.delete(id);
      timer.fn();
      await settle();
    }
    now = until;
    await settle();
  };
  render();
  return {
    calls, values, timers, button, settle, advance, render,
    html: () => renderToStaticMarkup(tree),
    async submitCode(value) {
      const input = nodes('input')[0];
      input.props.onChange({ target: { value } });
      render();
      nodes('form')[0].props.onSubmit({ preventDefault() {} });
      await settle();
    },
    async click(name) { button(name).props.onClick(); await settle(); },
    async visible(visible) {
      doc.hidden = !visible;
      doc.visibilityState = visible ? 'visible' : 'hidden';
      doc.dispatchEvent(new Event('visibilitychange'));
      await settle();
    },
    async online() { win.dispatchEvent(new Event('online')); await settle(); },
    close() {
      slots.forEach(slot => slot?.cleanup?.());
      for (const [key, descriptor] of Object.entries(originals)) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
      [globalThis.setTimeout, globalThis.clearTimeout, globalThis.setInterval, globalThis.clearInterval] = originalTimers;
      Date.now = originalNow;
    },
  };
}

const smsSession = { id: 'sms-session', sessionSecret: 'sms-proof', phone: '+12025550123',
  status: 'WAITING_CODE', keyStatus: 'REDEEMING', attempt: 1, maxAttempts: 3 };
const smsReads = h => h.calls.filter(call => call.path.includes('/sessions/'));

test('SMS unchanged retry response keeps the change-number action available', async () => {
  const current = { ...smsSession, keyStatus: 'LOCKED', canRetry: true, cancelGraceSeconds: 30 };
  const h = harness({
    '/api/v1/sms/redeem': () => current,
    '/api/v1/sms/sessions/sms-session': () => current,
    '/api/v1/sms/sessions/sms-session/retry': () => current,
  });
  try {
    await h.submitCode('valid-card');
    await h.click('获取验证码');
    await h.advance(30000);
    assert.equal(h.button('换号').props.disabled, false);
    await h.click('换号');
    assert.ok(h.button('换号'), 'an unchanged number must not restart a full listening cycle');
    assert.equal(h.button('换号').props.disabled, false);
    assert.equal(h.button('获取验证码'), undefined);
    assert.equal(h.calls.filter(call => call.path.endsWith('/retry')).length, 1);
  } finally { h.close(); }
});

test('SMS a genuinely new number resets to fetch-code without replaying retry', async () => {
  const current = { ...smsSession, keyStatus: 'LOCKED', canRetry: true, numberInvalid: true, status: 'CANCELLED' };
  const fresh = { ...smsSession, id: 'new-session', phone: '+13105550123', keyStatus: 'LOCKED', attempt: 2, canRetry: false };
  const h = harness({
    '/api/v1/sms/redeem': () => current,
    '/api/v1/sms/sessions/sms-session/retry': () => fresh,
    '/api/v1/sms/sessions/new-session': () => fresh,
  });
  try {
    await h.submitCode('valid-card');
    await h.click('换号');
    assert.ok(h.button('获取验证码'));
    assert.equal(h.button('换号'), undefined);
    assert.match(h.html(), /310/);
    assert.equal(h.calls.filter(call => call.path.endsWith('/retry')).length, 1);
  } finally { h.close(); }
});

test('SMS waits for slow status reads, stops hidden-tab traffic and immediately shows the code on return', async () => {
  const pending = deferred();
  let read = 0;
  const h = harness({
    '/api/v1/sms/redeem': () => smsSession,
    '/api/v1/sms/sessions/sms-session': call => {
      assert.equal(call.headers['X-Session-Secret'], 'sms-proof');
      return ++read === 1 ? pending.promise : { ...smsSession, status: 'SUCCESS', keyStatus: 'USED', smsCode: '123456' };
    },
  });
  try {
    await h.submitCode('valid-card');
    assert.equal(smsReads(h).length, 1);
    await h.click('获取验证码');
    await h.advance(6000);
    assert.equal(smsReads(h).length, 1, 'interval and fetch click share the pending read');
    const signal = smsReads(h)[0].signal;
    await h.visible(false);
    assert.equal(signal.aborted, true);
    await h.advance(20000);
    assert.equal(smsReads(h).length, 1, 'hidden intervals contact no API');
    await h.visible(true);
    assert.equal(smsReads(h).length, 2);
    assert.match(h.html(), /123456/);
    pending.resolve(smsSession);
    await h.settle();
    assert.match(h.html(), /123456/, 'aborted old read cannot remove the new code');
    assert.equal(h.calls.filter(call => call.path.endsWith('/redeem')).length, 1);
  } finally { h.close(); }
});

test('SMS visible listening keeps one-second reads and online refreshes once without creating a new redemption', async () => {
  const h = harness({
    '/api/v1/sms/redeem': () => smsSession,
    '/api/v1/sms/sessions/sms-session': () => smsSession,
  });
  try {
    await h.submitCode('valid-card');
    await h.click('获取验证码');
    const before = smsReads(h).length;
    await h.advance(3000);
    assert.equal(smsReads(h).length - before, 3);
    await h.online();
    assert.equal(smsReads(h).length - before, 4);
    assert.equal(h.calls.filter(call => call.path.endsWith('/redeem')).length, 1);
  } finally { h.close(); }
});

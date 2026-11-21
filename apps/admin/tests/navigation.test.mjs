import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
const source = new URL('../src/lib/navigation.tsx', import.meta.url);
const compiled = ts.transpileModule(readFileSync(source, 'utf8'), {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.ReactJSX,
  },
}).outputText;
const original = Object.getOwnPropertyDescriptor(globalThis, 'window');
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
afterEach(() => {
  if (original) Object.defineProperty(globalThis, 'window', original);
  else delete globalThis.window;
  if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument);
  else delete globalThis.document;
});
function harness(base = '/admin/') {
  const window = new EventTarget();
  window.location = new URL(`http://localhost${base}login`);
  window.location.assign = () => {
    throw new Error('unexpected full-page navigation');
  };
  window.scrollTo = () => {};
  const entries = [];
  window.history = {
    pushState(_data, _title, url) {
      entries.push(url);
      window.location = new URL(url, window.location);
    },
    replaceState(_data, _title, url) {
      entries.push(url);
      window.location = new URL(url, window.location);
    },
  };
  globalThis.window = window;
  globalThis.document = { querySelector: () => ({ getAttribute: () => base }) };
  const mod = { exports: {} };
  const require = createRequire(source);
  new Function('require', 'module', 'exports', compiled)(
    (name) =>
      name === 'react'
        ? { useSyncExternalStore: (_subscribe, snapshot) => snapshot() }
        : require(name),
    mod,
    mod.exports,
  );
  return { ...mod.exports, entries, window };
}
test('static admin navigation preserves the /admin base and filter query', () => {
  const h = harness();
  const router = h.useRouter();
  router.push('/batches?skuId=fixture');
  assert.equal(h.window.location.pathname, '/admin/batches');
  assert.equal(h.window.location.search, '?skuId=fixture');
  assert.equal(h.usePathname(), '/batches');
  assert.equal(h.useRouter(), router);
  router.replace('/admin/security');
  assert.equal(h.window.location.pathname, '/admin/security');
});
test('deployment entry drives login, deep links, queries and boundary matching', () => {
  const h = harness('/admin-v1/');
  assert.equal(h.usePathname(), '/login');
  assert.equal(h.adminHref('/'), '/admin-v1/');
  assert.equal(h.adminHref('/security'), '/admin-v1/security');
  assert.equal(h.adminHref('/admin-v1/security'), '/admin-v1/security');
  assert.equal(h.adminHref('/admin-v10'), '/admin-v1/admin-v10');
  assert.equal(h.adminHref('https://example.test'), 'https://example.test');
  h.useRouter().push('/sms/redeems?status=done#result');
  assert.equal(h.window.location.pathname, '/admin-v1/sms/redeems');
  assert.equal(h.window.location.search, '?status=done');
  assert.equal(h.window.location.hash, '#result');
  assert.equal(h.usePathname(), '/sms/redeems');
  h.useRouter().replace('/login');
  assert.equal(h.window.location.pathname, '/admin-v1/login');
  assert.equal(
    h.Link({ href: '/invitation/registered', target: '_blank' }).props.href,
    '/admin-v1/invitation/registered',
  );
});
test('links retain native open-in-new-tab behavior and unsaved-edit cancellation', () => {
  const h = harness();
  const link = h.Link({ href: '/sms/redeems' });
  assert.equal(link.props.href, '/admin/sms/redeems');
  let prevented = false;
  link.props.onClick({
    button: 0,
    ctrlKey: true,
    preventDefault() {
      prevented = true;
    },
  });
  assert.equal(prevented, false);
  assert.equal(h.entries.length, 0);
  link.props.onClick({
    button: 0,
    defaultPrevented: true,
    preventDefault() {
      prevented = true;
    },
  });
  assert.equal(h.entries.length, 0);
  link.props.onClick({
    button: 0,
    preventDefault() {
      prevented = true;
    },
  });
  assert.equal(h.window.location.pathname, '/admin/sms/redeems');
  assert.equal(prevented, true);
});

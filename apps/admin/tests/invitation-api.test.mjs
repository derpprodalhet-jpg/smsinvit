import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const originalFetch = globalThis.fetch;
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const [key, descriptor] of [
    ['document', originalDocument],
    ['window', originalWindow],
  ]) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
});
function client() {
  globalThis.document = { cookie: 'lc_admin_session=1; lc_csrf=csrf-fixture' };
  globalThis.window = new EventTarget();
  const cache = new Map();
  const load = (file) => {
    if (cache.has(file)) return cache.get(file).exports;
    const mod = { exports: {} };
    cache.set(file, mod);
    const code = ts.transpileModule(readFileSync(file, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const require = createRequire(file);
    new Function('require', 'module', 'exports', code)(
      (name) => {
        if (name.startsWith('.')) {
          const target = resolve(dirname(file), name + '.ts');
          if (existsSync(target)) return load(target);
        }
        return require(name);
      },
      mod,
      mod.exports,
    );
    return mod.exports;
  };
  return load(fileURLToPath(new URL('../src/lib/invitation-api.ts', import.meta.url)));
}

test('native response stays intact, including a nested credential data field', async () => {
  const api = client();
  const raw = { ok: true, data: { email: 'fixture@example.test', password: '***' } };
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/v1/admin/invitation/native/api/registered/fixture');
    assert.equal(init.credentials, 'include');
    return Response.json(raw);
  };
  assert.deepEqual(await api.invitationApi('/registered/fixture'), raw);
});

test('native writes share the existing cookie and CSRF transport', async () => {
  const api = client();
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/v1/admin/invitation/native/api/accounts/bulk_reset');
    assert.equal(init.method, 'POST');
    assert.equal(init.headers.get('X-CSRF-Token'), 'csrf-fixture');
    assert.deepEqual(JSON.parse(init.body), { emails: ['fixture@example.test'] });
    return Response.json({ ok: true, reset: 1 });
  };
  assert.equal(
    (await api.invitationAction('/accounts/bulk_reset', { emails: ['fixture@example.test'] }))
      .reset,
    1,
  );
});

test('import validation keeps status, message and per-line details without replay', async () => {
  const api = client();
  const raw = { ok: false, message: '第 2 行格式错误', errors: [{ line: 2, error: '缺少字段' }] };
  let count = 0;
  globalThis.fetch = async () => {
    count++;
    return Response.json(raw, { status: 422 });
  };
  await assert.rejects(
    api.invitationAction('/import', { text: 'fixture', kind: 'outlook' }),
    (error) => {
      assert.equal(error.status, 422);
      assert.equal(error.message, raw.message);
      assert.deepEqual(error.data.errors, raw.errors);
      assert.deepEqual(error.details, raw);
      return true;
    },
  );
  assert.equal(count, 1);
});

test('HTTP 200 operation failures remain failures with diagnostic details', async () => {
  const api = client();
  globalThis.fetch = async () =>
    Response.json({ ok: false, reason: '代理不可用', diagnostics: ['fixture'] });
  await assert.rejects(api.invitationAction('/proxy/test', {}), (error) => {
    assert.equal(error.code, 'INVITATION_REJECTED');
    assert.equal(error.message, '代理不可用');
    assert.deepEqual(error.data.diagnostics, ['fixture']);
    return true;
  });
});

test('a blocked native action does not refresh or retry', async () => {
  const api = client();
  let count = 0;
  globalThis.fetch = async () => {
    count++;
    return Response.json(
      { success: false, error: { code: 'PERMISSION_DENIED', message: '没有操作权限。' } },
      { status: 403 },
    );
  };
  await assert.rejects(api.invitationAction('/cdk/generate', {}), {
    code: 'PERMISSION_DENIED',
    status: 403,
  });
  assert.equal(count, 1);
});

test('aborting a background read preserves AbortError', async () => {
  const api = client();
  const controller = new AbortController();
  globalThis.fetch = async () => {
    controller.abort();
    throw new DOMException('Aborted', 'AbortError');
  };
  await assert.rejects(api.invitationApi('/overview', { signal: controller.signal }), {
    name: 'AbortError',
  });
});

test('relative invitation paths cannot escape into an external website', async () => {
  const api = client();
  globalThis.fetch = () => {
    throw new Error('must not issue request');
  };
  for (const path of ['https://external.test', '//external.test', 'overview']) {
    await assert.rejects(api.invitationApi(path), /相对路径/);
  }
});

import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

/**
 * Exercise the actual browser helpers with a controlled HTTP boundary.
 * @param {string | URL} sourceUrl
 * @param {{ mePath?: string, refreshPath?: string, sessionCookie?: string }} [options]
 */
export function apiClientContracts(sourceUrl, options = {}) {
  const mePath = options.mePath || '/api/v1/auth/me';
  const refreshPath = options.refreshPath || '/api/v1/auth/refresh';
  const sessionCookie = options.sessionCookie || 'lc_session=1';
  const source = readFileSync(sourceUrl, 'utf8');
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  });
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
  function client(cookie = `${sessionCookie}; lc_csrf=old-token`) {
    globalThis.document = { cookie };
    globalThis.window = new EventTarget();
    const module = { exports: {} };
    new Function('require', 'module', 'exports', outputText)(
      createRequire(sourceUrl),
      module,
      module.exports,
    );
    return module.exports.apiFetch;
  }
  const ok = () => Response.json({ success: true, data: { saved: true } });
  const error = (code, status = 403) =>
    Response.json({ success: false, error: { code } }, { status });
  const isMe = (url) => String(url) === mePath || String(url).endsWith(mePath);
  const isRefresh = (url) => String(url) === refreshPath || String(url).endsWith(refreshPath);

  test('writes include credentials and current CSRF token', async () => {
    const api = client();
    globalThis.fetch = async (url, init) => {
      assert.equal(url, '/api/v1/admin/plans');
      assert.equal(init.credentials, 'include');
      assert.equal(init.headers.get('X-CSRF-Token'), 'old-token');
      assert.equal(init.body, '{"name":"test"}');
      return ok();
    };
    assert.deepEqual(
      (await api('/api/v1/admin/plans', { method: 'POST', body: '{"name":"test"}' })).data,
      { saved: true },
    );
  });

  test('CSRF renewal replaces even a caller-provided stale header', async () => {
    const api = client();
    const calls = [];
    globalThis.fetch = async (url, init) => {
      calls.push(url);
      if (calls.length === 1) return error('CSRF_INVALID');
      if (isMe(url)) {
        document.cookie = `${sessionCookie}; lc_csrf=new-token`;
        return ok();
      }
      assert.equal(init.headers.get('X-CSRF-Token'), 'new-token');
      assert.equal(init.body, '{}');
      return ok();
    };
    await api('/api/v1/admin/plans', {
      method: 'POST',
      headers: { 'X-CSRF-Token': 'old-token' },
      body: '{}',
    });
    assert.equal(calls.length, 3);
  });

  test('CSRF failures stop after one renewal and one retry', async () => {
    const api = client();
    let calls = 0;
    globalThis.fetch = async (url) => {
      calls++;
      return isMe(url) ? ok() : error('CSRF_INVALID');
    };
    await assert.rejects(api('/api/v1/admin/plans', { method: 'POST' }), {
      code: 'CSRF_INVALID',
    });
    assert.equal(calls, 3);
  });

  test('verification outage signals fallback without replaying a write', async () => {
    const api = client();
    let calls = 0,
      signals = 0;
    window.addEventListener('smsinvit:captcha-fallback', () => signals++);
    globalThis.fetch = async () => {
      calls++;
      return error('CAPTCHA_UNAVAILABLE', 503);
    };
    await assert.rejects(api('/api/v1/admin/inventory/generate', { method: 'POST' }), {
      code: 'CAPTCHA_UNAVAILABLE',
    });
    assert.equal(calls, 1);
    assert.equal(signals, 1);
  });

  test('network failures never replay a potentially committed write', async () => {
    const api = client();
    let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      throw new TypeError('network unavailable');
    };
    await assert.rejects(api('/api/v1/admin/inventory/generate', { method: 'POST' }), {
      code: 'NETWORK_ERROR',
    });
    assert.equal(calls, 1);
  });

  test('permission failures do not refresh or replay writes', async () => {
    const api = client();
    let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      return error('PERMISSION_DENIED');
    };
    await assert.rejects(api('/api/v1/admin/plans', { method: 'POST' }), {
      code: 'PERMISSION_DENIED',
    });
    assert.equal(calls, 1);
  });

  test('concurrent expired sessions share a single refresh', async () => {
    const api = client();
    let refreshed = false;
    let refreshCount = 0;
    globalThis.fetch = async (url) => {
      if (isRefresh(url)) {
        refreshCount++;
        await new Promise((resolve) => setImmediate(resolve));
        refreshed = true;
        return ok();
      }
      return refreshed ? ok() : error('AUTH_SESSION_EXPIRED', 401);
    };
    await Promise.all([
      api('/api/v1/admin/inventory/generate'),
      api('/api/v1/admin/notifications/outbox'),
    ]);
    assert.equal(refreshCount, 1);
  });
}

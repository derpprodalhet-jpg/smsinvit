import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
const source = new URL('../src/lib/api.ts', import.meta.url);
const compiled = ts.transpileModule(readFileSync(source, 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
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
  globalThis.document = { cookie: 'lc_csrf=old' };
  globalThis.window = new EventTarget();
  const mod = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(createRequire(source), mod, mod.exports);
  return mod.exports.apiFetch;
}
function clientWithBackoff() {
  globalThis.document = { cookie: 'lc_csrf=old' };
  globalThis.window = new EventTarget();
  const mod = { exports: {} };
  new Function('require', 'module', 'exports', compiled)(createRequire(source), mod, mod.exports);
  return mod.exports;
}
const ok = () => Response.json({ success: true, data: { id: 'session' } });
test('redemption sends the session capability and CSRF token unchanged', async () => {
  const api = client();
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/v1/sms/sessions/session/next-sms');
    assert.equal(init.credentials, 'include');
    assert.equal(init.headers.get('X-Session-Secret'), 'private-capability');
    assert.equal(init.headers.get('X-CSRF-Token'), 'old');
    return ok();
  };
  await api('/api/v1/sms/sessions/session/next-sms', {
    method: 'POST',
    headers: { 'X-Session-Secret': 'private-capability' },
  });
});
test('public CSRF renewal uses page-content and never calls removed customer auth', async () => {
  const api = client();
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push(url);
    if (calls.length === 1)
      return Response.json({ success: false, error: { code: 'CSRF_INVALID' } }, { status: 403 });
    if (url === '/api/v1/sms/page-content') {
      document.cookie = 'lc_csrf=new';
      return ok();
    }
    assert.equal(init.headers.get('X-CSRF-Token'), 'new');
    assert.equal(init.headers.get('X-Session-Secret'), 'private-capability');
    return ok();
  };
  await api('/api/v1/sms/sessions/session/next-sms', {
    method: 'POST',
    headers: { 'X-CSRF-Token': 'stale', 'X-Session-Secret': 'private-capability' },
    body: '{}',
  });
  assert.deepEqual(calls, [
    '/api/v1/sms/sessions/session/next-sms',
    '/api/v1/sms/page-content',
    '/api/v1/sms/sessions/session/next-sms',
  ]);
});
test('network, permission, authentication and non-JSON failures do not replay an external write', async () => {
  for (const code of [
    'NETWORK_ERROR',
    'PERMISSION_DENIED',
    'AUTH_SESSION_EXPIRED',
    'SERVICE_UNAVAILABLE',
  ]) {
    const api = client();
    let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      if (code === 'NETWORK_ERROR') throw new Error('offline');
      if (code === 'SERVICE_UNAVAILABLE') return new Response('<html>gateway</html>');
      return Response.json({ success: false, error: { code } }, { status: 403 });
    };
    await assert.rejects(
      api('/api/v1/sms/sessions/session/next-sms', { method: 'POST', body: '{}' }),
      { code },
    );
    assert.equal(calls, 1);
  }
});

test('valid SMS and invitation envelopes retain data and metadata', async () => {
  for (const [url, data] of [
    ['/api/v1/sms/redeem', { id: 'sms', sessionSecret: 'capability' }],
    ['/api/v1/invitation/status', { id: 'x', state: 'PENDING' }],
    ['/api/v1/invitation/redeem', { items: [{ id: 'invite' }], capacity: { concurrency: 3 } }],
    ['/api/v1/invitation/status', null],
  ]) {
    const api = client();
    const envelope = { success: true, data, meta: { fixture: true } };
    globalThis.fetch = async () => Response.json(envelope);
    assert.deepEqual(await api(url), { data, meta: envelope.meta, raw: envelope });
  }
});

test('malformed JSON envelopes never masquerade as a completed mutation or trigger replay', async () => {
  for (const payload of [null, [], {}, { data: {} }, { success: 'true', data: {} },
    { success: true }, { success: false }, { success: false, error: [] },
    { success: false, error: { code: 7 } }, { error: { code: 'CSRF_INVALID' } }]) {
    const api = client();
    let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      return Response.json(payload, { headers: { 'X-Request-ID': 'fixture-invalid' } });
    };
    await assert.rejects(api('/api/v1/invitation/redeem', { method: 'POST', body: '{}' }),
      { code: 'SERVICE_UNAVAILABLE', httpStatus: 200, requestId: 'fixture-invalid' });
    assert.equal(calls, 1);
  }
});

test('business failures retain status and prefer body request ID with header fallback', async () => {
  for (const bodyId of ['fixture-body', undefined]) {
    const api = client();
    globalThis.fetch = async () => Response.json({ success: false,
      error: { code: 'INVITATION_MAIL_UNAVAILABLE', message: '邮箱服务暂不可用。', requestId: bodyId } },
    { status: 503, headers: { 'X-Request-ID': 'fixture-header' } });
    await assert.rejects(api('/api/v1/invitation/redeem', { method: 'POST', body: '{}' }),
      { code: 'INVITATION_MAIL_UNAVAILABLE', message: '邮箱服务暂不可用。', httpStatus: 503,
        requestId: bodyId || 'fixture-header', structuredResponse: true });
  }
});

test('HTML gateway failures retain diagnostics without exposing the body or retrying', async () => {
  for (const status of [200, 401, 403, 502]) {
    const api = client();
    let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      return new Response('<html>private-fixture-token</html>',
        { status, headers: { 'X-Request-ID': 'fixture-gateway' } });
    };
    await assert.rejects(api('/api/v1/invitation/redeem', { method: 'POST', body: '{}' }),
      (error) => {
        assert.equal(error.httpStatus, status);
        assert.equal(error.requestId, 'fixture-gateway');
        assert.equal(error.structuredResponse, false);
        assert.equal(error.code, status === 403 ? 'PERMISSION_DENIED' : 'SERVICE_UNAVAILABLE');
        assert.doesNotMatch(error.message, /private-fixture-token|<html>/);
        return true;
      });
    assert.equal(calls, 1);
  }
});

test('fetch cancellation propagates the original AbortError instead of NETWORK_ERROR', async () => {
  for (const withSignal of [true, false]) {
    const api = client();
    const abort = new DOMException('fixture cancellation', 'AbortError');
    const controller = new AbortController();
    globalThis.fetch = async () => {
      if (withSignal) controller.abort(abort);
      throw abort;
    };
    await assert.rejects(api('/api/v1/invitation/status',
      withSignal ? { signal: controller.signal } : undefined), (error) => error === abort);
  }
});

test('response-body cancellation propagates AbortError without replay', async () => {
  const api = client();
  const abort = new DOMException('fixture body cancellation', 'AbortError');
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return { status: 200, ok: true, headers: new Headers(), json: async () => { throw abort; } };
  };
  await assert.rejects(api('/api/v1/invitation/start', { method: 'POST', body: '{}' }),
    (error) => error === abort);
  assert.equal(calls, 1);
});

test('already-aborted requests never send and cancellation during CSRF renewal prevents replay', async () => {
  const api = client();
  const abort = new DOMException('fixture cancellation', 'AbortError');
  const stopped = new AbortController();
  stopped.abort(abort);
  let calls = 0;
  globalThis.fetch = async () => { calls++; return ok(); };
  await assert.rejects(api('/api/v1/invitation/start', { method: 'POST', signal: stopped.signal }),
    (error) => error === abort);
  assert.equal(calls, 0);
  const controller = new AbortController();
  globalThis.fetch = async (url) => {
    calls++;
    if (url === '/api/v1/sms/page-content') {
      controller.abort(abort);
      return ok();
    }
    return Response.json({ success: false, error: { code: 'CSRF_INVALID' } }, { status: 403 });
  };
  await assert.rejects(api('/api/v1/invitation/start', { method: 'POST', body: '{}', signal: controller.signal }),
    (error) => error === abort);
  assert.equal(calls, 2);
});

test('repeated CSRF rejection stops after one renewal and one replay with unchanged business data', async () => {
  const api = client();
  let writes = 0;
  let reads = 0;
  globalThis.fetch = async (url, init) => {
    if (url === '/api/v1/sms/page-content') {
      reads++;
      document.cookie = 'lc_csrf=new';
      return ok();
    }
    writes++;
    assert.equal(init.body, '{"code":"fixture"}');
    assert.equal(init.headers.get('X-Session-Secret'), 'fixture-capability');
    return Response.json({ success: false, error: { code: 'CSRF_INVALID' } },
      { status: 403, headers: { 'X-Request-ID': 'fixture-repeat' } });
  };
  await assert.rejects(api('/api/v1/invitation/redeem', { method: 'POST',
    headers: { 'X-Session-Secret': 'fixture-capability' }, body: '{"code":"fixture"}' }),
  { code: 'CSRF_INVALID', httpStatus: 403, requestId: 'fixture-repeat' });
  assert.equal(writes, 2);
  assert.equal(reads, 1);
});

test('captcha failures notify the challenge once without replaying the mutation', async () => {
  const api = client();
  let notifications = 0;
  let calls = 0;
  window.addEventListener('smsinvit:captcha-fallback', () => notifications++);
  globalThis.fetch = async () => {
    calls++;
    return Response.json({ success: false, error: { code: 'CAPTCHA_INVALID' } }, { status: 403 });
  };
  await assert.rejects(api('/api/v1/invitation/redeem', { method: 'POST', body: '{}' }),
    { code: 'CAPTCHA_INVALID' });
  assert.equal(calls, 1);
  assert.equal(notifications, 1);
});

test('malformed cookie encoding does not prevent a request or send an invalid CSRF header', async () => {
  const api = client();
  document.cookie = 'lc_csrf=%broken';
  let calls = 0;
  globalThis.fetch = async (_url, init) => {
    calls++;
    assert.equal(init.headers.has('X-CSRF-Token'), false);
    return ok();
  };
  assert.equal((await api('/api/v1/invitation/start', { method: 'POST', body: '{}' })).data.id, 'session');
  assert.equal(calls, 1);
});

test('a contradictory successful envelope cannot smuggle a business rejection or CSRF replay', async () => {
  const api = client();
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return Response.json({ success: true, data: null, error: { code: 'CSRF_INVALID' } }, { status: 403 });
  };
  await assert.rejects(api('/api/v1/invitation/redeem', { method: 'POST', body: '{}' }),
    { code: 'PERMISSION_DENIED', httpStatus: 403, structuredResponse: false });
  assert.equal(calls, 1);
});

test('HTTP failure with a success envelope rejects unvalidated object error codes safely', async () => {
  const api = client();
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return Response.json({ success: true, data: null, error: { code: {} } },
      { status: 503, headers: { 'X-Request-ID': 'fixture-contradiction' } });
  };
  await assert.rejects(api('/api/v1/invitation/redeem', { method: 'POST', body: '{}' }),
    { name: 'ApiClientError', code: 'SERVICE_UNAVAILABLE', httpStatus: 503,
      requestId: 'fixture-contradiction', structuredResponse: false });
  assert.equal(calls, 1);
});

test('rate limits retain Retry-After on structured and gateway failures without replaying writes', async () => {
  for (const json of [true, false]) {
    const api = client(); let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      return json ? Response.json({success:false,error:{code:'RATE_LIMITED'}}, {status:429,headers:{'Retry-After':'17'}})
        : new Response('gateway', {status:429,headers:{'Retry-After':'17'}});
    };
    await assert.rejects(() => api('/api/v1/invitation/status', {method:'POST',body:'{}'}), error => error.httpStatus === 429 && error.retryAfterMs === 17000);
    assert.equal(calls, 1);
  }
});

test('mutation and page-content rate limits preserve retry guidance without stopping in-flight progress reads', async () => {
  for (const [service, path, method] of [
    ['sms', '/api/v1/sms/redeem', 'POST'],
    ['sms', '/api/v1/sms/sessions/session/next-sms', 'POST'],
    ['invitation', '/api/v1/invitation/redeem', 'POST'],
    ['invitation', '/api/v1/invitation/redeem', 'POST'],
    ['invitation', '/api/v1/invitation/start', 'POST'],
    ['invitation', '/api/v1/invitation/page-content', 'GET'],
  ]) {
    const { apiFetch, pollRetryDelay } = clientWithBackoff();
    globalThis.fetch = async () => Response.json({success:false,error:{code:'RATE_LIMIT_EXCEEDED'}},
      {status:429,headers:{'Retry-After':'17'}});
    await assert.rejects(apiFetch(path, {method}), err => err.retryAfterMs === 17000 && err.retryAt > Date.now());
    assert.equal(pollRetryDelay(service), 0, path);
  }
});

test('progress-only GET and POST limits stop their service pollers for Retry-After', async () => {
  for (const [service, path, method] of [
    ['sms', '/api/v1/sms/sessions/session', 'GET'],
    ['invitation', '/api/v1/invitation/status', 'POST'],
    ['invitation', '/api/v1/invitation/status', 'POST'],
    ['invitation', '/api/v1/invitation/redemption-attempts/attempt', 'GET'],
  ]) {
    const { apiFetch, pollRetryDelay } = clientWithBackoff();
    globalThis.fetch = async () => new Response('gateway', {status:429,headers:{'Retry-After':'17'}});
    await assert.rejects(apiFetch(path, {method}), err => err.retryAfterMs === 17000);
    assert.ok(pollRetryDelay(service) > 16000, path);
    assert.equal(pollRetryDelay(service === 'sms' ? 'invitation' : 'sms'), 0);
  }
});

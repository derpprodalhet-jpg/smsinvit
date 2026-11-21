import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const source = new URL('../src/lib/recovery-rules.ts', import.meta.url);
const compiled = ts.transpileModule(readFileSync(source, 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const module = { exports: {} };
new Function('require', 'module', 'exports', compiled)(createRequire(source), module, module.exports);
const rules = module.exports;

// 模拟 lib/api 抛出的 ApiClientError 形状（code + httpStatus）。
const apiError = (httpStatus, code = 'SERVICE_UNAVAILABLE') => ({ name: 'ApiClientError', code, httpStatus });

test('F06: classifyRecoveryError never treats an unknown shape as terminal', () => {
  // catch 子句拿到 unknown：非对象、null、抛字符串都必须当临时故障，不能当成终局。
  for (const value of [null, undefined, 'boom', 42, new Error('x'), {}]) {
    assert.equal(rules.classifyRecoveryError(value), 'transient', `value ${String(value)}`);
  }
  assert.equal(rules.classifyRecoveryError(apiError(404)), 'not-found');
  assert.equal(rules.classifyRecoveryError(apiError(429)), 'transient');
});

test('F06: transient infrastructure failures stay retryable', () => {
  // 断网（没有 httpStatus）、502、503、429、超时、5xx 都必须是"还能找回"。
  for (const error of [
    { name: 'ApiClientError', code: 'NETWORK_ERROR' },
    apiError(502, 'SERVICE_UNAVAILABLE'),
    apiError(503),
    apiError(429, 'RATE_LIMIT_EXCEEDED'),
    apiError(408),
    apiError(500),
  ]) {
    assert.equal(rules.classifyRecoveryError(error), 'transient', JSON.stringify(error));
  }
});

test('F06: only an explicit invalid/expired capability is classified as terminal-invalid', () => {
  for (const [status, expected] of [
    [404, 'not-found'],
    [410, 'not-found'],
    [401, 'invalid'],
    [403, 'invalid'],
    [400, 'invalid'],
  ]) {
    assert.equal(rules.classifyRecoveryError(apiError(status)), expected, `status ${status}`);
  }
});

test('F06: business rejections are terminal, not retryable', () => {
  // 409「卡密已使用」、422 参数不合法同样是终局，不能劝用户重试。
  for (const status of [409, 422]) {
    assert.equal(rules.classifyRecoveryError(apiError(status)), 'permanent', `status ${status}`);
  }
});

test('F08: only transient deep-link failures offer a retry of the original redemption', () => {
  assert.equal(rules.canRetryDeepLink({ code: 'NETWORK_ERROR' }), true);
  assert.equal(rules.canRetryDeepLink(apiError(502)), true);
  assert.equal(rules.canRetryDeepLink(apiError(429)), true);
  // 卡密无效/不存在属于终局：不该劝用户重试。
  assert.equal(rules.canRetryDeepLink(apiError(404, 'SMS_CDK_NOT_FOUND')), false);
  assert.equal(rules.canRetryDeepLink(apiError(409, 'SMS_CDK_FAILED')), false);
});

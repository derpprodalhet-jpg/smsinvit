/**
 * 用户前台的错误文案规则。
 *
 * 产品要求：**只有卡密本身的问题**给明确提示，其余一律同一句通用文案。
 * 这同时修掉两件事：
 *   1. 后端把「邮箱服务暂时不可用」「请求参数格式或长度不合法」这类基础设施/校验状态
 *      原样送到了买家面前（共享的 toUserFacingError 只要消息含中文就直接回显）；
 *   2. 一次失败里同时出现"第 N 行：卡密无效或不存在"和"邮箱服务暂时不可用"两条，
 *      指向同一件事却互相矛盾。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const source = new URL('../src/lib/user-error.ts', import.meta.url);
const compiled = ts.transpileModule(readFileSync(source, 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const module = { exports: {} };
new Function('require', 'module', 'exports', compiled)(createRequire(source), module, module.exports);
const { userError, isCardError, GENERIC_USER_ERROR } = module.exports;

// 模拟 lib/api 的 ApiClientError：code + 服务端中文消息。
const apiError = (code, message) => ({ name: 'ApiClientError', code, message, httpStatus: 400 });

test('只有卡密类错误回显具体原因', () => {
  for (const [code, expected] of [
    ['SMS_CDK_NOT_FOUND', '卡密错误，请核对后重新输入'],
    ['SMS_CDK_INVALID', '卡密错误，请核对后重新输入'],
    ['INVITATION_CDK_NOT_FOUND', '卡密错误，请核对后重新输入'],
    ['SMS_CDK_USED', '该 CDK 已使用成功'],
    ['SMS_CDK_DISABLED', '该 CDK 已被停用或作废'],
    ['INVITATION_CDK_DISABLED', '该卡密已停用或作废'],
  ]) {
    assert.equal(userError(apiError(code, undefined)), expected, code);
    assert.equal(isCardError(code), true, code);
  }
});

test('卡密规则只认 SMS / 邀请前缀，其它前缀一律走通用文案', () => {
  // CDK 下线任何一条业务后，它的 *_CDK_* 码都不该继续命中卡密规则并回显原因。
  for (const code of ['LEGACY_CDK_NOT_FOUND', 'CDK_NOT_FOUND', 'SMS_CDK_UNAVAILABLE']) {
    assert.equal(isCardError(code), false, code);
    assert.equal(userError(apiError(code, '卡密错误，请核对后重新输入')), GENERIC_USER_ERROR, code);
  }
});

test('不存在与无效给同一句，不泄露"这个卡密存在"', () => {
  // 枚举风险：两条文案不同就等于确认卡密存在。
  assert.equal(userError(apiError('SMS_CDK_NOT_FOUND')), userError(apiError('SMS_CDK_INVALID')));
});

test('服务端的中文基础设施消息不再直达用户', () => {
  // 这两条正是截图里出现过的。
  const cases = [
    ['INVITATION_MAIL_UNAVAILABLE', '邮箱服务暂时不可用，请稍后重试。'],
    ['VALIDATION_ERROR', '请求参数格式或长度不合法。'],
    ['SMS_UNAVAILABLE', '接码服务暂时异常，请稍后再试'],
    ['INVITATION_MAIL_INSUFFICIENT', '可用邮箱不足，请稍后重试。'],
    ['INVITATION_REDEMPTION_REVIEW', '领取结果待核查，请稍后再试。'],
    ['SMS_PROVIDER_ERROR', '取号结果暂时无法确认，请联系客服。'],
    ['INVITATION_EXECUTOR_UNAVAILABLE', '执行器暂时不可用。'],
    ['SERVICE_UNAVAILABLE', '服务暂时不可用，请稍后重试。'],
  ];
  for (const [code, message] of cases) {
    const text = userError(apiError(code, message));
    assert.equal(text, GENERIC_USER_ERROR, `${code} 必须走通用文案`);
    assert.doesNotMatch(text, /邮箱|上游|执行器|服务暂时|参数/, `${code} 不得泄露内部状态`);
  }
});

test('未知错误、非对象、网络错误都收敛到通用文案', () => {
  for (const value of [null, undefined, 'boom', 42, new Error('x'), {}, apiError('SOMETHING_NEW')]) {
    assert.equal(userError(value), GENERIC_USER_ERROR, String(value));
  }
  assert.equal(userError(apiError('NETWORK_ERROR')), GENERIC_USER_ERROR);
});

test('卡密已停用/已使用仍与"卡密错误"区分开（对持有者是有用信息）', () => {
  assert.notEqual(userError(apiError('SMS_CDK_DISABLED')), GENERIC_USER_ERROR);
  assert.notEqual(userError(apiError('SMS_CDK_USED')), GENERIC_USER_ERROR);
  assert.notEqual(userError(apiError('SMS_CDK_NOT_FOUND')), GENERIC_USER_ERROR);
});

test('会话状态类错误仍给可操作提示（卡密已通过校验）', () => {
  assert.notEqual(userError(apiError('SMS_SESSION_NOT_FOUND')), GENERIC_USER_ERROR);
  assert.notEqual(userError(apiError('SMS_SESSION_STATE')), GENERIC_USER_ERROR);
  assert.notEqual(userError(apiError('SMS_CDK_FAILED')), GENERIC_USER_ERROR);
});

test('通用文案自身不含内部信息', () => {
  assert.doesNotMatch(GENERIC_USER_ERROR, /邮箱|上游|执行器|服务|参数|队列|代理/);
});

test('不再有任何凭据长度专用文案：未列入卡密/会话规则的码只走通用文案', () => {
  for (const code of ['CREDENTIAL_TOO_LONG', 'CREDENTIAL_INPUT_TOO_LONG']) {
    const result = userError(apiError(code, '机密凭据 SECRET_TOKEN'));
    assert.equal(result, GENERIC_USER_ERROR, code);
    assert.doesNotMatch(result, /机密|SECRET_TOKEN/);
  }
});

test('429 shows only safe retry guidance and its remaining cooldown instead of blaming the input', () => {
  const error = { code: 'RATE_LIMIT_EXCEEDED', httpStatus: 429,
    message: 'PRIVATE_INFRASTRUCTURE', retryAt: Date.now() + 17000 };
  assert.match(userError(error), /等待 17 秒后重试/);
  assert.doesNotMatch(userError(error), /输入错误|PRIVATE_INFRASTRUCTURE/);
  assert.equal(userError({ ...error, retryAt: Date.now() - 1 }), '操作较频繁，请稍后重试。');
  assert.equal(userError({ httpStatus: 429, code: 'SERVICE_UNAVAILABLE' }), '操作较频繁，请稍后重试。');
});

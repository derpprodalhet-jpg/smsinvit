import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

function load(relative) {
  const cache = new Map();
  function moduleAt(path) {
    if (cache.has(path)) return cache.get(path).exports;
    const module = { exports: {} },
      require = createRequire(path);
    cache.set(path, module);
    const code = ts.transpileModule(readFileSync(path, 'utf8'), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }).outputText;
    new Function('require', 'module', 'exports', code)(
      (name) => {
        if (name.startsWith('.')) {
          for (const extension of ['.ts', '.tsx']) {
            const target = resolve(dirname(path), name + extension);
            if (existsSync(target)) return moduleAt(target);
          }
        }
        return require(name);
      },
      module,
      module.exports,
    );
    return module.exports;
  }
  return moduleAt(fileURLToPath(new URL('../src/' + relative, import.meta.url)));
}

const { SmsBalanceDetails } = load('pages/invitation/settings/diagnostics.tsx');
const { InvitationStatus } = load('components/invitation-status.tsx');
const { CdkSyncDetails, CdkSyncTag } = load('pages/invitation/runs/cdk-sync.tsx');
const { InvitationPhoneTooltip, InvitationPhoneStatus } = load('components/invitation-phone-status.tsx');
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));

test('balance diagnostics display every platform and preserve zero balance, failures and missing configuration', () => {
  const html = render(SmsBalanceDetails, {
    result: {
      ok: true,
      all_ok: false,
      platforms: {
        smsbower: {
          label: 'SmsBower',
          configured: true,
          status: 'success',
          ok: true,
          balance: 0,
          elapsed_ms: 13,
          error: '',
        },
        herosms: {
          label: 'HeroSMS',
          configured: true,
          status: 'failed',
          ok: false,
          balance: null,
          elapsed_ms: 15,
          error: '连接超时',
        },
        smspool: {
          label: 'SMSPool',
          configured: false,
          status: 'unconfigured',
          ok: false,
          balance: null,
          elapsed_ms: 0,
          error: '',
        },
      },
    },
  });
  for (const text of [
    'SmsBower',
    'HeroSMS',
    'SMSPool',
    '查询成功',
    '查询失败',
    '未配置密钥',
    '连接超时',
    '部分平台失败或未配置',
    '不能确认 OpenAI 发码与收码',
  ])
    assert.ok(html.includes(text), text);
  assert.match(html, />0<\/td>/, 'zero is a real balance, not a missing value');
  assert.match(html, />13<\/td>/);
  assert.match(html, />15<\/td>/);
  assert.doesNotMatch(html, /三个平台余额查询成功/);
});

test('three successful balances show complete query success without claiming code delivery', () => {
  const platforms = Object.fromEntries(
    ['smsbower', 'herosms', 'smspool'].map((platform) => [
      platform,
      {
        label: platform,
        configured: true,
        status: 'success',
        ok: true,
        balance: 4.75,
        elapsed_ms: 10,
        error: '',
      },
    ]),
  );
  const html = render(SmsBalanceDetails, { result: { ok: true, all_ok: true, platforms } });
  assert.ok(html.includes('三个平台余额查询成功'));
  assert.ok(html.includes('余额查询不能确认 OpenAI 发码与收码是否成功'));
  assert.equal((html.match(/4\.75/g) || []).length, 3);
});

test('mail registration labels are local and do not change completed run or CDK labels', () => {
  // 邮箱管理的账号类型只有三个身份词（邮箱 / GPT / Codex）；长说明挪到悬浮说明里，
  // 不再出现在标签文本中。
  const mailDone = render(InvitationStatus, { status: 'done', context: 'mail-account' });
  assert.ok(mailDone.includes('Codex'));
  assert.ok(!mailDone.includes('Codex（已授权）'));
  assert.ok(render(InvitationStatus, { status: 'registered', context: 'mail-account' }).includes('GPT'));
  assert.ok(render(InvitationStatus, { status: 'available', context: 'mail-account' }).includes('邮箱'));
  assert.ok(!render(InvitationStatus, { status: 'available', context: 'mail-account' }).includes('新邮箱'));
  const global = render(InvitationStatus, { status: 'done' });
  assert.ok(global.includes('已完成'));
  assert.ok(!global.includes('Codex（已授权）'));
});

test('card synchronization failures remain visible alongside a completed authorization', () => {
  const html = renderToStaticMarkup(
    React.createElement(
      React.Fragment,
      null,
      React.createElement(InvitationStatus, { status: 'done' }),
      React.createElement(CdkSyncTag, { status: 'failed' }),
      React.createElement(CdkSyncDetails, { status: 'failed', error: '同步连接暂时失败' }),
    ),
  );
  for (const text of ['已完成', '同步失败', '卡密状态同步失败', '同步连接暂时失败'])
    assert.ok(html.includes(text), text);
  assert.ok(render(CdkSyncDetails, { status: 'pending' }).includes('卡密状态尚待同步'));
  assert.ok(render(CdkSyncTag, { status: 'unbound' }).includes('未绑定卡密'));
  assert.equal(render(CdkSyncDetails, {}), '');
  assert.equal(render(CdkSyncDetails, { status: 'done' }), '');
});

test('successful registration phone tooltip and details retain the actual number and code', () => {
  const record = { phone_bound_ok: 1, phone_number: '+15550001001', phone_code: '012345' };
  for (const component of [InvitationPhoneTooltip, InvitationPhoneStatus]) {
    const html = render(component, { record });
    for (const text of ['已接码', '手机号：', '+15550001001', '验证码：', '012345'])
      assert.ok(html.includes(text), `${component.name}: ${text}`);
    assert.doesNotMatch(html, /未接码|未记录/);
  }
});

test('successful legacy records distinguish missing phone evidence from an unbound phone', () => {
  const html = render(InvitationPhoneStatus, { record: { phone_bound_ok: 1 } });
  assert.match(html, /已接码/);
  assert.equal((html.match(/未记录/g) || []).length, 2);
  assert.doesNotMatch(html, /未接码/);
  for (const state of [0, -1, undefined]) {
    const pending = render(InvitationPhoneTooltip, { record: { phone_bound_ok: state } });
    assert.doesNotMatch(pending, /手机号：|验证码：/);
  }
});

test('masked phone evidence identifies missing credential permission without exposing placeholders', () => {
  const record = { phone_bound_ok: 1, phone_number: '***', phone_code: '***' };
  for (const component of [InvitationPhoneTooltip, InvitationPhoneStatus]) {
    const html = render(component, { record });
    assert.equal((html.match(/无凭据权限/g) || []).length, 2);
    assert.doesNotMatch(html, /\*\*\*|复制|未记录/);
  }
});

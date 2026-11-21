import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

function loadHelpers(path) {
  const module = { exports: {} };
  const code = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function('module', 'exports', code)(module, module.exports);
  return module.exports;
}
const { changedConfigPayload, configDraft } = loadHelpers(
  '../src/pages/invitation/settings/config-values.ts',
);
const { normalizeInvitationProxy, previewInvitationProxies, mergeProxyTestResults } = loadHelpers(
  '../src/pages/invitation/proxies/proxy-values.ts',
);

test('incomplete proxy checks preserve prior evidence and never label transport failure as dead proxy', () => {
  const previous = { proxyA: { ok: true, ip: '203.0.113.1' }, proxyB: { ok: false, error: 'old failure' } };
  const next = mergeProxyTestResults(previous, ['proxyA', 'proxyC'], undefined, '网络暂时不可用');
  assert.equal(next.proxyA.ok, true);
  assert.equal(next.proxyA.request_error, '网络暂时不可用');
  assert.equal(next.proxyC.ok, undefined);
  assert.deepEqual(next.proxyB, previous.proxyB);
  assert.equal(previous.proxyA.request_error, undefined);
});

test('proxy result merge clears stale success errors and ignores unrelated or missing results', () => {
  const previous = { proxyA: { ok: false, error: 'old', request_error: 'network' } };
  const next = mergeProxyTestResults(previous, ['proxyA', 'proxyB'], {
    proxyA: { ok: true, error: 'stale', alt_scheme: 'socks5h://example.test:1080' },
    other: { ok: true },
  });
  assert.equal(next.proxyA.ok, true);
  assert.equal(next.proxyA.error, '');
  assert.equal(next.proxyA.request_error, undefined);
  assert.equal(next.proxyA.alt_scheme, 'socks5h://example.test:1080');
  assert.ok(next.proxyB.request_error);
  assert.equal(next.other, undefined);
});

test('proxy protocol preview rejects unsupported SOCKS4', () => {
  assert.equal(normalizeInvitationProxy('socks4://example.test:1080'), '');
});

test('dynamic provider secrets and masked URLs stay absent from initial drafts and unchanged saves', () => {
  const fields = [
    { key: 'mail_source', label: '来源' },
    { key: 'new_provider_token', label: '新来源密钥', type: 'password' },
    { key: 'new_provider_endpoint', label: '新来源地址' },
    { key: 'new_provider_domain', label: '域名' },
  ];
  for (const secret of ['***', '[REDACTED]', '<masked>', '•••', 'old-plaintext-token']) {
    const saved = {
      mail_source: 'new-provider',
      new_provider_token: secret,
      new_provider_endpoint: '***',
      new_provider_domain: 'example.test',
    };
    const draft = configDraft(saved, fields);
    assert.equal(draft.new_provider_token, '');
    assert.equal(draft.new_provider_endpoint, '');
    assert.deepEqual(changedConfigPayload(saved, draft, fields), {});
    assert.deepEqual(
      changedConfigPayload(saved, { ...draft, new_provider_domain: 'new.example.test' }, fields),
      { new_provider_domain: 'new.example.test' },
    );
  }
});

test('saving ordinary changes cannot clear unrelated credentials or resend guarded CLI settings', () => {
  const fields = [
    { key: 'sms_api_key_smsbower', label: '密钥', type: 'password' },
    { key: 'sms_allowed_countries', label: '国家' },
    { key: 'sms_price_margin', label: '容差' },
    { key: 'sayhi_prompt', label: '提示词' },
  ];
  const saved = {
    sms_api_key_smsbower: '***',
    sms_allowed_countries: '52',
    sms_price_margin: '0.2',
    sayhi_prompt: '你好',
  };
  const draft = { ...configDraft(saved, fields), sms_allowed_countries: '', sms_price_margin: '0' };
  assert.deepEqual(changedConfigPayload(saved, draft, fields), {
    sms_allowed_countries: '',
    sms_price_margin: '0',
  });
  assert.deepEqual(
    changedConfigPayload(
      saved,
      { ...draft, sms_api_key_smsbower: ' new-valid-key-replacement ' },
      fields,
    ),
    { sms_api_key_smsbower: 'new-valid-key-replacement', sms_allowed_countries: '', sms_price_margin: '0' },
  );
});

test('an accidental mask cannot be submitted as a replacement credential', () => {
  const fields = [{ key: 'cpa_mgmt_key', label: 'CPA 密钥', type: 'password' }];
  assert.deepEqual(
    changedConfigPayload({ cpa_mgmt_key: '***' }, { cpa_mgmt_key: '********' }, fields),
    {},
  );
  assert.deepEqual(changedConfigPayload({ cpa_mgmt_key: '' }, { cpa_mgmt_key: '' }, fields), {});
});

test('proxy preview canonicalizes legacy credentials and SOCKS remote DNS, retaining precise bad line numbers', () => {
  const text =
    '# 注释\n\n host.test:1080:user:pass \nsocks5://user:pass@host.test:1080\nsocks5h://user:pass@host.test:1080\nwrong\nftp://host.test:9\nhost.test:65536';
  const preview = previewInvitationProxies(text);
  assert.deepEqual(preview.urls, [
    'http://user:pass@host.test:1080',
    'socks5h://user:pass@host.test:1080',
  ]);
  assert.deepEqual(
    preview.invalid.map((row) => row.line),
    [6, 7, 8],
  );
  assert.deepEqual(
    preview.duplicates.map((row) => row.line),
    [5],
  );
  assert.equal(normalizeInvitationProxy('127.0.0.1:8080'), 'http://127.0.0.1:8080');
  assert.equal(normalizeInvitationProxy('https://host.test:443'), 'https://host.test:443');
  assert.equal(normalizeInvitationProxy('http://[::1]:7890'), 'http://[::1]:7890');
  assert.equal(normalizeInvitationProxy('host.test:0'), '');
});

test('a forced scheme rewrites bare legacy lines without typing the scheme by hand', () => {
  // 粘贴 host:port:user:pass 时选 SOCKS5，就该按 SOCKS5 生成地址。
  assert.equal(normalizeInvitationProxy('host.example:1080:user:pass', 'socks5h'),
               'socks5h://user:pass@host.example:1080');
  assert.equal(normalizeInvitationProxy('user:pass@host.example:1080', 'socks5h'),
               'socks5h://user:pass@host.example:1080');
  assert.equal(normalizeInvitationProxy('user:pass@host.example:8080', 'http'),
               'http://user:pass@host.example:8080');
});

test('a forced scheme overrides an inline scheme so the operator stays in control', () => {
  assert.equal(normalizeInvitationProxy('http://host.example:8080', 'socks5h'),
               'socks5h://host.example:8080');
  assert.equal(normalizeInvitationProxy('socks5://host.example:1080', 'auto'),
               'socks5h://host.example:1080');
  // socks5 与 socks5h 同义（远端解析）。
  assert.equal(normalizeInvitationProxy('socks5h://host.example:1080', 'auto'),
               'socks5h://host.example:1080');
});

test('auto keeps the historical behaviour of defaulting to HTTP', () => {
  const preview = previewInvitationProxies('host.example:8080\nsocks5://h.example:1080');
  assert.deepEqual(preview.urls, ['http://host.example:8080', 'socks5h://h.example:1080']);
});

test('a forced scheme applies to every line of the preview', () => {
  const preview = previewInvitationProxies('host.example:9080:user:pass\nbare.example:9081', 'socks5h');
  assert.deepEqual(preview.urls,
                   ['socks5h://user:pass@host.example:9080', 'socks5h://bare.example:9081']);
  assert.equal(preview.invalid.length, 0);
});

test('the pool page switches protocol by id through the dedicated endpoint', () => {
  // 回归：旧实现靠"重写整池文本 + /proxy/pool/replace"，url 是唯一键，改协议等于
  // 删旧行插新行（统计归零），而且地址被脱敏时整池覆盖会被拒绝、根本切不了。
  const page = readFileSync(new URL('../src/pages/invitation/proxies/page.tsx', import.meta.url), 'utf8');
  const switchBody = page.slice(page.indexOf('function switchScheme'), page.indexOf('async function copyAll'));
  assert.match(switchBody, /'\/proxy\/pool\/scheme'/);
  assert.match(switchBody, /id: row\.id/);
  assert.doesNotMatch(switchBody, /proxy\/pool\/replace/);
  assert.doesNotMatch(switchBody, /masked/);
  // 批量切换逐行调用，并能报出失败的行。
  const bulkBody = page.slice(page.indexOf('async function switchSelectedSchemes'),
                              page.indexOf('async function copyAll'));
  assert.match(bulkBody, /'\/proxy\/pool\/scheme'/);
  assert.match(bulkBody, /failures\.push/);
  assert.match(bulkBody, /selectedIds/);
});

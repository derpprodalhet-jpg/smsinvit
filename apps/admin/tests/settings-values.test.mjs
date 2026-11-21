import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const helperSource = new URL('../src/lib/settings-values.ts', import.meta.url);
function compile(source) {
  return ts.transpileModule(readFileSync(source, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
}
const helpers = { exports: {} };
new Function('module', 'exports', compile(helperSource))(helpers, helpers.exports);
const { initialSettingDraft, isSecretSetting, preservesSecret, TURNSTILE_SECRET_KEY, SETTING_SECRET_MASK } = helpers.exports;
const row = (value, isSecret = true) => ({
  key: TURNSTILE_SECRET_KEY,
  isSecret,
  valueType: 'TEXT',
  valueJson: { value },
});

// 渲染真实的 settings-workspace 列：注入的 useState 让 load() 之后拿到给定的列表行。
function renderSettings(section, loadedSettings) {
  const source = new URL('../src/components/settings-workspace.tsx', import.meta.url);
  const require = createRequire(source);
  const React = require('react');
  const antd = require('antd');
  const { renderToStaticMarkup } = require('react-dom/server');
  let state = 0;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compile(source))((name) => {
    if (name === 'react') return {
      ...React,
      useEffect: () => {},
      // 第一次 useState 给行，第二次给密钥草稿（与 load() 完成后的状态一致）。
      useState: (initial) => [state++ === 0 ? loadedSettings
        : state === 2 ? Object.fromEntries(loadedSettings.map((item) => [item.key, initialSettingDraft(item)]))
        : initial, () => {}],
    };
    if (name === 'antd') return {
      ...antd,
      Table: ({ columns, dataSource }) => React.createElement(React.Fragment, null,
        (dataSource.length ? dataSource : [{}]).map((entry, i) => React.createElement('div', { key: i },
          columns.map((column, j) => React.createElement('span', { key: j }, column.render(undefined, entry)))))),
    };
    if (name === './admin-page') return { AdminPage: ({ children }) => React.createElement('div', null, children) };
    if (name === '../lib/admin-api') return { adminApi: () => { throw new Error('No HTTP calls allowed'); } };
    if (name === '../lib/format-datetime') return { setBusinessTimeZone: () => {} };
    if (name === '../lib/settings-values') return helpers.exports;
    return require(name);
  }, module, module.exports);
  return renderToStaticMarkup(React.createElement(module.exports.SettingsWorkspace, { section }));
}

test('old plaintext responses are never copied into the secret input draft', () => {
  const legacy = row('mock-legacy-secret', false);
  assert.equal(isSecretSetting(legacy), true);
  assert.equal(initialSettingDraft(legacy), SETTING_SECRET_MASK);
});
test('an unchanged mask preserves the existing value and never becomes an empty draft', () => {
  const configured = row(SETTING_SECRET_MASK);
  assert.equal(initialSettingDraft(configured), SETTING_SECRET_MASK);
  assert.equal(preservesSecret(configured, initialSettingDraft(configured)), true);
});
test('a replacement and an explicitly cleared Turnstile secret are intentional saves', () => {
  const configured = row(SETTING_SECRET_MASK);
  assert.equal(preservesSecret(configured, 'mock-replacement-secret'), false);
  assert.equal(preservesSecret(configured, ''), false);
  assert.equal(initialSettingDraft(row('')), '');
});
test('notification secret empty drafts retain their existing keep-value behavior', () => {
  // SMTP 通道已删除（通知配置只含 Telegram），这里改用剩下的通知密钥字段。
  const notification = { key: 'notify.telegram_bot_token', isSecret: true, valueJson: { value: SETTING_SECRET_MASK } };
  assert.equal(initialSettingDraft(notification), '');
  assert.equal(preservesSecret(notification, ''), true);
  assert.equal(preservesSecret(notification, 'mock-new-password'), false);
});
test('ordinary runtime values retain their text and boolean drafts', () => {
  assert.equal(initialSettingDraft({ key: 'runtime.turnstile_site_key', valueType: 'TEXT', valueJson: { value: 'public-site-key' } }), 'public-site-key');
  assert.equal(initialSettingDraft({ key: 'runtime.secure_cookies', valueType: 'BOOLEAN', valueJson: { value: 'true' } }), true);
  assert.equal(preservesSecret({ key: 'runtime.turnstile_site_key' }, ''), false);
});
test('the actual settings column renders Turnstile as a password input without plaintext', () => {
  const legacy = row('mock-legacy-secret', false);
  const html = renderSettings('runtime', [legacy]);
  assert.match(html, /type="password"/);
  assert.ok(!html.includes('mock-legacy-secret'));
  assert.ok(html.includes('value="********"'));
});
test('通知配置页只渲染 Telegram 字段，SMTP/邮件字段与测试邮件入口全部移除', () => {
  // SMTP 通道已删除：通知配置只含 Telegram（其他任务要求删掉邮件通道）。
  // 模拟 load() 完成后的通知行：label/valueType 由页面的 Telegram 白名单给出。
  const html = renderSettings('notify', [
    { key: 'notify.telegram_bot_token', category: 'notify', label: 'Telegram Bot Token', isSecret: true, valueType: 'TEXT', valueJson: { value: SETTING_SECRET_MASK } },
    { key: 'notify.telegram_ops_chat_id', category: 'notify', label: '运维告警群 ID', valueType: 'TEXT', valueJson: { value: '-100123' } },
  ]);
  assert.ok(html.includes('Telegram Bot Token'));
  assert.ok(html.includes('运维告警群 ID'));
  assert.ok(html.includes('发送 Telegram 测试通知'));
  for (const removed of ['SMTP', 'smtp', '启用邮件', '发件邮箱', '测试收件邮箱', '发送测试邮件']) {
    assert.ok(!html.includes(removed), `通知配置页不应再出现「${removed}」`);
  }
});

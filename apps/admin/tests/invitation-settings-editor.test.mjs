import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const source = new URL('../src/pages/invitation/settings/config-editor.tsx', import.meta.url);
const require = createRequire(source);
const React = require('react');
const transpile = (url) =>
  ts.transpileModule(readFileSync(url, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
const valuesModule = { exports: {} };
new Function(
  'module',
  'exports',
  transpile(new URL('../src/pages/invitation/settings/config-values.ts', import.meta.url)),
)(valuesModule, valuesModule.exports);

function editorHarness(section, config, providers = [], actionResults = {}) {
  const state = [],
    refs = [],
    effectDependencies = [],
    effects = [],
    actions = [],
    reads = [],
    notices = [];
  let stateIndex = 0,
    refIndex = 0,
    effectIndex = 0;
  const react = {
    ...React,
    useState(initial) {
      const index = stateIndex++;
      if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [
        state[index],
        (next) => {
          state[index] = typeof next === 'function' ? next(state[index]) : next;
        },
      ];
    },
    useRef(initial) {
      const index = refIndex++;
      return (refs[index] ||= { current: initial });
    },
    useCallback(callback) {
      return callback;
    },
    useMemo(callback) {
      return callback();
    },
    useEffect(callback, dependencies) {
      const index = effectIndex++;
      const previous = effectDependencies[index];
      // useCallback retains identity in real React; this harness compares dependency shape.
      if (
        !previous ||
        dependencies.some(
          (value, position) => typeof value !== 'function' && value !== previous[position],
        )
      ) {
        effectDependencies[index] = dependencies;
        effects.push(callback);
      }
    },
  };
  const antd = Object.fromEntries(
    [
      'Alert',
      'Button',
      'Card',
      'Col',
      'Form',
      'Input',
      'InputNumber',
      'Modal',
      'Row',
      'Select',
      'Slider',
      'Space',
      'Spin',
      'Switch',
      'Table',
      'Tag',
    ].map((name) => [name, name]),
  );
  antd.Form = Object.assign(() => null, { Item: 'FormItem' });
  antd.Input = Object.assign(() => null, { Password: 'PasswordInput', TextArea: 'TextArea' });
  antd.Typography = { Paragraph: 'Paragraph', Text: 'Text' };
  antd.message = Object.fromEntries(
    ['error', 'warning', 'info', 'success'].map((level) => [
      level,
      (text) => notices.push({ level, text }),
    ]),
  );
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transpile(source))(
    (name) => {
      if (name === 'react') return react;
      if (name === 'antd') return antd;
      if (name.endsWith('invitation-api'))
        return {
          invitationApi: async (path) => {
            reads.push(path);
            if (path === '/mail/providers') return { providers, current: config.mail_source };
            if (path.startsWith('/settings/sms/all_countries'))
              return {
                countries: [{ id: '52', name_cn: '泰国', openai_sms_safe: true }],
                source: 'live',
              };
            return { config };
          },
          invitationAction: async (path, body) => {
            actions.push({ path, body });
            if (Object.hasOwn(actionResults, path)) return actionResults[path];
            Object.assign(config, body);
            return { config };
          },
          invitationErrorMessage: (error) => error.message,
          invitationTime: String,
        };
      if (name.endsWith('use-invitation-query'))
        return {
          useInvitationQuery: () => ({ data: undefined, error: '', reload: async () => {} }),
        };
      if (name.endsWith('navigation')) return { Link: 'Link' };
      if (name === './config-values') return valuesModule.exports;
      if (name === './diagnostics')
        return {
          RuntimeDetails: 'RuntimeDetails',
          SmsSelftestDetails: 'SmsSelftestDetails',
          SmsBalanceDetails: 'SmsBalanceDetails',
        };
      // 配置编辑器现在统一走共用中文映射；补上同名分支，避免深层相对导入落到 Node 解析器。
      if (name.endsWith('/zh-label'))
        return {
          resolveLabel: (map, key) => (key ? map?.[key] || `未知（${key}）` : '—'),
          resolveLabelInsensitive: (map, key) => (key ? map?.[key] || `未知（${key}）` : '—'),
          unknownLabel: (value) => (value ? `未知（${value}）` : '—'),
          yesNoLabel: () => '—',
        };
      if (name.endsWith('/zh-text')) return { localizedText: (value, empty) => value || empty || '—' };
      return require(name);
    },
    module,
    module.exports,
  );
  function render() {
    stateIndex = 0;
    refIndex = 0;
    effectIndex = 0;
    const tree = module.exports.ConfigEditor({ section });
    for (const effect of effects.splice(0)) effect();
    return tree;
  }
  function allElements(tree) {
    if (!tree || typeof tree !== 'object') return [];
    if (Array.isArray(tree)) return tree.flatMap(allElements);
    return [tree, ...allElements(tree.props?.children)];
  }
  const find = (tree, condition) => allElements(tree).find(condition);
  const settled = async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
    return render();
  };
  return { render, settled, find, actions, reads, notices };
}

test('account pool editor renders its own four numeric fields and saves only the changed one', async () => {
  const config = {
    pool_min_available: '10',
    pool_register_concurrency: '2',
    pool_check_seconds: '60',
    pool_otp_timeout: '180',
    cdk_max_concurrency: '3',
    invite_otp_timeout: '120',
  };
  const harness = editorHarness('pool', config);
  harness.render();
  let tree = await harness.settled();
  assert.deepEqual(harness.actions, []);
  // 账号池数值项与邀请设置同一端点往返，但只提交本区块字段。
  assert.equal(harness.reads.at(-1), '/settings/invite');
  assert.ok(harness.find(tree, (node) => node.type === 'Card' && node.props.title === '账号池自动补号'));
  for (const label of [
    '可兑换账号下限',
    '补号并发',
    '巡检间隔（秒）',
    '补号邮箱验证码等待（秒）',
  ])
    assert.ok(harness.find(tree, (node) => node.props?.label === label), label);
  for (const label of ['全局并发上限', '邮箱验证码等待（秒）'])
    assert.equal(
      harness.find(tree, (node) => node.props?.label === label),
      undefined,
      `${label} belongs to the invitation form only`,
    );
  const fields = [
    ['可兑换账号下限', 20],
    ['补号并发', 4],
    ['巡检间隔（秒）', 120],
    ['补号邮箱验证码等待（秒）', 240],
  ];
  for (const [label, value] of fields)
    harness.find(tree, (node) => node.props?.label === label).props.children.props.onChange(value);
  tree = harness.render();
  harness
    .find(tree, (node) => node.type === 'Button' && node.props.children === '保存配置')
    .props.onClick();
  await harness.settled();
  assert.deepEqual(harness.actions, [
    {
      path: '/settings/invite',
      body: {
        pool_min_available: '20',
        pool_register_concurrency: '4',
        pool_check_seconds: '120',
        pool_otp_timeout: '240',
      },
    },
  ]);
});

test('actual dynamic mail editor renders new provider fields and submits only edited values', async () => {
  const config = {
    mail_source: 'custom',
    custom_endpoint: '***',
    custom_domain: 'example.test',
    custom_token: '***',
  };
  const harness = editorHarness('mail', config, [
    {
      kind: 'custom',
      display_name: '新邮箱来源',
      pooled: false,
      config_fields: [
        { key: 'custom_endpoint', label: '服务地址', type: 'text', required: false },
        { key: 'custom_domain', label: '收件域名', type: 'text', required: true },
        { key: 'custom_token', label: '服务密钥', type: 'password', required: true },
      ],
    },
  ]);
  harness.render();
  let tree = await harness.settled();
  assert.deepEqual(harness.actions, [], 'no diagnostic or writes on mount');
  const password = harness.find(tree, (node) => node.type === 'PasswordInput');
  assert.equal(password.props.value, '');
  assert.match(password.props.placeholder, /保留原值/);
  const domain = harness.find(tree, (node) => node.props?.label === '收件域名');
  domain.props.children.props.onChange({ target: { value: 'updated.test' } });
  tree = harness.render();
  harness
    .find(tree, (node) => node.type === 'Button' && node.props.children === '保存配置')
    .props.onClick();
  await harness.settled();
  assert.deepEqual(harness.actions, [
    { path: '/settings/mail', body: { custom_domain: 'updated.test' } },
  ]);
  assert.equal(
    harness.notices.some(({ level }) => level === 'warning' || level === 'error'),
    false,
  );
});

test('actual SMS editor preserves country clearing and blank independent platform keys', async () => {
  const config = {
    sms_allowed_countries: '52',
    sms_platform_mode: 'auto',
    sms_api_key_smsbower: '***',
    sms_api_key_herosms: '***',
    sms_api_key_smspool: '***',
  };
  const harness = editorHarness('sms', config);
  harness.render();
  let tree = await harness.settled();
  assert.deepEqual(harness.actions, []);
  assert.ok(harness.reads.includes('/settings/sms/all_countries?provider='));
  const country = harness.find(tree, (node) => node.props?.label === '允许的国家');
  country.props.children.props.onChange([]);
  tree = harness.render();
  harness
    .find(tree, (node) => node.type === 'Button' && node.props.children === '保存配置')
    .props.onClick();
  await harness.settled();
  assert.deepEqual(harness.actions, [{ path: '/settings/sms', body: { sms_allowed_countries: '' } }]);
});

for (const { saved, selected, changed, expected } of [
  { saved: 'auto', selected: [], changed: ['herosms'], expected: 'herosms' },
  { saved: 'smsbower', selected: ['smsbower'], changed: ['herosms', 'smsbower'], expected: 'smsbower,herosms' },
  { saved: 'smsbower,herosms', selected: ['smsbower', 'herosms'], changed: [], expected: 'auto' },
  { saved: 'herosms, smsbower', selected: ['smsbower', 'herosms'], changed: ['smspool', 'herosms', 'smsbower'], expected: 'smsbower,herosms,smspool' },
]) {
  test(`SMS platform multiselect restores ${saved} and saves ${expected} without resending keys`, async () => {
    const harness = editorHarness('sms', {
      sms_platform_mode: saved,
      sms_api_key_smsbower: '***', sms_api_key_herosms: '***', sms_api_key_smspool: '***',
    });
    harness.render();
    let tree = await harness.settled();
    const field = harness.find(tree, (node) => node.props?.label === '接码平台');
    assert.equal(field.props.children.props.mode, 'multiple');
    assert.deepEqual(field.props.children.props.value, selected);
    assert.deepEqual(field.props.children.props.options.map((item) => item.value), ['smsbower', 'herosms', 'smspool']);
    field.props.children.props.onChange(changed);
    tree = harness.render();
    harness.find(tree, (node) => node.type === 'Button' && node.props.children === '保存配置').props.onClick();
    tree = await harness.settled();
    assert.deepEqual(harness.actions, [{ path: '/settings/sms', body: { sms_platform_mode: expected } }]);
    assert.deepEqual(harness.find(tree, (node) => node.props?.label === '接码平台').props.children.props.value,
      expected === 'auto' ? [] : expected.split(','));
    assert.ok(harness.reads.includes('/settings/sms/all_countries?provider=' + encodeURIComponent(expected === 'auto' ? '' : expected)));
  });
}

test('SMS editor omits legacy reuse fields and submits one saved-config test for all three platforms', async () => {
  const config = {
    sms_allowed_countries: '52',
    sms_country: '52',
    sms_provider: 'smsbower',
    sms_use_monitor_best: '0',
    sms_auto_top_n: '60',
    sms_auto_country: '1',
    sms_platform_mode: 'herosms',
    sms_reuse_phone: '1',
    sms_phone_success_max: '8',
    sms_max_phone_attempts: '9',
    sms_per_phone_timeout: '90',
    sms_prefix_max_retry: '99',
  };
  const result = {
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
  };
  const harness = editorHarness('sms', config, [], { '/settings/sms/test': result });
  harness.render();
  let tree = await harness.settled();
  for (const label of [
    '启用号码复用',
    '同号成功复用上限',
    '最多换号次数（空为平台默认）',
    '单号等待时间（秒）',
    '前缀命中后重租上限',
  ])
    assert.equal(
      harness.find(tree, (node) => node.props?.label === label),
      undefined,
    );
  for (const label of ['默认国家','手动默认平台','使用价格任务最优结果','候选国家数量','没有最优结果时自动选号'])
    assert.equal(harness.find(tree, (node) => node.props?.label === label), undefined);
  const country = harness.find(tree, (node) => node.props?.label === '允许的国家');
  country.props.children.props.onChange(['7']);
  tree = harness.render();
  harness
    .find(tree, (node) => node.type === 'Button' && node.props.children === '测试三个平台余额')
    .props.onClick();
  tree = await harness.settled();
  assert.deepEqual(harness.actions, [{ path: '/settings/sms/test', body: {} }]);
  assert.equal(config.sms_allowed_countries, '52', 'unsaved inputs cannot alter the saved-config test');
  assert.deepEqual(
    harness.find(tree, (node) => node.type === 'SmsBalanceDetails').props.result,
    result,
  );
  harness
    .find(tree, (node) => node.type === 'Button' && node.props.children === '保存配置')
    .props.onClick();
  await harness.settled();
  assert.deepEqual(harness.actions.at(-1), { path: '/settings/sms', body: { sms_allowed_countries: '7' } });
});

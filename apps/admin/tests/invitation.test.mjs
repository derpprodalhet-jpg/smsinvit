import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

function compile(file) {
  return ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
}
function screen(file, fixtures = {}) {
  const requests = [];
  const cache = new Map();
  const load = (source) => {
    if (cache.has(source)) return cache.get(source).exports;
    const require = createRequire(source),
      module = { exports: {} };
    cache.set(source, module);
    new Function('require', 'module', 'exports', compile(source))(
      (name) => {
        if (name.endsWith('/admin-page'))
          return {
            AdminPage: ({ title, children }) =>
              React.createElement('main', null, React.createElement('h1', null, title), children),
          };
        if (name.endsWith('/use-invitation-query'))
          return {
            useInvitationQuery: (path, interval = 5000) => {
              requests.push({ path, interval });
              return {
                data: fixtures[path],
                loading: false,
                error: '',
                reload: async () => {},
                setData: () => {},
              };
            },
          };
        if (name === '../maintenance')
          return {
            useInvitationAdminQuery: (path, interval = 5000) => {
              requests.push({ path, interval });
              return { data: fixtures[path], loading: false, error: '', reload: async () => {} };
            },
            invitationDuration: (seconds) => (seconds == null ? '—' : `${seconds} 秒`),
          };
        if (name.endsWith('/invitation-runtime'))
          return {
            useInvitationRuntime: () => ({
              runId: null,
              run: null,
              log: '',
              events: [],
              running: false,
              watchRun: () => {},
              clearRun: () => {},
              refresh: async () => {},
            }),
          };
        if (name.endsWith('/invitation-run-panel')) return { InvitationRunPanel: () => null };
        if (name.endsWith('/navigation'))
          return {
            Link: ({ href, children }) =>
              React.createElement('a', { href: `/admin${href}` }, children),
            useRouter: () => ({ push: () => {}, replace: () => {} }),
          };
        if (name.endsWith('/admin-api'))
          return {
            adminApi: () => {
              throw new Error('Screen rendering must never mutate or call upstream');
            },
          };
        if (name.startsWith('.')) {
          for (const extension of ['.ts', '.tsx']) {
            const target = resolve(dirname(source), name + extension);
            if (existsSync(target)) return load(target);
          }
        }
        return require(name);
      },
      module,
      module.exports,
    );
    return module.exports;
  };
  const page = load(
    fileURLToPath(new URL('../src/pages/invitation/' + file, import.meta.url)),
  ).default;
  return { html: renderToStaticMarkup(React.createElement(page)), requests };
}

for (const [file, title] of [
  ['page.tsx', '邀请概览'],
  ['mail-accounts/page.tsx', '邮箱管理'],
  ['register/page.tsx', '单次注册'],
  ['registered/page.tsx', '注册结果'],
  ['runs/page.tsx', '运行记录'],
  ['cdk/page.tsx', '卡密管理'],
  ['proxies/page.tsx', '代理池'],
  ['sms-prices/page.tsx', '接码价格'],
  ['settings/page.tsx', '服务配置'],
  // 'legacy/page.tsx'（迁移前历史）已按运营要求下线：菜单、路由与页面一并删除，
  // 这里不再断言该屏存在。后端只读历史接口（legacy_router）仍保留，未在本次变更内。
])
  test(`${title} renders a native SmsInvit screen without an embedded web page`, () => {
    const { html } = screen(file);
    assert.match(html, new RegExp(title));
    assert.doesNotMatch(
      html,
      /<(iframe|object|embed)\b|\/admin\/invitation\/native#|password_hash|sessionSecret/i,
    );
  });

test('overview displays business stats, best SMS and recent execution with native destinations', () => {
  const { html, requests } = screen('page.tsx', {
    '/overview': {
      stats: { total: 22, available: 18 },
      cdk: { unused: 17, done: 1, mail_pool: -1 },
      proxies: { ready: 8, dead: 1 },
      mail_source: 'fixture-provider',
      pool_available: -1,
      sms: {
        best: {
          platform_label: 'Fixture SMS',
          country_name: '测试国家',
          country: '99',
          price: 0.1,
          stock: 40,
          success_rate: 0.75,
          score: 0.3,
        },
        rounds: 4,
      },
      runs: [
        {
          run_id: 'fixture-run',
          email: 'fixture@example.test',
          kind: 'codex_auth',
          status: 'done',
          started_at: 1700000000,
        },
      ],
    },
  });
  for (const text of [
    '邮箱总计',
    '卡密 / 邀请概览',
    '代理池健康度',
    'Fixture SMS',
    '75%',
    'fixture@example.test',
    'fixture-provider',
    '不限量',
  ])
    assert.ok(html.includes(text), text);
  assert.match(html, /href="\/admin\/invitation\/cdk"/);
  assert.match(html, /href="\/admin\/invitation\/proxies"/);
  assert.match(html, /href="\/admin\/invitation\/sms-prices"/);
  assert.deepEqual(requests, [{ path: '/overview', interval: 5000 }]);
});

test('runs show execution stages, progress, attempts and authenticated maintenance interface', () => {
  const { html, requests } = screen('runs/page.tsx', {
    // 状态/类型筛选与分页现在都在服务端，请求带上它们。
    '/runs?limit=100&offset=0&status=&kind=': {
      items: [
        {
          run_id: 'fixture-run',
          email: 'fixture@example.test',
          kind: 'codex_auth',
          status: 'running',
          stage_label: '等待邀请信',
          progress: 35,
          attempt: 2,
          elapsed: 63,
        },
        {
          run_id: 'authorization-done-sync-failed',
          email: 'sync@example.test',
          kind: 'codex_auth',
          status: 'done',
          cdk_sync_status: 'failed',
          cdk_sync_error: '同步连接失败',
        },
      ],
    },
    '/cdk/monitor?limit=100': {
      items: [],
      events: [],
      summary: { running: 1, queued: 2, max_concurrency: 3 },
    },
  });
  for (const text of [
    '等待邀请信',
    '35%',
    '第 2 次',
    '日志详情',
    '执行占位复核',
    '结果待复核',
    '邀请作业 / 排队',
    '最近事件',
    '已完成',
    '同步失败',
    // 「卡密同步」不再单独占一列（codex_auth 运行里只有人工授权那几条才有该状态，
    // 其余永远是「—」）。待同步/同步失败改挂在卡密单元格里；这条运行没有绑定卡密，
    // 所以同时会渲染「未绑定卡密」——失败信号不会因为没有卡密就消失。
    '未绑定卡密',
  ])
    assert.ok(html.includes(text), text);
  assert.ok(requests.some(({ path }) => path === '/api/v1/admin/invitation/execution-permits'));
  assert.ok(requests.some(({ path, interval }) => path?.startsWith('/runs?') && interval === 5000));
  assert.ok(
    requests.some(({ path, interval }) => path?.startsWith('/cdk/monitor') && interval === 2000),
  );
});

test('mailbox identity stages read as account milestones, not as run completion', () => {
  const { html } = screen('mail-accounts/page.tsx', {
    '/accounts?status=&kind=&limit=20&offset=0': {
      items: [{ email: 'registered@example.test', kind: 'cf_temp', status: 'done' }],
      total: 1,
    },
  });
  // 账号类型只显示身份词「Codex」，绝不复用运行状态里的「已完成」。
  assert.ok(html.includes('Codex'));
  assert.ok(html.includes('账号类型'));
  assert.ok(html.includes('registered@example.test'));
  assert.ok(!html.includes('已完成'));
  assert.ok(!html.includes('Codex（已授权）'), '长标签已按运营要求收敛成三个词');
  // 来源也只放短名，全称改到悬浮里。
  assert.ok(html.includes('CF临时'));
  assert.ok(!html.includes('CF Worker 临时邮箱'));
  // 筛选下拉与批量删除用的是**另一份**标签表，只在展开下拉时才进 DOM，
  // 所以直接查源码 —— 上一轮就是漏了这一处，运营看到「前端没变」。
  const source = readFileSync(
    new URL('../src/pages/invitation/mail-accounts/page.tsx', import.meta.url),
    'utf8',
  );
  assert.doesNotMatch(source, /GPT（已注册）|Codex（已授权）|'新邮箱'/);
  assert.match(source, /available: '邮箱'/);
  assert.match(source, /registered: 'GPT'/);
  assert.match(source, /done: 'Codex'/);
});

test('SMS pricing describes the project experience list without promising delivery', () => {
  for (const openai_safe of [true, false]) {
    const { html } = screen('sms-prices/page.tsx', {
      '/sms/prices': {
        effective: { source: 'monitor', openai_safe },
      },
    });
    assert.ok(html.includes('项目经验名单'));
    assert.ok(html.includes('不能保证发码或收码成功'));
    assert.ok(!html.includes('OpenAI 短信白名单'));
  }
});

// 旧期望已删除：'legacy reads one bounded page and never displays encrypted credential material'
// 断言的是「迁移前历史」页面（legacy/page.tsx）。运营要求删除该页面，页面文件已不存在，
// 因此该用例整体移除；无需再渲染一个已下线的屏。后端只读历史接口保持原样，未在本次变更内。

test('manual permit release requires explicit stopped confirmation and a single-line review note', () => {
  const source = new URL('../src/pages/invitation/runs/permit-release.ts', import.meta.url);
  const module = { exports: {} };
  new Function('module', 'exports', compile(source))(module, module.exports);
  const { permitReleaseBody } = module.exports;
  const token = '11111111-1111-4111-8111-111111111111';
  assert.deepEqual(permitReleaseBody(token, ' 已停止并复核 ', true), {
    ownerToken: token,
    note: '已停止并复核',
    confirmAllExecutorsStopped: true,
  });
  for (const [note, confirmed] of [
    ['已停止并复核', false],
    ['', true],
    ['短', true],
    ['跨行\n说明', true],
    ['a'.repeat(251), true],
  ])
    assert.throws(() => permitReleaseBody(token, note, confirmed), /必须确认/);
});

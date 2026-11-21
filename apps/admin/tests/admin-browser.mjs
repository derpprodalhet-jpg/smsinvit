/* Actual Python static routing + Chromium; every API request is a local fixture. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.SMSINVIT_PLAYWRIGHT_PATH || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const output = path.join(root, 'apps/admin/test-results');
fs.mkdirSync(output, { recursive: true });
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'smsinvit-admin-entry-'));
fs.symlinkSync(path.join(root, 'apps/admin/dist'), path.join(temp, 'admin'), 'dir');
fs.writeFileSync(path.join(temp, 'index.html'), '<html>public fixture</html>');
// The boundary middleware rejects any request whose Origin is not an explicitly
// declared CORS origin, and browsers do send Origin on same-origin module
// scripts / stylesheets. Bind the socket first so the fixture can declare the
// real http://127.0.0.1:<port> as its allowed origin, exactly like a real
// deployment declares its own public URL.
const code = `import socket,sys\nfrom types import SimpleNamespace\nfrom smsinvit.app import Services,create_app\nfrom smsinvit.config import Config\nimport uvicorn\nsock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]\nc=Config({'smsinvit.inventory-encryption-key':'a1'*32,'smsinvit.inventory-fingerprint-key':'synthetic-test','smsinvit.jwt-secret':'synthetic-jwt-secret-at-least-32-characters','SMSINVIT_STATIC_DIR':sys.argv[1],'smsinvit.admin-path':'/admin-v1/','runtime.secure_cookies':False,'runtime.cors_origins':f'http://127.0.0.1:{port}'},environ={})\nd=SimpleNamespace(one=lambda *a:None,all=lambda *a:[],execute=lambda *a:None)\ns=Services(d,c);s.limiter=SimpleNamespace(check=lambda *a:None)\na=create_app(c,d,services=s,include_domains=False,load_runtime=False)\nprint(port,flush=True)\nuvicorn.Server(uvicorn.Config(a,log_level='error',access_log=False)).run(sockets=[sock])`;
const child = spawn(process.env.SMSINVIT_TEST_PYTHON || path.join(root, '.dsh-venv2/bin/python'), ['-c', code, temp], { cwd: root, env: { ...process.env, PYTHONPATH: path.join(root, 'apps/api') }, stdio: ['ignore', 'pipe', 'pipe'] });
let logs = '';
child.stderr.on('data', chunk => { logs += chunk.toString(); });
let browser;
try {
  const port = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('Preview startup timeout: ' + logs)), 10000);
    child.stdout.once('data', chunk => { clearTimeout(timeout); resolve(Number(chunk.toString().trim())); });
    child.once('exit', () => { clearTimeout(timeout); reject(Error('Preview exited: ' + logs)); });
  });
  const base = `http://127.0.0.1:${port}`;
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const results = [];
  for (const width of [375, 1440]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    const page = await context.newPage();
    let loggedIn = false, challengeCount = 0, loginCount = 0;
    const requests = [], failures = [], violations = [];
    // 结构验收用的固定标识：批次号必须与后端派生规则一致（BATCH-YYYYMMDD-XXXXXXXX）。
    const BATCH_ID = '9a3e47ac-00d0-4000-8000-000000000001';
    const BATCH_NO = 'BATCH-20261010-9A3E47AC';
    const CDK = 'LC-CDK-9fa2FIXTUREQ7Kd';
    const user = { id: 'fixture', username: 'test-admin', email: 'admin@example.test', displayName: '测试管理员', roles: ['SUPER_ADMIN'], totpEnabled: true, totpSetupRequired: false };
    page.on('pageerror', error => failures.push(error.message));
    await page.addInitScript(() => { window.cspViolations = []; document.addEventListener('securitypolicyviolation', event => window.cspViolations.push(event.violatedDirective)); });
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== base) { failures.push('External request: ' + url.origin); return route.abort(); }
      if (!url.pathname.startsWith('/api/')) return route.continue();
      requests.push(url.pathname);
      const respond = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? { success: true, data } : { success: false, error: { code: 'AUTH_REQUIRED', message: '请先登录' } }) });
      // 真实登录响应会下发会话与 CSRF Cookie；管理端每个受保护请求都先看
      // lc_admin_session，缺了就会直接跳回登录页。夹具必须照实下发。
      const issueSessionCookies = async () => {
        const origin = new URL(base);
        await context.addCookies([
          { name: 'lc_admin_session', value: '1', domain: origin.hostname, path: '/' },
          { name: 'lc_csrf', value: 'synthetic-csrf', domain: origin.hostname, path: '/' },
        ]);
      };
      if (url.pathname.endsWith('/auth/me')) return respond(loggedIn ? user : null, loggedIn ? 200 : 401);
      if (url.pathname.endsWith('/auth/captcha')) return respond({ captchaToken: 'challenge-' + (++challengeCount), prompt: '12 + 7 = ?', expiresInSeconds: 300, type: 'math' });
      if (url.pathname.endsWith('/auth/login')) {
        const body = request.postDataJSON();
        assert.equal(body.captchaAnswer, '19');
        assert.equal(body.turnstileToken, undefined);
        loginCount++;
        return respond({ needTotp: true, challengeToken: 'totp-fixture', user });
      }
      if (url.pathname.endsWith('/auth/login/totp')) { loggedIn = true; await issueSessionCookies(); return respond({ user }); }
      if (url.pathname.endsWith('/auth/totp')) return respond({ totpEnabled: true, totpSetupRequired: false, backupCodesRemaining: 8 });
      if (url.pathname.endsWith('/auth/logout')) { loggedIn = false; return respond({}); }
      // ── 兑换工作台（冻结契约 /api/v1/admin/stats/overview）──
      // 块① 台账必须有 4 个模块行 + 合计行；块② 成本按币种分行（不做跨币种合计）；
      // 块③ 待处理小卡跟随 pending[]。夹具数字刻意可区分，避免「全是 0 也通过」。
      if (url.pathname.endsWith('/api/v1/admin/stats/overview'))
        return respond({
          businessTimeZone: 'Asia/Shanghai',
          generatedAt: '2026-10-11T08:00:00Z',
          cards: {
            modules: [
              { module: 'sms', label: '接码', generatedTotal: 1234, generatedToday: 12, redeemedTotal: 1100, redeemedToday: 5, expired: 3, expiredApplicable: true },
              { module: 'invitation', label: 'Codex 邀请', generatedTotal: 77, generatedToday: 7, redeemedTotal: 60, redeemedToday: 6, expired: 0, expiredApplicable: false },
            ],
            totals: { generatedTotal: 1311, generatedToday: 19, redeemedTotal: 1160, redeemedToday: 11, expired: 3 },
          },
          costs: [
            { key: 'sms_bdt', label: '接码（BDT）', currency: 'BDT', totalMinor: 30000, todayMinor: 1250, basis: '完成会话的上游扣款', available: true, availableSince: null, unpricedCount: 0, detailPath: '/sms/redeems' },
            { key: 'sms', label: '接码（美区 GPT）', currency: 'USD', totalMinor: 15, todayMinor: 0, basis: '成功会话已确认成本', available: true, availableSince: null, unpricedCount: 0, detailPath: '/sms/redeems' },
          ],
          pending: [
            { key: 'sms_uncertain', label: '接码待确认', count: 2, detailPath: '/sms/redeems' },
            { key: 'codex_settlement_review', label: '结算待复核', count: 0 },
          ],
        });
      // ── 结构验收夹具：卡密批次 / 卡密管理 / 运行记录 ──
      // 邀请原生接口直接读整段 JSON（invitationApi 返回 raw），不能再包一层 data。
      const respondRaw = (payload, status = 200) =>
        route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(payload) });
      const respondMeta = (data, meta) =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, data, meta }) });
      if (url.pathname.endsWith('/api/v1/admin/plans'))
        return respondMeta(
          [{ id: 'sku-1', code: 'SMS-REDEEM', name: '接码测试套餐', status: 'ACTIVE', attributesJson: { productKind: 'SMS_REDEEM', cdkValidDays: 15 }, createdAt: '2026-10-10T00:00:00Z', updatedAt: '2026-10-10T00:00:00Z' }],
          { page: 1, pageSize: 100, total: 1, totalPages: 1 },
        );
      if (url.pathname.endsWith('/api/v1/admin/inventory/batches'))
        return respondMeta(
          [{ id: BATCH_ID, skuId: 'sku-1', batchNo: BATCH_NO, note: '', source: 'GENERATE', requestedCount: 2, successCount: 2, duplicateCount: 0, invalidCount: 0, attributesSnapshotJson: { productKind: 'SMS_REDEEM', cdkValidDays: 15 }, actorEmail: 'admin@example.test', createdAt: '2026-10-10T01:35:24Z' }],
          { page: 1, pageSize: 20, total: 1, totalPages: 1 },
        );
      if (/\/api\/v1\/admin\/inventory\/batches\/[^/]+\/cards$/.test(url.pathname))
        return respondMeta(
          [{ id: 'card-1', status: 'ISSUED', issuedAt: '2026-10-10T01:35:24Z', secretFingerprint: 'a'.repeat(64), secretMasked: 'LC-CDK-9fa2…Q7Kd', secret: CDK }],
          { page: 1, pageSize: 20, total: 1, totalPages: 1 },
        );
      if (url.pathname.endsWith('/api/v1/admin/invitation/execution-permits'))
        return respond({ items: [], automaticReclaim: false });
      if (url.pathname.endsWith('/api/v1/admin/invitation/task-holds'))
        return respond({ items: [], reviewRequiresExecutorsStopped: false });
      if (url.pathname.endsWith('/invitation/native/api/cdk/monitor'))
        return respondRaw({
          ok: true,
          items: [{ job_id: 'job-1', email: 'user@example.test', status: 'queued', stage: 'queued', stage_label: '正在排队', progress: 0, attempt: 1, proxy: '', error: '' }],
          events: [],
          summary: { running: 0, queued: 1, done: 3, failed: 1, workers: 2, max_concurrency: 3, proxy_available: 5, mail_pool: 11, itemsReturned: 1, itemsLimit: 40 },
        });
      if (url.pathname.endsWith('/invitation/native/api/runs'))
        return respondRaw({
          ok: true,
          total: 1,
          items: [{ run_id: 'run-1', job_id: 'job-1', email: 'user@example.test', status: 'queued', kind: 'codex_auth', stage: 'queued', stage_label: '正在排队', progress: 0, attempt: 1, started_at: Math.floor(Date.now() / 1000) - 30, error: '', cdk_code_masked: 'LC-CDK-9fa2…Q7Kd', cdk_batch: BATCH_NO, cdk_status: 'bound', cdk_fail_count: 0 }],
        });
      if (url.pathname.endsWith('/invitation/native/api/cdk/list'))
        return respondRaw({ ok: true, total: 1, batches: [BATCH_NO], stats: {}, items: [{ code: CDK, status: 'bound', email: 'user@example.test', batch: BATCH_NO, attempts: 0, fail_count: 0, created_at: Math.floor(Date.now() / 1000) - 60 }] });
      if (url.pathname.endsWith('/invitation/native/api/cdk/stats'))
        return respondRaw({ ok: true, stats: { total: 1, unused: 0, bound: 1, queued: 0, running: 0, done: 0, failed: 0, void: 0, mail_pool: 11 } });
      if (url.pathname.endsWith('/invitation/native/api/cdk/export/formats'))
        return respondRaw({ ok: true, formats: [] });
      if (url.pathname.endsWith('/invitation/native/api/proxy/pool'))
        return respondRaw({ ok: true, items: [] });
      if (url.pathname.endsWith('/invitation/native/api/registered/export/formats'))
        return respondRaw({ ok: true, formats: [] });
      if (url.pathname.endsWith('/invitation/native/api/registered'))
        return respondRaw({
          ok: true,
          total: 1,
          items: [{
            email: 'user@example.test',
            password: '***',
            totp_secret: '***',
            at_len: 1988, st_len: 4120, rt_len: 0, codex_rt_len: 640,
            source: 'invite', phone_bound_ok: 1, created_at: Math.floor(Date.now() / 1000) - 60,
          }],
        });
      // ── 两个「卡密管理」页面的夹具 ──
      if (url.pathname.endsWith('/api/v1/admin/sms/redeems'))
        return respondMeta(
          [{ id: 'sms-key-1', status: 'UNUSED', inventoryItemId: 'inv-sms-1', codeMasked: 'LC-CDK-abcd…WXYZ',
             codeFingerprint: 'b'.repeat(64), smsServiceName: 'SmsBower', smsCountryName: '泰国',
             skuName: '接码套餐', attemptCount: 0, maxAttempts: 3, issuedAt: '2026-10-10T01:00:00Z',
             expiresAt: '2026-10-25T01:00:00Z' }],
          { page: 1, pageSize: 20, total: 1, totalPages: 1 },
        );
      if (url.pathname.endsWith('/api/v1/admin/sms/redeems'))
        return respondMeta(
          [{ id: 'sms-key-1', status: 'CONSUMED', inventoryItemId: 'inv-sms-1', codeMasked: 'LC-CDK-efgh…UVWX',
             codeFingerprint: 'c'.repeat(64), plan: 'plus', paymentCountry: 'PH', paymentCurrency: 'PHP',
             accountEmail: 'buyer@example.test', skuName: 'Plus 1 个月', postActionStatus: 'DONE',
             issuedAt: '2026-10-10T01:00:00Z', expiresAt: '2026-10-25T01:00:00Z' }],
          { page: 1, pageSize: 20, total: 1, totalPages: 1 },
        );
      
      
      return respond({});
    });
    const response = await page.goto(base + '/admin-v1/security?from=bookmark');
    assert.equal(response.status(), 200);
    await page.waitForURL('**/admin-v1/login');
    await page.getByLabel('计算验证码', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: /^登\s*录$/ }).isDisabled(), true);
    await page.getByRole('button', { name: '刷新计算验证码' }).click();
    await page.waitForFunction(() => document.querySelector('button[aria-label="刷新计算验证码"]')?.textContent.includes('12 + 7'));
    assert.equal(challengeCount, 2);
    await page.getByPlaceholder('用户名或邮箱').fill('test-admin');
    await page.getByPlaceholder('密码', { exact: true }).fill('synthetic-password');
    await page.getByLabel('计算验证码', { exact: true }).fill('19');
    await page.getByRole('button', { name: /^登\s*录$/ }).click();
    await page.getByPlaceholder('6 位验证码或备份码').waitFor();
    assert.equal(loginCount, 1);
    await page.screenshot({ path: path.join(output, `admin-totp-${width}.png`), fullPage: true });
    await page.getByPlaceholder('6 位验证码或备份码').fill('123456');
    await page.getByRole('button', { name: '验证并登录' }).click();
    await page.waitForURL('**/admin-v1/');
    // A fresh deep-link navigation exercises lazy imports relative to the injected base.
    await page.goto(base + '/admin-v1/security?from=bookmark');
    await page.getByText('登录密码与谷歌验证器（TOTP）').waitFor();
    assert.equal(new URL(page.url()).search, '?from=bookmark');
    await page.reload();
    await page.getByText('登录密码与谷歌验证器（TOTP）').waitFor();
    await page.screenshot({ path: path.join(output, `admin-security-${width}.png`), fullPage: true });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(overflow, false);
    // ── 结构验收（只在 1440 档跑一次）──
    // 1) 卡密批次：删除「单卡管理」，批次详情抽屉给出批次号与导入独角指引。
    // 2) 卡密管理：删除与运行记录重复的「实时执行监控」。
    // 3) 运行记录：全局计数补齐（可用代理 / 可兑换账号），并可对排队作业点「停止」。
    if (width === 1440) {
      // 0) 兑换工作台：三块（卡密台账 / 成本 / 待处理），不得退回 12 张扁平卡片墙。
      await page.goto(base + '/admin-v1/');
      await page.getByText('兑换工作台').first().waitFor();
      await page.getByText('① 卡密台账（按模块）').first().waitFor();
      const ledger = page.locator('.ant-table').first();
      const ledgerHeaders = (await ledger.locator('thead th').allInnerTexts()).map((t) => t.trim());
      assert.deepEqual(
        ledgerHeaders.slice(0, 6),
        ['模块', '累计生成', '今日生成', '累计核销', '今日核销', '已过期'],
        `台账列顺序错误：${ledgerHeaders.join(' / ')}`,
      );
      const ledgerText = await ledger.locator('tbody').innerText();
      for (const label of ['接码', 'Codex 邀请', '合计'])
        assert.ok(ledgerText.includes(label), `台账缺少行「${label}」`);
      assert.ok(ledgerText.includes('不适用'), 'Codex 邀请没有有效期概念，必须显示「不适用」');
      // 块②：同一成本项按币种分行，金额带币种后缀，且不做跨币种相加。
      await page.getByText('300.00 BDT').first().waitFor();
      await page.getByText('$0.15').first().waitFor();
      assert.equal(await page.getByText('300.15 BDT').count(), 0, '不同币种不得相加');
      // 块③：待处理小卡跟随 pending[]。
      await page.getByText('③ 待处理').first().waitFor();
      await page.getByText('结算待复核').first().waitFor();
      // 旧卡片墙的标题不得复活。
      assert.equal(await page.getByText('今日接码成功').count(), 0, '工作台不得退回扁平卡片墙');
      assert.equal(await page.getByText('累计生成卡密').count(), 0, '工作台不得退回扁平卡片墙');
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
        '工作台不应横向溢出',
      );
      await page.screenshot({ path: path.join(output, 'admin-workbench-1440.png'), fullPage: true });

      await page.goto(base + '/admin-v1/batches?skuId=sku-1');
      await page.getByText('卡密批次').first().waitFor();
      await page.getByText(BATCH_NO).first().waitFor();
      assert.equal(await page.getByRole('tab', { name: '单卡管理' }).count(), 0, '「单卡管理」标签页必须删除');
      await page.getByRole('button', { name: '详情' }).first().click();
      await page.getByText('导入独角').first().waitFor();
      await page.getByText('本批卡密').first().waitFor();
      await page.getByText(CDK).first().waitFor();
      // Drawer 有 ~300ms 滑入动画：等它停稳再截图，否则截到的是还在屏外的面板。
      await page.waitForTimeout(600);
      // Drawer 是 position:fixed 的传送门内容，fullPage 截图会把它排到滚动区之外，
      // 所以这两个证据图用视口截图。
      await page.screenshot({ path: path.join(output, 'admin-batches-detail-1440.png') });

      await page.goto(base + '/admin-v1/invitation/cdk');
      await page.getByText('卡密管理').first().waitFor();
      assert.equal(await page.getByText('实时执行监控').count(), 0, '与运行记录重复的「实时执行监控」必须删除');

      await page.goto(base + '/admin-v1/invitation/runs');
      await page.getByText('运行记录').first().waitFor();
      await page.getByText('可用代理').first().waitFor();
      await page.getByText('可兑换账号').first().waitFor();
      // 必须按「按钮」断言：运行记录里本来就有大量「执行实例已停止」之类的文案，
      // 用纯文本匹配会在完全没有停止按钮时也通过。
      await page.getByRole('button', { name: '停止' }).first().waitFor();
      await page.screenshot({ path: path.join(output, 'admin-runs-counters-1440.png'), fullPage: true });

      // 4) 注册结果：凭证列必须在列表里（运营要求一屏看到每行有哪些凭证），
      //    且「操作」是最后一列（贴右）。
      await page.goto(base + '/admin-v1/invitation/registered');
      await page.getByText('注册结果').first().waitFor();
      await page.getByText('user@example.test').first().waitFor();
      const headers = (await page.locator('.ant-table-thead th').allInnerTexts()).map((text) =>
        text.trim(),
      );
      for (const column of ['邮箱', '密码', '2FA', 'Access', 'Session', 'RT', 'Codex RT', 'Plus 状态', '操作'])
        assert.ok(
          headers.some((header) => header.includes(column)),
          `注册结果列表缺少「${column}」列：${headers.join(' / ')}`,
        );
      assert.equal(headers[headers.length - 1], '操作', '「操作」必须是最后一列（贴右）');
      await page.screenshot({ path: path.join(output, 'admin-registered-columns-1440.png'), fullPage: true });

      // 5) 两个「卡密管理」页面：菜单存在 + 页面真能渲染出卡密台账。
      //    侧边栏菜单项与路由都要在，避免只有路由没有入口（或反之）。
      const sections = [
        { path: '/sms/cdk', title: '接码卡密管理', status: '未使用', masked: 'LC-CDK-abcd…WXYZ' },
        // 卡密终态统一「已核销」，与接码/邀请口径一致。
        { path: '/sms/cdk', title: '接码卡密', status: '已核销', masked: 'LC-CDK-efgh…UVWX' },
      ];
      for (const section of sections) {
        await page.goto(base + '/admin-v1' + section.path);
        await page.getByText(section.title).first().waitFor();
        await page.getByText(section.masked).first().waitFor();
        await page.getByText(section.status).first().waitFor();
        // 完整卡密在详情里按需复制；列表只给脱敏值。
        assert.equal(
          await page.getByText('已脱敏（需要卡密权限）').count(),
          0,
          `${section.path} 有脱敏值时不应再提示缺少权限`,
        );
      }
      // 侧边栏的子项默认折叠，innerText 取不到嵌套标签，所以这里只断言三个顶级分组
      // 存在（证明侧边栏渲染正常）；「子项名称与路由是否齐全」由
      // tests/module-cdk-pages.test.mjs 做源码级断言。
      const menu = await page.locator('.ant-pro-sider-menu').innerText();
      for (const group of ['接码', 'Codex 邀请'])
        assert.ok(menu.includes(group), `侧边栏缺少顶级分组「${group}」`);
      await page.screenshot({ path: path.join(output, 'admin-cdk-1440.png'), fullPage: true });
    }
    // 侧边栏：一级与二级菜单项都必须有图标（运营明确要求）。
    // 现场教训：一级有图标、二级没有，肉眼看截图时容易被忽略 —— 这里逐项断言。
    // 必须显式回到工作台再断言：此时页面可能还停在别处，或侧栏处于折叠态。
    await page.goto(base + '/admin-v1/');
    await page.locator('.ant-pro-sider, [class*="sider"]').first().waitFor();
    const expand = await page.locator('.ant-menu-submenu-title').count();
    for (let i = 0; i < expand; i += 1) await page.locator('.ant-menu-submenu-title').nth(i).click();
    await page.waitForTimeout(400);
    fs.writeFileSync('/tmp/subitem.html', await page.evaluate(() => {
      const el = [...document.querySelectorAll('.ant-menu-item')].find((x) => (x.textContent || '').includes('接码概览'));
      return el ? el.outerHTML : '(not found)';
    }));
    // 注意：ProLayout 会给二级项渲染一个**空的、display:none** 的图标占位，
    // 所以「DOM 里有 .anticon」是假阳性 —— 必须断言图标**真的可见**（有尺寸）。
    const menuItems = await page.locator('.ant-menu-item').evaluateAll((els) =>
      els.map((el) => {
        const icon = el.querySelector('.anticon');
        const box = icon ? icon.getBoundingClientRect() : null;
        return {
          text: (el.textContent || '').trim(),
          visible: Boolean(box && box.width > 0 && box.height > 0 && icon.querySelector('svg')),
        };
      }),
    );
    const missingIcons = menuItems.filter((item) => !item.visible).map((item) => item.text);
    if (width === 1440) {
      await page.screenshot({ path: path.join(output, 'admin-sidebar-icons-1440.png') });
      assert.equal(menuItems.length > 10, true, `侧边栏菜单项太少（${menuItems.length}），断言可能失效`);
      assert.deepEqual(missingIcons, [], `以下菜单项没有图标：${missingIcons.join(' / ')}`);
    }

    violations.push(...await page.evaluate(() => window.cspViolations));
    assert.deepEqual(violations, []);
    assert.deepEqual(failures, []);
    assert.equal(requests.some(url => /turnstile/.test(url)), false);
    for (const old of ['/admin/', '/admin/index.html', '/admin/assets/main.js']) {
      assert.equal((await context.request.get(base + old)).status(), 404);
    }
    results.push({ width, customEntry: true, arithmeticChallenge: true, totpLogin: true, deepLinkRefresh: true, oldEntry404: true, workbenchBlocks: width === 1440, cspViolations: 0, externalRequests: 0, structureVerified: width === 1440 });
    await context.close();
  }
  fs.writeFileSync(path.join(output, 'admin-browser-results.json'), JSON.stringify({ status: 'PASS', results }, null, 2));
  console.log(JSON.stringify({ status: 'PASS', results }));
} finally {
  await browser?.close();
  child.kill('SIGTERM');
  fs.rmSync(temp, { recursive: true, force: true });
}

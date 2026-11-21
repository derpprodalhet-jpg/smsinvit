import { runPublicPages } from './public-pages-browser.mjs';
/* Real Chromium interactions with the built page and local intercepted API only. */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.env.SMSINVIT_PLAYWRIGHT_PATH || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const dist = path.join(root, 'apps/web/dist');
const output = process.env.SMSINVIT_BROWSER_OUTPUT || path.join(root, 'apps/web/test-results');
const resultPath = path.join(output, 'invitation-browser-results.json');
const KEY = 'lc_invitation_recovery_v1';
const TTL = 86400000;
const idFor = index => `00000000-0000-4000-8000-${(index + 1).toString(16).padStart(12, '0')}`;
const secretFor = index => (index + 1).toString(16).padStart(64, '0');
const row = (index, line = 1, actions = ['CONFIRM_INVITE', 'POLL']) => ({ line, id: idFor(index), email: `synthetic-${index}@example.test`,
  sessionSecret: secretFor(index), status: 'bound', progressPercent: 0, displayResult: null, allowedActions: actions });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

async function fixture(browser, base, width = 375, options = {}) {
  const context = await browser.newContext({ viewport: { width, height: 950 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  const state = { canRedeem: true, canStart: true, sessions: new Map(), receipts: new Map(), codes: new Map(), next: 0,
    requests: [], errors: [], external: [], copies: [], metadataFailures: 0, dropRedeem: false, dropStart: false,
    receiptMode: null, pendingCycles: 0, redeemRateFailures: 0, statusFailure: false, holdStatus: null, holdConfirm: null, ...options.state };
  page.on('pageerror', error => state.errors.push(error.message));
  await page.addInitScript(({ key, stored, blockStorage }) => {
    window.fixtureCopies = [];
    window.fixtureHidden = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.fixtureHidden });
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => window.fixtureCopies.push(value) } });
    if (stored && !sessionStorage.getItem(key)) sessionStorage.setItem(key, stored);
    if (blockStorage) Storage.prototype.setItem = function () { throw new DOMException('blocked', 'QuotaExceededError'); };
  }, { key: KEY, stored: options.stored, blockStorage: options.blockStorage });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
    if (url.origin !== base) { state.external.push(url.origin); return route.abort('blockedbyclient'); }
    if (!endpoint.startsWith('/api/')) return route.continue();
    const payload = request.postData() ? request.postDataJSON() : null;
    state.requests.push({ endpoint, method: request.method(), payload, at: Date.now() });
    const availability = { canRedeem: state.canRedeem, canStart: state.canStart,
      unavailableMessage: state.canRedeem && state.canStart ? '' : '请稍候重试。' };
    const fulfill = data => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, data }) });
    const failure = (code, message, status = 503, headers = {}) => route.fulfill({ status, headers, contentType: 'application/json', body: JSON.stringify({ success: false, error: { code, message } }) });
    if (endpoint === '/api/v1/invitation/page-content') {
      if (state.metadataFailures-- > 0) return failure('SERVICE_UNAVAILABLE', '服务暂时不可用');
      return fulfill(availability);
    }
    if (endpoint === '/api/v1/invitation/redeem') {
      assert.ok(payload.codes.length <= 10);
      assert.equal(new Set(payload.codes).size, payload.codes.length);
      assert.match(payload.attemptId, /^[0-9a-f-]{36}$/);
      assert.match(payload.recoverySecret, /^[0-9a-f]{64}$/);
      const stored = await page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), KEY);
      assert.ok(stored.attempts.some(attempt => attempt.id === payload.attemptId && attempt.recoverySecret === payload.recoverySecret));
      if (state.redeemRateFailures-- > 0) return failure('RATE_LIMIT_EXCEEDED', '请求过于频繁', 429, { 'Retry-After': '2' });
      const invalid = payload.codes.findIndex(code => code === 'INVALID');
      const unavailable = !state.canRedeem && payload.codes.some(code => !state.codes.has(code));
      const items = invalid >= 0 ? [{ line: invalid + 1, error: { code: 'INVITATION_CDK_NOT_FOUND', message: '邀请券无效或不存在。' } }]
        : unavailable ? [{ line: 1, error: { code: 'INVITATION_MAIL_UNAVAILABLE', message: '邮箱暂时不可用' } }]
        : payload.codes.map((code, index) => {
          const existing = state.codes.get(code);
          if (existing) return { ...state.sessions.get(existing), line: index + 1 };
          const item = row(state.next++, index + 1);
          state.sessions.set(item.id, item); state.codes.set(code, item.id); return { ...item };
        });
      const receipt = { attemptId: payload.attemptId, recoverySecret: payload.recoverySecret,
        attemptStatus: invalid >= 0 || unavailable ? 'rejected' : 'completed', items };
      state.receipts.set(receipt.attemptId, receipt);
      if (state.dropRedeem) { state.dropRedeem = false; return route.abort('failed'); }
      return fulfill({ ...availability, ...receipt, ...(state.receiptMode ? { attemptStatus: state.receiptMode, items: [], message: '领取结果待核查，请勿重复领取。' } : {}) });
    }
    if (endpoint.startsWith('/api/v1/invitation/redemption-attempts/')) {
      const receipt = state.receipts.get(endpoint.split('/').at(-1));
      if (state.receiptMode === 'missing') return failure('INVITATION_ATTEMPT_NOT_FOUND', '领取结果无法确认', 404);
      assert.ok(receipt);
      assert.equal(request.headers()['x-session-secret'], receipt.recoverySecret);
      if (state.receiptMode === 'network') return route.abort('failed');
      if (state.pendingCycles-- > 0) return fulfill({ ...availability, attemptId: receipt.attemptId, attemptStatus: 'pending', items: [] });
      if (state.receiptMode === 'review') return fulfill({ ...availability, attemptId: receipt.attemptId, attemptStatus: 'review', items: [], message: '领取结果待人工核查，请勿重复领取。' });
      const items = receipt.items.map(item => item.id ? { ...state.sessions.get(item.id), line: item.line } : item);
      return fulfill({ ...availability, ...receipt, items });
    }
    if (['/api/v1/invitation/status', '/api/v1/invitation/start', '/api/v1/invitation/invite-sent'].includes(endpoint)) {
      assert.ok(payload.sessions.length <= 10);
      const items = payload.sessions.map(session => {
        const item = state.sessions.get(session.id);
        assert.ok(item);
        assert.equal(session.sessionSecret, item.sessionSecret);
        return item;
      });
      if (endpoint.endsWith('/status')) {
        if (state.statusFailure) return failure('SERVICE_UNAVAILABLE', '状态查询暂时不可用');
        const snapshot = items.map(item => ({ ...item }));
        if (state.holdStatus) await state.holdStatus.promise;
        return fulfill({ ...availability, items: snapshot });
      }
      if (endpoint.endsWith('/invite-sent')) {
        if (state.holdConfirm) await state.holdConfirm.promise;
        items.forEach(item => { item.allowedActions = ['START', 'POLL']; item.status = 'bound'; });
      } else {
        assert.equal(state.canStart, true);
        items.forEach(item => { item.allowedActions = ['POLL']; item.status = 'processing'; item.queuePosition = 2; item.stageLabel = '等待处理'; item.progressPercent = 10; });
        if (state.dropStart) { state.dropStart = false; return route.abort('failed'); }
      }
      return fulfill({ ...availability, items: items.map(item => ({ ...item })) });
    }
    throw new Error(`Unexpected API endpoint: ${endpoint}`);
  });
  await page.goto(`${base}/supprot_codex`, { waitUntil: 'networkidle' });
  return { page, state, context, cards: page.locator('section[aria-label="邀请邮箱"] > ul > li'),
    async refreshReads() {
      await page.evaluate(() => { window.fixtureHidden = true; document.dispatchEvent(new Event('visibilitychange')); });
      await page.evaluate(() => { window.fixtureHidden = false; document.dispatchEvent(new Event('visibilitychange')); });
    },
    async submit(value) { await page.getByLabel('输入 CDK').fill(value); await page.getByRole('button', { name: '获取邮箱', exact: true }).click(); },
    async finish() { assert.deepEqual(state.errors, []); assert.deepEqual(state.external, []); await context.close(); } };
}

async function run() {
  fs.mkdirSync(output, { recursive: true });
  const server = http.createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url, 'http://fixture').pathname);
    const candidate = path.resolve(dist, pathname.slice(1));
    const file = candidate.startsWith(`${dist}${path.sep}`) && fs.existsSync(candidate) && fs.statSync(candidate).isFile() ? candidate : path.join(dist, 'index.html');
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const results = [];
  const coreOnly = process.env.SMSINVIT_INVITATION_BROWSER_CORE === '1';
  const bannerOnly = process.env.SMSINVIT_INVITATION_BROWSER_BANNER === '1';
  const test = async (name, fn) => {
    if ((coreOnly || bannerOnly) && !(/cold refresh.*375px/.test(name) || name === 'dropped redemption response is recovered by GET without replay' || name === 'pending receipt uses only reads until the original result completes' || bannerOnly && name.startsWith('disabled executor'))) return;
    await fn(); results.push({ name, passed: true }); console.log(`PASS ${name}`);
  };
  try {
    for (const width of [375, 768, 1440]) await test(`layout, confirmation and cold refresh with explicit lookup at ${width}px`, async () => {
      const ui = await fixture(browser, base, width);
      assert.equal(await ui.page.getByRole('progressbar').count(), 0);
      await ui.submit('SYNTHETIC-A\nSYNTHETIC-B');
      await ui.cards.nth(1).waitFor();
      assert.equal(await ui.page.getByRole('progressbar').count(), 0);
      await ui.page.getByText('等待邀请信', { exact: true }).first().waitFor();
      await ui.page.getByRole('button', { name: '我已发送邀请信' }).first().click();
      await ui.page.getByRole('button', { name: '开始选中（1）', exact: true }).waitFor();
      await ui.page.getByText('尚未开始执行，请点击“开始”。', { exact: true }).waitFor();
      assert.equal(await ui.page.getByRole('progressbar').count(), 0);
      assert.equal(ui.state.requests.some(({ endpoint }) => endpoint.endsWith('/start')), false);
      await ui.page.getByRole('button', { name: '开始选中（1）', exact: true }).click();
      await ui.page.getByRole('progressbar').waitFor();
      assert.doesNotMatch(await ui.cards.first().innerText(), /等待处理|排队|执行中/);
      assert.equal(await ui.page.getByRole('progressbar').count(), 1);
      await ui.page.getByRole('button', { name: /复制 synthetic-0/ }).click();
      assert.deepEqual(await ui.page.evaluate(() => window.fixtureCopies), ['synthetic-0@example.test']);
      const before = await ui.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), KEY);
      assert.doesNotMatch(JSON.stringify(before), /SYNTHETIC-|example.test|allowedActions|progressPercent/);
      await ui.page.evaluate(() => { window.fixtureHidden = true; document.dispatchEvent(new Event('visibilitychange')); });
      const requestsBefore = ui.state.requests.length;
      await ui.page.reload({ waitUntil: 'networkidle' });
      assert.equal(await ui.cards.count(), 0);
      assert.equal(await ui.page.getByRole('progressbar').count(), 0);
      assert.equal(await ui.page.getByLabel('输入 CDK').inputValue(), '');
      assert.ok(ui.state.requests.slice(requestsBefore).every(({ endpoint }) => endpoint.endsWith('/page-content')));
      await ui.refreshReads();
      await ui.page.waitForTimeout(100);
      assert.ok(ui.state.requests.slice(requestsBefore).every(({ endpoint }) => endpoint.endsWith('/page-content')));
      const after = await ui.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), KEY);
      assert.equal(after.sessions[0].expiresAt, before.sessions[0].expiresAt);
      await ui.submit('SYNTHETIC-B');
      await ui.cards.waitFor();
      assert.equal(await ui.cards.count(), 1);
      assert.match(await ui.cards.innerText(), /synthetic-1@example.test/);
      assert.doesNotMatch(await ui.page.locator('body').innerText(), /synthetic-0@example.test/);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 2);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/start')).length, 1);
      const fit = await ui.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
      assert.equal(fit, true);
      await ui.page.screenshot({ path: path.join(output, `invitation-recovery-${width}.png`), fullPage: true });
      await ui.finish();
    });
    await test('ten accepted, eleven rejected without HTTP or loss of old rows', async () => {
      const ui = await fixture(browser, base);
      await ui.submit(Array.from({ length: 11 }, (_, i) => `SYNTHETIC-${i}`).join('\n'));
      await ui.page.getByRole('alert').filter({ hasText: '输入错误，请核对后重新输入。' }).waitFor();
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 0);
      await ui.submit(Array.from({ length: 10 }, (_, i) => `SYNTHETIC-${i}`).join('\n'));
      await ui.cards.nth(9).waitFor();
      assert.equal(await ui.page.getByRole('progressbar').count(), 0);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).at(-1).payload.codes.length, 10);
      await ui.finish();
    });
    await test('deduplication and atomic rejection preserve original line numbers and prior tasks', async () => {
      const ui = await fixture(browser, base);
      await ui.submit('SYNTHETIC-A'); await ui.cards.waitFor();
      await ui.submit('synthetic-b\n\n SYNTHETIC-B\n\nINVALID');
      await ui.page.getByText('第 5 行：', { exact: true }).waitFor();
      assert.equal(await ui.cards.count(), 1);
      assert.deepEqual(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).at(-1).payload.codes, ['SYNTHETIC-B', 'INVALID']);
      await ui.submit('SYNTHETIC-C');
      await ui.cards.nth(1).waitFor();
      await ui.finish();
    });
    await test('dropped redemption response is recovered by GET without replay', async () => {
      const ui = await fixture(browser, base, 375, { state: { dropRedeem: true } });
      await ui.submit('SYNTHETIC-LOST');
      await ui.cards.waitFor();
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 1);
      assert.ok(ui.state.requests.some(({ endpoint, method }) => endpoint.includes('/redemption-attempts/') && method === 'GET'));
      await ui.finish();
    });
    await test('pending receipt uses only reads until the original result completes', async () => {
      const ui = await fixture(browser, base, 375, { state: { dropRedeem: true, pendingCycles: 1 } });
      await ui.submit('SYNTHETIC-PENDING');
      await ui.page.getByText(/正在确认领取结果/).waitFor();
      await ui.cards.waitFor({ timeout: 6000 });
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 1);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.includes('/redemption-attempts/')).length, 2);
      await ui.finish();
    });
    for (const mode of ['review', 'missing', 'network']) await test(`${mode} receipt retains recovery data and never replays redemption`, async () => {
      const ui = await fixture(browser, base, 375, { state: { dropRedeem: true, receiptMode: mode } });
      await ui.submit(`SYNTHETIC-${mode.toUpperCase()}`);
      await ui.page.getByText(mode === 'network' ? /暂时无法确认领取结果/ : /领取结果.*待确认|领取结果仍无法确认/).first().waitFor();
      assert.equal((await ui.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), KEY)).attempts.length, 1);
      await ui.page.waitForTimeout(2500);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 1);
      if (mode !== 'network') assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.includes('/redemption-attempts/')).length, 1);
      await ui.finish();
    });
    await test('disabled executor shows no false progress; enabling still requires an explicit start', async () => {
      const ui = await fixture(browser, base, 375, { state: { canStart: false } });
      assert.equal(await ui.cards.count(), 0);
      assert.equal(await ui.page.getByText('请稍候重试。', { exact: true }).count(), 0);
      await ui.submit('SYNTHETIC-OFF');
      await ui.page.getByText('请稍候重试。', { exact: true }).waitFor();
      await ui.page.getByRole('button', { name: '我已发送邀请信' }).click();
      await ui.page.getByText('请稍候重试。', { exact: true }).nth(1).waitFor();
      assert.equal(await ui.page.getByRole('progressbar').count(), 0);
      assert.equal(await ui.page.getByRole('button', { name: '开始选中（1）', exact: true }).count(), 0);
      assert.equal(await ui.page.getByRole('button', { name: '开始选中', exact: true }).isDisabled(), true);
      await ui.page.getByRole('button', { name: /复制 synthetic-0/ }).click();
      await ui.refreshReads();
      await ui.page.waitForTimeout(100);
      assert.ok(ui.state.requests.some(({ endpoint }) => endpoint.endsWith('/status')));
      assert.equal(ui.state.requests.some(({ endpoint }) => endpoint.endsWith('/start')), false);
      ui.state.canStart = true;
      await ui.refreshReads();
      await ui.page.getByText('尚未开始执行，请点击“开始”。', { exact: true }).waitFor();
      assert.equal(await ui.page.getByRole('progressbar').count(), 0);
      assert.equal(ui.state.requests.some(({ endpoint }) => endpoint.endsWith('/start')), false);
      await ui.page.getByRole('button', { name: '开始选中（1）', exact: true }).click();
      await ui.page.getByRole('progressbar').waitFor();
      assert.equal(await ui.page.getByRole('progressbar').count(), 1);
      assert.doesNotMatch(await ui.cards.first().innerText(), /等待处理|排队|执行中|正在接受邀请/);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/start')).length, 1);
      await ui.finish();
    });
    await test('initial metadata failure can be retried without allocation', async () => {
      const ui = await fixture(browser, base, 375, { state: { metadataFailures: 1 } });
      await ui.page.getByRole('alert').waitFor();
      assert.equal(ui.state.requests.some(({ method }) => method !== 'GET'), false);
      await ui.submit('SYNTHETIC-RECHECK'); await ui.cards.waitFor();
      assert.ok(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/page-content')).length >= 2);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 1);
      assert.equal(await ui.page.getByRole('button', { name: '重新检查服务', exact: true }).count(), 0);
      assert.equal(await ui.page.getByRole('button', { name: '查询已有结果', exact: true }).count(), 0);
      await ui.finish();
    });
    await test('explicit redemption 429 clears the uncreated receipt and permits retry after cooldown', async () => {
      const ui = await fixture(browser, base, 375, { state: { redeemRateFailures: 1 } });
      await ui.submit('SYNTHETIC-RATE-RETRY');
      await ui.page.getByRole('alert').filter({ hasText: /操作较频繁，请等待/ }).waitFor();
      const button = ui.page.getByRole('button', { name: '获取邮箱', exact: true });
      assert.equal(await button.isDisabled(), true);
      assert.equal((await ui.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), KEY))?.attempts.length || 0, 0);
      assert.equal(await ui.page.getByLabel('输入 CDK').inputValue(), 'SYNTHETIC-RATE-RETRY');
      assert.equal(ui.state.requests.some(({ endpoint }) => endpoint.includes('/redemption-attempts/')), false);
      await ui.page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === '获取邮箱' && !button.disabled));
      await button.click(); await ui.cards.waitFor();
      const redeems = ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem'));
      assert.equal(redeems.length, 2);
      assert.ok(redeems[1].at - redeems[0].at >= 1900, 'Retry-After cooldown must be respected');
      assert.notEqual(redeems[1].payload.attemptId, redeems[0].payload.attemptId);
      assert.equal(ui.state.requests.some(({ endpoint }) => endpoint.includes('/redemption-attempts/')), false);
      await ui.finish();
    });
    await test('blocked tab storage stops allocation before POST', async () => {
      const ui = await fixture(browser, base, 375, { blockStorage: true });
      await ui.submit('SYNTHETIC-BLOCKED');
      await ui.page.getByText(/请允许网站保存数据/).waitFor();
      assert.equal(ui.state.requests.some(({ endpoint }) => endpoint.endsWith('/redeem')), false);
      await ui.finish();
    });
    await test('confirmation lock rejects repeated clicks and supersedes an old status snapshot', async () => {
      const status = deferred(), confirm = deferred();
      const ui = await fixture(browser, base, 375, { state: { holdStatus: status, holdConfirm: confirm } });
      await ui.submit('SYNTHETIC-RACE'); await ui.cards.waitFor();
      await ui.refreshReads();
      await ui.page.waitForTimeout(100);
      await ui.page.getByRole('button', { name: '我已发送邀请信' }).evaluate(button => { button.click(); button.click(); });
      await ui.page.waitForTimeout(100);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/invite-sent')).length, 1);
      confirm.resolve();
      await ui.page.getByRole('button', { name: '开始选中（1）', exact: true }).waitFor();
      status.resolve();
      await ui.page.waitForTimeout(100);
      assert.equal(await ui.page.getByRole('button', { name: '开始选中（1）', exact: true }).isDisabled(), false);
      await ui.finish();
    });
    await test('fifty-one merged tasks query and start in ten-item chunks', async () => {
      const ui = await fixture(browser, base);
      // 单次上限已经降到 10 条，所以分六批（5×10 + 1）攒够 51 条。
      for (let offset = 0; offset < 50; offset += 10) {
        await ui.submit(Array.from({ length: 10 }, (_, i) => `SYNTHETIC-CHUNK-${offset + i}`).join('\n'));
        await ui.cards.nth(offset + 9).waitFor();
      }
      await ui.submit('SYNTHETIC-CHUNK-50');
      await ui.cards.nth(50).waitFor();
      ui.state.sessions.forEach(item => { item.allowedActions = ['START', 'POLL']; });
      const before = ui.state.requests.length;
      await ui.refreshReads();
      await ui.page.waitForFunction(() => !document.querySelector('[aria-label="选择 synthetic-50@example.test"]').disabled);
      const chunked = (endpoint, total) =>
        ui.state.requests.slice(before).filter(({ endpoint: e }) => e.endsWith(endpoint))
          .flatMap(({ payload }) => payload.sessions.length)
          .reduce((acc, n) => { const last = acc.length - 1; if (last >= 0 && acc[last] + n <= 10) acc[last] += n; else acc.push(n); return acc; }, [])
          .every(chunk => chunk <= 10) &&
        ui.state.requests.slice(before).filter(({ endpoint: e }) => e.endsWith(endpoint))
          .every(({ payload }) => payload.sessions.length <= 10) &&
        ui.state.requests.slice(before).filter(({ endpoint: e }) => e.endsWith(endpoint))
          .flatMap(({ payload }) => payload.sessions.length).reduce((a, b) => a + b, 0) >= total;
      assert.ok(chunked('/status', 51), 'status must be split into batches of at most ten');
      await ui.page.getByLabel(/^全选可开始的/).check();
      await ui.page.getByRole('button', { name: '开始选中（51）', exact: true }).click();
      await ui.page.waitForFunction(() => document.querySelectorAll('[role="progressbar"]').length === 51);
      const starts = ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/start'));
      assert.ok(starts.every(({ payload }) => payload.sessions.length <= 10), 'start batches must be at most ten');
      assert.equal(starts.flatMap(({ payload }) => payload.sessions.length).reduce((a, b) => a + b, 0), 51);
      await ui.finish();
    });
    await test('dropped start response is reconciled by reads without duplicate start', async () => {
      const ui = await fixture(browser, base, 375, { state: { dropStart: true } });
      await ui.submit('SYNTHETIC-START-LOST');
      await ui.page.getByRole('button', { name: '我已发送邀请信' }).click();
      await ui.page.getByRole('button', { name: '开始选中（1）', exact: true }).click();
      await ui.page.getByRole('progressbar').waitFor();
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/start')).length, 1);
      const before = ui.state.requests.length;
      await ui.page.reload({ waitUntil: 'networkidle' });
      assert.equal(await ui.cards.count(), 0);
      assert.equal(await ui.page.getByLabel('输入 CDK').inputValue(), '');
      assert.ok(ui.state.requests.slice(before).every(({ endpoint }) => endpoint.endsWith('/page-content')));
      await ui.submit('SYNTHETIC-START-LOST');
      await ui.page.getByRole('progressbar').waitFor();
      assert.equal(await ui.cards.count(), 1);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/start')).length, 1);
      await ui.finish();
    });
    await test('used CDK returns the original mailbox and current progress without technical text or automatic start', async () => {
      const ui = await fixture(browser, base);
      await ui.submit('SYNTHETIC-SAME-CDK'); await ui.cards.waitFor();
      const initial = await ui.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)).sessions[0], KEY);
      const current = ui.state.sessions.get(idFor(0));
      // 服务端公开状态只会是 processing：这里同时注入内部阶段与队列字段，验证页面既不显示也不据此涨进度。
      current.status = 'processing'; current.allowedActions = ['POLL']; current.progressPercent = 70;
      current.stageLabel = '正在接受邀请'; current.queuePosition = 4; ui.state.canRedeem = false; ui.state.canStart = false;
      await ui.submit('SYNTHETIC-SAME-CDK');
      await ui.page.waitForFunction(() => document.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow') === '70');
      assert.equal(await ui.cards.count(), 1);
      assert.doesNotMatch(await ui.cards.first().innerText(), /正在接受邀请|排队|等待处理/);
      assert.deepEqual(await ui.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)).sessions[0], KEY), initial);
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 2);
      assert.equal(ui.state.requests.some(({ endpoint }) => endpoint.endsWith('/start')), false);
      assert.doesNotMatch(await ui.page.locator('body').innerText(), /重新检查服务|查询已有结果|执行器|worker|CLI|凭据|会话|恢复|并发|最多同时处理|管理员/);
      await ui.finish();
    });
    await test('two hundred saved mailboxes retain the overflow receipt and permit used-CDK lookup before and after refresh', async () => {
      const createdAt = Date.now(), expiresAt = createdAt + TTL;
      const sessions = new Map(Array.from({ length: 200 }, (_, index) => [idFor(index), row(index)]));
      const codes = new Map(Array.from({ length: 200 }, (_, index) => [`SYNTHETIC-OLD-${index}`, idFor(index)]));
      const records = Array.from({ length: 200 }, (_, index) => ({ id: idFor(index), sessionSecret: secretFor(index), line: index + 1, createdAt, expiresAt }));
      const ui = await fixture(browser, base, 375, { stored: JSON.stringify({ version: 1, sessions: records, attempts: [] }), state: { sessions, codes, next: 200 } });
      assert.equal(await ui.cards.count(), 0);
      assert.ok(ui.state.requests.every(({ endpoint }) => endpoint.endsWith('/page-content')));
      await ui.submit('SYNTHETIC-OLD-0'); await ui.cards.waitFor();
      assert.equal(await ui.cards.count(), 1);
      await ui.submit('SYNTHETIC-NEW-200'); await ui.cards.nth(1).waitFor();
      let stored = await ui.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), KEY);
      assert.equal(stored.sessions.length, 200); assert.equal(stored.attempts.length, 1);
      assert.deepEqual(stored.attempts[0].restoreIds, [idFor(200)]);
      await ui.submit('SYNTHETIC-OLD-0');
      await ui.page.waitForFunction(() => { const button = [...document.querySelectorAll('button')].find(button => button.textContent === '获取邮箱'); return Boolean(button && !button.disabled); });
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 3);
      assert.equal(await ui.cards.count(), 2);
      const before = ui.state.requests.length;
      await ui.page.reload({ waitUntil: 'networkidle' });
      assert.equal(await ui.cards.count(), 0);
      assert.equal(await ui.page.getByLabel('输入 CDK').inputValue(), '');
      assert.ok(ui.state.requests.slice(before).every(({ endpoint }) => endpoint.endsWith('/page-content')));
      await ui.submit('SYNTHETIC-OLD-0');
      await ui.page.waitForFunction(() => { const button = [...document.querySelectorAll('button')].find(button => button.textContent === '获取邮箱'); return Boolean(button && !button.disabled); });
      assert.equal(ui.state.requests.filter(({ endpoint }) => endpoint.endsWith('/redeem')).length, 4);
      assert.equal(await ui.cards.count(), 1);
      assert.doesNotMatch(await ui.page.locator('body').innerText(), /synthetic-200@example.test/);
      assert.equal(ui.state.requests.slice(before).some(({ endpoint }) => endpoint.includes('/redemption-attempts/')), false);
      stored = await ui.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), KEY);
      assert.deepEqual(stored.sessions, records); assert.equal(stored.attempts.length, 1);
      assert.equal(ui.state.requests.some(({ endpoint }) => endpoint.endsWith('/start')), false);
      await ui.finish();
    });
    await test('expired or corrupted records trigger no recovery or write requests', async () => {
      for (const stored of ['{bad', JSON.stringify({ version: 1, sessions: [{ id: idFor(0), sessionSecret: secretFor(0), line: 1, createdAt: Date.now() - TTL * 2, expiresAt: Date.now() - TTL }], attempts: [] })]) {
        const ui = await fixture(browser, base, 375, { stored });
        assert.equal(await ui.page.getByRole('progressbar').count(), 0);
        assert.ok(ui.state.requests.every(({ endpoint }) => endpoint.endsWith('/page-content')));
        assert.equal(await ui.page.evaluate(key => sessionStorage.getItem(key), KEY), null);
        await ui.finish();
      }
    });
    if (!coreOnly && !bannerOnly) await runPublicPages({ browser, base, output, test });
    fs.writeFileSync(resultPath, JSON.stringify({ testOnly: true, productionBundle: true, interceptedApi: true, externalNetworkRequests: 0, cases: results.length, results }, null, 2) + '\n');
    console.log(JSON.stringify({ passed: true, cases: results.length, results: resultPath }));
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
}
run().catch(error => { console.error(error); process.exitCode = 1; });

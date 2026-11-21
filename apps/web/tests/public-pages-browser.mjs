import assert from 'node:assert/strict';
import path from 'node:path';

const SECRET = 'b'.repeat(64);
const expiresAt = () => new Date(Date.now() + 600_000).toISOString();
const smsSession = (extra = {}) => ({ id: 'synthetic-sms', sessionSecret: SECRET, status: 'WAITING_CODE', keyStatus: 'ACTIVE', phone: '+12025550123', attempt: 1, attemptCount: 1, maxAttempts: 3, maxCodes: 3, smsCodes: [], canRetry: true, canRequestNextSms: false, smsCountryName: '美国', smsServiceName: 'OpenAI', cancelGraceSeconds: 30, ...extra });

async function fixture(browser, base, width, options = {}) {
  const context = await browser.newContext({ viewport: { width, height: 950 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  const state = { sms: smsSession(), job: null, requests: [], external: [], errors: [], ...options };
  page.on('pageerror', e => state.errors.push(e.message));
  await page.addInitScript(() => {
    window.fixtureCopies = [];
    window.fixtureHidden = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.fixtureHidden });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => window.fixtureHidden ? 'hidden' : 'visible' });
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => window.fixtureCopies.push(text) } });
  });
  await context.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== base) { state.external.push(url.origin); return route.abort('blockedbyclient'); }
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const body = request.postData() ? request.postDataJSON() : null;
    state.requests.push({ path: url.pathname, method: request.method(), body, at: Date.now() });
    const send = data => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, data }) });
    if (url.pathname.endsWith('/page-content')) return send({ available: true, paused: false, pollIntervalMs: 2000, intro: '粘贴接码 CDK，获取手机号与验证码。', notices: [] });
    if (url.pathname === '/api/v1/sms/redeem') return send(state.sms);
    if (url.pathname === '/api/v1/sms/sessions/synthetic-sms/retry') { state.sms = smsSession({ phone: '+12025550124', attempt: 2, attemptCount: 2 }); return send(state.sms); }
    if (url.pathname === '/api/v1/sms/sessions/synthetic-sms/next-sms') { state.sms = { ...state.sms, listeningNextSms: true, canRequestNextSms: false, nextListenUntil: expiresAt() }; return send(state.sms); }
    if (url.pathname === '/api/v1/sms/sessions/synthetic-sms') return send(state.sms);
    throw new Error(`Unmocked request ${url.pathname}`);
  });
  await page.goto(`${base}/supprot_sim`, { waitUntil: 'networkidle' });
  return { page, state, context, async finish() { assert.deepEqual(state.external, []); assert.deepEqual(state.errors, []); await context.close(); } };
}

async function snapshot(ui, output, name) {
  assert.equal(await ui.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `horizontal overflow: ${name}`);
  assert.equal(await ui.page.evaluate(() => getComputedStyle(document.body).backgroundColor), 'rgb(8, 12, 18)');
  await ui.page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true });
}

export async function runPublicPages({ browser, base, output, test }) {
  await test('公开页导航只保留接码兑换与 Codex 邀请两个入口', async () => {
    const ui = await fixture(browser, base, 375);
    const nav = ui.page.getByRole('navigation', { name: '兑换服务' }).getByRole('link');
    assert.deepEqual(await nav.allInnerTexts(), ['接码兑换', 'Codex 邀请']);
    assert.equal(await nav.first().getAttribute('aria-current'), 'page');
    assert.equal(await nav.nth(1).getAttribute('aria-current'), null);
    assert.equal(await ui.page.getByRole('heading', { level: 1 }).innerText(), '接码兑换');
    await ui.finish();
  });
  await test('SMS hidden tab pauses reads and visible resume immediately shows the latest code', async () => {
    const ui = await fixture(browser, base, 375);
    await ui.page.getByLabel('输入 CDK').fill('SYNTHETIC-SMS');
    await ui.page.getByRole('button', { name: '开始接码', exact: true }).click();
    await ui.page.getByRole('button', { name: '获取验证码', exact: true }).waitFor();
    await ui.page.getByRole('button', { name: '获取验证码', exact: true }).click();
    await ui.page.evaluate(() => { window.fixtureHidden = true; document.dispatchEvent(new Event('visibilitychange')); });
    const before = ui.state.requests.filter(r => r.path === '/api/v1/sms/sessions/synthetic-sms').length;
    ui.state.sms = { ...ui.state.sms, status: 'SUCCESS', keyStatus: 'USED', smsCodes: ['654321'], codeCount: 1 };
    await ui.page.waitForTimeout(1300);
    assert.equal(ui.state.requests.filter(r => r.path === '/api/v1/sms/sessions/synthetic-sms').length, before);
    assert.equal(await ui.page.getByText('654321', { exact: true }).count(), 0);
    const resumedAt = Date.now();
    await ui.page.evaluate(() => { window.fixtureHidden = false; document.dispatchEvent(new Event('visibilitychange')); });
    await ui.page.getByText('654321', { exact: true }).waitFor({ timeout: 1200 });
    assert.ok(Date.now() - resumedAt < 1200);
    assert.equal(ui.state.requests.filter(r => r.path.endsWith('/redeem')).length, 1);
    await ui.finish();
  });
  for (const width of [375, 768, 1440]) {
    await test(`SMS number, code copy, next code and retry controls at ${width}px`, async () => {
      const ui = await fixture(browser, base, width);
      await snapshot(ui, output, `sms-initial-${width}`);
      await ui.page.getByLabel('输入 CDK').fill('SYNTHETIC-SMS');
      await ui.page.getByRole('button', { name: '开始接码', exact: true }).click();
      await ui.page.getByRole('button', { name: '获取验证码', exact: true }).waitFor();
      assert.equal(await ui.page.getByRole('button', { name: /换号/ }).count(), 0);
      await ui.page.getByRole('button', { name: '复制', exact: true }).click();
      await ui.page.getByRole('menuitem', { name: /带国家号/ }).click();
      assert.deepEqual(await ui.page.evaluate(() => window.fixtureCopies), ['+12025550123']);
      await snapshot(ui, output, `sms-number-${width}`);
      await ui.page.getByRole('button', { name: '获取验证码', exact: true }).click();
      assert.equal(await ui.page.getByRole('button', { name: /换号/ }).count(), 0);
      ui.state.sms = { ...ui.state.sms, status: 'SUCCESS', keyStatus: 'USED', smsCodes: ['123456'], codeCount: 1, canRequestNextSms: true };
      await ui.page.getByText('123456', { exact: true }).waitFor();
      assert.equal(await ui.page.getByRole('button', { name: /换号/ }).count(), 0);
      await snapshot(ui, output, `sms-code-${width}`);
      await ui.page.getByRole('button', { name: '获取新短信（1/3）' }).click();
      ui.state.sms = { ...ui.state.sms, smsCodes: ['123456', '234567', '345678'], codeCount: 3, canRequestNextSms: false, listeningNextSms: false, providerFinalized: true };
      await ui.page.getByText('345678', { exact: true }).waitFor();
      assert.equal(await ui.page.getByRole('button', { name: /获取新短信/ }).count(), 0);
      assert.equal(ui.state.requests.filter(r => r.path.endsWith('/next-sms')).length, 1);
      await ui.finish();
    });
  }
  await test('SMS invalid number allows replacement and keeps received numbers immutable', async () => {
    const ui = await fixture(browser, base, 375, { sms: smsSession({ numberInvalid: true }) });
    await ui.page.getByLabel('输入 CDK').fill('SYNTHETIC-SMS'); await ui.page.getByRole('button', { name: '开始接码', exact: true }).click();
    await ui.page.getByRole('button', { name: /换号/ }).click();
    await ui.page.getByRole('button', { name: '获取验证码', exact: true }).waitFor();
    assert.equal(ui.state.requests.filter(r => r.path.endsWith('/retry')).length, 1);
    assert.equal(await ui.page.getByRole('button', { name: /换号/ }).count(), 0);
    await ui.finish();
  });
}

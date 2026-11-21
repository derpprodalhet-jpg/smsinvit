/* Local browser regression; every API response is synthetic and external requests are blocked. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.SMSINVIT_PLAYWRIGHT_PATH || 'playwright');
const dist = path.resolve('apps/web/dist');
const output = path.resolve('docs/validation/2026-10-18-sms-local');
fs.mkdirSync(output, { recursive: true });
const server = http.createServer((req, res) => {
  const asset = path.resolve(dist, '.' + new URL(req.url, 'http://localhost').pathname);
  if (asset.startsWith(dist + '/assets/') && fs.existsSync(asset)) {
    res.setHeader('Content-Type', asset.endsWith('.css') ? 'text/css' : 'application/javascript');
    res.end(fs.readFileSync(asset));
  } else {
    res.setHeader('Content-Type', 'text/html');
    res.end(fs.readFileSync(path.join(dist, 'index.html')));
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
const results = [];
try {
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  for (const width of [375, 1440]) {
    const context = await browser.newContext({ viewport: { width, height: 950 }, timezoneId: 'Asia/Shanghai' });
    await context.addInitScript(() => {
      const originalNow = Date.now;
      window.smsTestOffset = 0;
      Date.now = () => originalNow() + window.smsTestOffset;
      const originalTimeout = window.setTimeout;
      window.setTimeout = (fn, delay, ...args) => originalTimeout(fn, delay === 15000 ? 100 : delay, ...args);
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let queries = 0, unavailable = false, hang = false;
    const deadline = new Date(Date.now() + 600000).toISOString();
    let session = { id: 'original-number', sessionSecret: 'synthetic-proof', phone: '+12025550123',
      status: 'WAITING_CODE', keyStatus: 'LOCKED', attempt: 1, attemptCount: 1,
      maxAttempts: 3, canRetry: false, cancelGraceSeconds: 300,
      numberExpiresAt: deadline, cancelEligibleAt: new Date(Date.now() + 300000).toISOString() };
    await context.route('**/*', route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== base) { errors.push('external request'); return route.abort(); }
      if (!url.pathname.startsWith('/api/')) return route.continue();
      const send = data => route.fulfill({ status: 200, contentType: 'application/json',
        headers: { 'set-cookie': 'lc_csrf=fixture; Path=/; SameSite=Lax' }, body: JSON.stringify({ success: true, data }) });
      if (url.pathname.endsWith('/page-content')) return send({ intro: '本地接码测试', notices: [] });
      if (url.pathname.endsWith('/redeem')) return send(session);
      if (req.method() === 'GET' && url.pathname.includes('/sessions/')) {
        queries++;
        if (hang) return;
        return send({ ...session, statusQueryUnavailable: unavailable });
      }
      errors.push(`unexpected mutation: ${url.pathname}`);
      return route.abort();
    });
    await page.goto(base + '/supprot_sim');
    assert.equal(await page.getByRole('link', { name: /X Premium/ }).count(), 0);
    await page.getByLabel('输入 CDK').fill('synthetic-cdk');
    await page.getByRole('button', { name: '开始接码', exact: true }).click();
    await page.getByRole('button', { name: '获取验证码', exact: true }).click();
    await page.evaluate(() => { window.smsTestOffset = 310000; });
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b => /监听中（2\d\ds）/.test(b.textContent)));
    assert.equal(await page.getByRole('button', { name: /^换号/ }).count(), 0);
    const displayed = await page.getByText(/^号码等待截止：/).textContent();
    const expectedHour = new Date(deadline).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }).split(' ')[1].split(':')[0];
    assert.ok(displayed.includes(expectedHour + ':'), displayed);
    unavailable = true;
    await page.getByRole('status').filter({ hasText: '验证码查询暂时失败' }).waitFor();
    await page.screenshot({ path: path.join(output, `waiting-${width}.png`), fullPage: true });
    // A hung read must release the poll controller, report the error and allow a later read.
    unavailable = false; hang = true;
    const before = queries;
    await page.waitForFunction(() => document.querySelector('[role="status"]')?.textContent.includes('查询暂时失败'));
    await new Promise(resolve => setTimeout(resolve, 3300));
    assert.ok(queries >= before + 2, 'a hung read must time out and allow another query');
    hang = false;
    session = { ...session, status: 'SUCCESS', keyStatus: 'USED', smsCode: '012345', smsCodes: ['012345'] };
    await page.getByText('012345', { exact: true }).waitFor();
    assert.equal(await page.getByRole('status').filter({ hasText: '验证码查询暂时失败' }).count(), 0);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(output, `received-${width}.png`), fullPage: true });
    results.push({ width, lateCodeDisplayed: true, queryFailureVisible: true, timedOutReadRecovered: true, noXNavigation: true, timezone: 'Asia/Shanghai', queries });
    await context.close();
  }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(results, null, 2) + '\n');
  console.log(JSON.stringify(results));
} finally {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
}

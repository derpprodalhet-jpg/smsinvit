/* Real browser interaction with synthetic SMS responses; never calls a supplier. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.SMSINVIT_PLAYWRIGHT_PATH || 'playwright');
const dist = path.resolve('apps/web/dist');
const output = path.resolve('docs/validation/2026-10-14-sms-retry');
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
try {
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const results = [];
  for (const width of [375, 1440]) {
    const context = await browser.newContext({ viewport: { width, height: 950 } });
    await context.addInitScript(() => {
      const original = Date.now;
      window.smsTestOffset = 0;
      Date.now = () => original() + window.smsTestOffset;
    });
    const page = await context.newPage();
    const errors = []; let retries = 0;
    page.on('pageerror', e => errors.push(e.message));
    let session = { id: 'original', sessionSecret: 'fixture-secret', phone: '+12025550123',
      status: 'WAITING_CODE', keyStatus: 'LOCKED', attempt: 1, attemptCount: 1,
      maxAttempts: 3, canRetry: true, cancelGraceSeconds: 30, smsServiceName: 'GPT', smsCountryName: '美国' };
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== base) { errors.push('unexpected external request'); return route.abort(); }
      if (!url.pathname.startsWith('/api/')) return route.continue();
      const send = data => route.fulfill({ status: 200, contentType: 'application/json',
        headers: { 'set-cookie': 'lc_csrf=fixture; Path=/; SameSite=Lax' }, body: JSON.stringify({ success: true, data }) });
      if (url.pathname.endsWith('/page-content')) return send({ intro: '接码换号回归测试', notices: [] });
      if (url.pathname.endsWith('/redeem')) return send(session);
      if (url.pathname.endsWith('/retry')) {
        assert.equal(req.method(), 'POST'); retries++;
        if (retries === 2) session = { ...session, id: 'new-number', phone: '+13105550123', attempt: 2, attemptCount: 2, canRetry: false };
        return send(session);
      }
      if (req.method() === 'GET' && url.pathname.includes('/sessions/')) return send(session);
      errors.push(`unexpected API ${url.pathname}`); return send({});
    });
    await page.goto(base + '/supprot_sim');
    await page.getByLabel('输入 CDK').fill('synthetic-card');
    await page.getByRole('button', { name: '开始接码', exact: true }).click();
    await page.getByRole('button', { name: '获取验证码', exact: true }).click();
    await page.evaluate(() => { window.smsTestOffset = 31000; });
    const retry = page.getByRole('button', { name: '换号（1/3）', exact: true });
    await retry.waitFor();
    await retry.click();
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b => b.textContent.includes('换号（1/3）') && !b.disabled));
    assert.equal(await page.getByRole('button', { name: '获取验证码', exact: true }).count(), 0);
    assert.equal(retries, 1);
    await page.screenshot({ path: path.join(output, `unchanged-${width}.png`), fullPage: true });
    await retry.click();
    await page.getByRole('button', { name: '获取验证码', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: /^换号/ }).count(), 0);
    assert.equal(retries, 2);
    assert.deepEqual(errors, []);
    results.push({ width, originalNumberKeepsRetry: true, newNumberResetsFetch: true, explicitRetryRequests: retries });
    await context.close();
  }
  fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({ mockOnly: true, results }, null, 2));
  console.log(JSON.stringify(results));
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }

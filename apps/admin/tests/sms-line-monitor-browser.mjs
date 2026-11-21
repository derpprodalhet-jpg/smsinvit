/* Actual loopback preview + isolated database. No supplier calls or deployment. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.SMSINVIT_PLAYWRIGHT_PATH || 'playwright');
const base = 'http://127.0.0.1:3010';
const output = path.resolve('docs/validation/2026-10-18-line-monitor');
fs.mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const results = [];
try {
  for (const width of [375, 1440]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await page.goto(base + '/admin/');
    await page.getByLabel('用户名或邮箱', { exact: true }).fill('preview');
    await page.getByLabel('密码', { exact: true }).fill('LocalPreview-2026!');
    await page.getByPlaceholder('计算结果').waitFor();
    await page.waitForFunction(() => /\d+\s*[+−-]\s*\d+\s*=\s*\?/.test(document.body.innerText));
    const match = (await page.locator('body').innerText()).match(/(\d+)\s*([+−-])\s*(\d+)\s*=\s*\?/);
    const answer = match[2] === '+' ? Number(match[1]) + Number(match[3]) : Number(match[1]) - Number(match[3]);
    await page.getByPlaceholder('计算结果').fill(String(answer));
    await page.getByRole('button', { name: /登\s*录/ }).click();
    await page.getByText('① 卡密台账（按模块）').waitFor().catch(async error => { console.error('Login state:', page.url(), await page.locator('body').innerText()); throw error; });
    const now = new Date().toISOString();
    const baseRow = { configured: true, checkedAt: now, quotaUpdatedAt: now };
    const snapshots = {
      primary: { providers: [
        { ...baseRow, provider: 'numlookupapi', state: 'available', remaining: 75, total: 100 },
        { ...baseRow, provider: 'abstractapi', state: 'not_checked', remaining: null, total: null },
      ], lastDecision: { type: 'LINE_TYPE_VERIFIED', provider: 'numlookupapi', reasons: {}, createdAt: now } },
      fallback: { providers: [
        { ...baseRow, provider: 'numlookupapi', state: 'quota_exhausted', remaining: 0, total: 100 },
        { ...baseRow, provider: 'abstractapi', state: 'available', remaining: null, total: null },
      ], lastDecision: { type: 'LINE_TYPE_VERIFIED', provider: 'abstractapi', reasons: { numlookupapi: 'quota_exhausted' }, createdAt: now } },
      skipped: { providers: [
        { ...baseRow, provider: 'numlookupapi', state: 'quota_exhausted', remaining: 0, total: 100 },
        { ...baseRow, provider: 'abstractapi', state: 'quota_exhausted', remaining: 0, total: null },
      ], lastDecision: { type: 'LINE_TYPE_SKIPPED', reasons: { numlookupapi: 'quota_exhausted', abstractapi: 'quota_exhausted' }, createdAt: now } },
    };
    let current = snapshots.primary; let requests = 0;
    await page.route('**/api/v1/admin/sms/line-check-status', route => {
      requests++;
      return route.fulfill({ json: { code: 'OK', data: { ...current, history: [current.lastDecision] } } });
    });
    for (const scenario of ['primary', 'fallback', 'skipped']) {
      console.log("scenario", width, scenario);
      current = snapshots[scenario];
      await page.goto(base + '/admin/sms/settings');
      await page.getByText('检测接口额度与状态', { exact: true }).waitFor();
      const card = page.locator('.ant-card').filter({ has: page.getByText('检测接口额度与状态', { exact: true }) }).last();
      if (scenario === 'primary') await card.getByText('75 次 / 总计 100 次', { exact: true }).waitFor();
      if (scenario === 'fallback') await card.getByText('最近结果：已切换备用接口，检测通过', { exact: true }).waitFor();
      if (scenario === 'skipped') await card.getByText('最近结果：已跳过检测，继续取号', { exact: true }).waitFor();
      assert.ok((await card.innerText()).includes(scenario === 'primary' ? '未知（接口未返回）' : '额度耗尽'));
      const before = requests;
      await page.getByRole('button', { name: '刷新检测状态', exact: true }).click();
      await page.getByRole('button', { name: '刷新检测状态', exact: true }).waitFor();
      assert.ok(requests > before);
      assert.deepEqual(errors, []);
      await page.screenshot({ path: path.join(output, `${scenario}-${width}.png`), fullPage: true });
      results.push({ width, scenario, refreshWorks: true, pageErrors: 0 });
    }
    await context.close();
  }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results));
} finally { await browser.close(); }

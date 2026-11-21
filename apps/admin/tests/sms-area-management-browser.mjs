/* Actual loopback preview + isolated database. No supplier calls or deployment. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.SMSINVIT_PLAYWRIGHT_PATH || 'playwright');
const base = 'http://127.0.0.1:3010';
const output = path.resolve('docs/validation/2026-10-18-sms-area-management');
fs.mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const results = [];
try {
  for (const width of [375, 1440]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    const page = await context.newPage();
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
    await page.goto(base + '/admin/sms/settings');
    await page.getByText('区号排除池', { exact: true }).waitFor();
    assert.equal(await page.getByText(/Twilio/).count(), 0);
    await page.getByLabel('numlookupapi API Key', { exact: true }).waitFor();
    await page.getByLabel('Abstract Phone Validation API Key', { exact: true }).waitFor();
    const current = await (await page.request.get(base + '/api/v1/admin/sms/excluded-areas')).json();
    const prefix = ['1999', '1998', '1997'].find(value => !current.data.some(row => row.prefix === value));
    assert.ok(prefix, 'a fresh test prefix must be available');
    const label = `+1 ${prefix.slice(1)}`;
    let mutations = 0;
    page.on('request', req => {
      if (req.url().includes('/sms/excluded-areas') && ['POST', 'DELETE'].includes(req.method())) mutations++;
    });
    const input = page.getByRole('textbox', { name: '添加排除区号' });
    const add = page.getByRole('button', { name: '添加区号', exact: true });
    const remove = () => page.getByRole('button', { name: `删除 ${label}`, exact: true });
    try {
      await input.fill('+18325550123');
      await add.click();
      await page.getByText('请输入三位区号或 +1 xxx，例如 +1 832；不要填写完整号码。', { exact: true }).waitFor();
      assert.equal(mutations, 0);
      await input.fill(label); await add.click(); await remove().waitFor();
      await input.fill(prefix.slice(1)); await add.click();
      await page.waitForFunction(() => document.querySelector('input[aria-label="添加排除区号"]')?.value === '');
      assert.equal(await remove().count(), 1);
      await page.reload(); await remove().waitFor();
      await page.screenshot({ path: path.join(output, `added-${width}.png`), fullPage: true });
      await remove().click();
      await page.getByRole('button', { name: /取\s*消/ }).click();
      assert.equal(await remove().count(), 1);
      await remove().click();
      await page.getByRole('button', { name: '确认删除', exact: true }).click();
      await remove().waitFor({ state: 'detached' });
      await page.reload();
      await page.getByRole('textbox', { name: '添加排除区号' }).waitFor();
      assert.equal(await remove().count(), 0);
      const stored = await (await page.request.get(base + '/api/v1/admin/sms/excluded-areas')).json();
      assert.ok(!stored.data.some(row => row.prefix === prefix));
      assert.deepEqual(errors, []);
      await page.screenshot({ path: path.join(output, `removed-${width}.png`), fullPage: true });
      results.push({ width, inputValidated: true, duplicateAddIdempotent: true, persistedAcrossRefresh: true,
        deleteCancelWorks: true, deletionPersists: true, pageErrors: 0 });
    } catch (error) {
      await page.screenshot({ path: path.join(output, `failure-${width}.png`), fullPage: true });
      console.error(await page.locator('body').innerText());
      console.error(await page.locator('input[aria-label="添加排除区号"]').inputValue());
      console.error(await page.locator('button').evaluateAll(elements => elements.filter(el => el.textContent.includes('添加区号')).map(el => el.outerHTML)));
      throw error;
    } finally {
      const cookie = (await context.cookies()).find(row => row.name === 'lc_csrf');
      await page.request.delete(base + '/api/v1/admin/sms/excluded-areas/' + prefix,
        { headers: { 'X-CSRF-Token': cookie?.value || '' } });
      await context.close();
    }
  }
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(results, null, 2) + '\n');
  console.log(JSON.stringify(results));
} finally { await browser.close(); }

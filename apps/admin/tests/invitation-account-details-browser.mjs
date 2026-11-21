/* Build the admin first. Every API response and every phone value below is synthetic. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.SMSINVIT_PLAYWRIGHT_PATH || 'playwright');
const admin = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dist = path.join(admin, 'dist');
const output = path.join(admin, 'test-results');
fs.mkdirSync(output, { recursive: true });
const fixtures = [
  { email: 'iv955blkan@ukgiffcard.xyz', phone_number: '+15550001001', phone_code: '012345' },
  { email: 'yk79r8dae8@ukgiffcard.xyz', phone_number: '+15550001002', phone_code: '987654' },
].map((row) => ({
  ...row,
  phone_bound_ok: 1,
  used_proxy: 1,
  proxy: 'http://synthetic-proxy.test:8080',
  kind: 'cf_temp',
  status: 'registered',
  imported_at: 1791849600,
  created_at: 1791849600,
  source: 'invite',
  at_len: 100,
  st_len: 100,
  rt_len: 100,
  codex_rt_len: 100,
}));
const contentTypes = { '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, 'http://127.0.0.1').pathname;
  const asset = pathname.startsWith('/admin-v1/assets/')
    ? path.resolve(dist, pathname.slice('/admin-v1/'.length))
    : null;
  if (asset && asset.startsWith(path.join(dist, 'assets') + path.sep) && fs.existsSync(asset)) {
    response.writeHead(200, { 'Content-Type': contentTypes[path.extname(asset)] || 'application/octet-stream' });
    response.end(fs.readFileSync(asset));
  } else {
    response.writeHead(200, { 'Content-Type': 'text/html' });
    response.end(fs.readFileSync(path.join(dist, 'index.html'), 'utf8').replace('<head>', '<head><base href="/admin-v1/" data-admin-base>'));
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const results = [];
  for (const width of [375, 1440]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    const page = await context.newPage();
    const failures = [];
    page.on('pageerror', (error) => failures.push(error.message));
    await context.addCookies([
      { name: 'lc_admin_session', value: '1', url: base },
      { name: 'lc_csrf', value: 'synthetic-csrf', url: base },
    ]);
    await context.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== base) {
        failures.push(`Unexpected external request: ${url.origin}`);
        return route.abort();
      }
      if (!url.pathname.startsWith('/api/')) return route.continue();
      const respond = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname === '/api/v1/admin/auth/me')
        return respond({ success: true, data: { id: 'fixture', username: 'fixture', email: 'admin@example.test', roles: ['SUPER_ADMIN'], totpEnabled: true, totpSetupRequired: false } });
      const nativePath = url.pathname.replace('/api/v1/admin/invitation/native/api', '');
      if (nativePath === '/mail/providers')
        return respond({ ok: true, current: 'cf_temp', providers: [{ kind: 'cf_temp', pooled: true, display_name: 'CF Worker 临时邮箱' }] });
      if (nativePath === '/accounts' || nativePath === '/registered')
        return respond({ ok: true, items: fixtures, total: fixtures.length });
      if (nativePath === '/proxy/pool') return respond({ ok: true, items: [] });
      if (nativePath === '/registered/export/formats') return respond({ ok: true, formats: [] });
      failures.push(`Unexpected API: ${url.pathname}`);
      return respond({ ok: true });
    });
    await page.goto(base + '/admin-v1/invitation/mail-accounts');
    for (const [index, row] of fixtures.entries()) {
      const tableRow = page.locator('.ant-table-tbody tr').filter({ hasText: row.email });
      await tableRow.getByText('已接', { exact: true }).hover();
      const tooltip = page.getByRole('tooltip');
      await tooltip.getByText(`手机号：${row.phone_number}`, { exact: true }).waitFor();
      await tooltip.getByText(`验证码：${row.phone_code}`, { exact: true }).waitFor();
      await page.waitForFunction(() => [...document.querySelectorAll('.ant-tooltip')].some((element) => getComputedStyle(element).opacity === '1' && getComputedStyle(element).visibility === 'visible'));
      if (index === 0)
        await page.screenshot({ path: path.join(output, `invitation-mail-phone-tooltip-${width}.png`) });
      await tableRow.getByRole('button', { name: '详情', exact: true }).click();
      const drawer = page.locator('.ant-drawer-content:visible');
      await drawer.getByText(`手机号：${row.phone_number}`, { exact: true }).waitFor();
      await drawer.getByText(`验证码：${row.phone_code}`, { exact: true }).waitFor();
      await drawer.getByText('已用代理', { exact: true }).waitFor();
      assert.doesNotMatch(await drawer.innerText(), /未接码|未记录/);
      if (index === 1)
        await page.screenshot({ path: path.join(output, `invitation-mail-phone-detail-${width}.png`) });
      await page.keyboard.press('Escape');
      await drawer.waitFor({ state: 'hidden' });
    }
    await page.goto(base + '/admin-v1/invitation/registered');
    for (const [index, row] of fixtures.entries()) {
      const tableRow = page.locator('.ant-table-tbody tr').filter({ hasText: row.email });
      await tableRow.getByRole('button', { name: '详情', exact: true }).click();
      const drawer = page.locator('.ant-drawer-content:visible');
      await drawer.getByText('已接码', { exact: true }).waitFor();
      await drawer.getByText(`手机号：${row.phone_number}`, { exact: true }).waitFor();
      await drawer.getByText(`验证码：${row.phone_code}`, { exact: true }).waitFor();
      const proxy = drawer.getByText('已用代理', { exact: true });
      await proxy.hover();
      await page.getByRole('tooltip').getByText(row.proxy, { exact: true }).waitFor();
      assert.doesNotMatch(await drawer.innerText(), /未接码|未记录/);
      await page.mouse.move(width - 5, 995);
      await page.getByRole('tooltip').waitFor({ state: 'hidden' });
      if (index === 1)
        await page.screenshot({ path: path.join(output, `invitation-registered-phone-detail-${width}.png`) });
      await page.keyboard.press('Escape');
      await drawer.waitFor({ state: 'hidden' });
    }
    assert.deepEqual(failures, []);
    results.push({ width, emailsChecked: fixtures.length, mailTooltip: true, mailDetails: true, registeredDetails: true, proxyConsistent: true });
    await context.close();
  }
  fs.writeFileSync(path.join(output, 'invitation-account-details-browser.json'), JSON.stringify({ mockOnly: true, results }, null, 2));
  console.log(JSON.stringify({ mockOnly: true, results }));
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}

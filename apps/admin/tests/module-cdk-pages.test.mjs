/**
 * 两个「XX 卡密管理」入口必须同时具备：菜单项、路由、页面文件。
 *
 * 之前只有邀请有卡密管理；接码 / 邀请 的卡密管理页签与页面是后补的。
 * 这里钉住「菜单 ↔ 路由 ↔ 页面」三者齐全，避免只加一半（点进去 404 或菜单里找不到）。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');

const ENTRIES = [
  { path: '/sms/cdk', name: '接码卡密管理', page: '../src/pages/sms/cdk/page.tsx' },
];

test('接码 / 邀请 都有卡密管理菜单、路由与页面', () => {
  const nav = read('../src/components/admin-shell.tsx');
  const main = read('../src/main.tsx');
  for (const { path, name, page } of ENTRIES) {
    assert.match(
      nav,
      new RegExp(`path:\\s*'${path.replace('/', '\\/')}',\\s*name:\\s*'${name}'`),
      `菜单缺少 ${name}（${path}）`,
    );
    assert.ok(
      main.includes(`'${path}': lazy(() => import('./pages${path}/page'))`),
      `路由缺少 ${path}`,
    );
    assert.ok(existsSync(fileURLToPath(new URL(page, import.meta.url))), `页面文件缺少 ${page}`);
  }
  // 邀请卡密管理走的是邀请后台的原生接口，页面文件同样必须在。
  assert.ok(
    existsSync(fileURLToPath(new URL('../src/pages/invitation/cdk/page.tsx', import.meta.url))),
  );
  assert.match(nav, /path:\s*'\/invitation\/cdk',\s*name:\s*'卡密管理'/);
});

test('接码卡密管理页复用共用的卡密台账组件，不另写一份', () => {
  const shared = read('../src/components/module-cdk-page.tsx');
  // 明文与权限：没有 inventory.export 时后端不返回 code，前端必须显示脱敏值并说明。
  assert.match(shared, /已脱敏（需要卡密权限）|codeMasked/);
  assert.match(shared, /inventory\.export/);
  for (const { page } of ENTRIES) {
    const source = read(page);
    assert.match(source, /ModuleCdkPage/, `${page} 必须复用共用组件`);
    assert.doesNotMatch(source, /from 'antd'/, `${page} 不应自己搭表格`);
  }
});

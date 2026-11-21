/**
 * 标题行的「当前可用账号」。
 *
 * 运营要求：这一行要显示在「Codex 邀请」标题的右侧、字号更大、且不超出容器。
 * 这里同时钉住它原本的语义：数量必须是后端给出的**非负实数**，未知（负数/非数字/
 * 未取到）就整块隐藏，绝不显示"不限量"。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const sourceUrl = new URL('../src/components/redeem-intro.tsx', import.meta.url);
const compiled = ts.transpileModule(readFileSync(sourceUrl, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const module = { exports: {} };
// jsx: ReactJSX 会 require('react/jsx-runtime')，所以要接真实的模块解析器。
const realRequire = createRequire(sourceUrl);
new Function('require', 'module', 'exports', compiled)(
  (name) => (name === 'react' ? React : realRequire(name)),
  module,
  module.exports,
);
const { RedeemIntro } = module.exports;

const render = (poolCount) =>
  renderToStaticMarkup(
    React.createElement(RedeemIntro, {
      title: 'Codex 邀请',
      description: '用卡密领取邮箱，发送邀请后再开始。',
      poolCount,
    }),
  );

test('可用账号数显示在标题同一行里，且是放大样式', () => {
  const html = render(4);
  assert.match(html, /Codex 邀请/);
  assert.match(html, /当前可用账号：<strong>4<\/strong> 个/);
  // 同处一个标题行容器（flex 行），不是标题下方的独立段落。
  const introStart = html.indexOf('redeem-intro');
  const countStart = html.indexOf('redeem-availability');
  assert.ok(introStart >= 0 && countStart > introStart, '数量必须在 .redeem-intro 容器内');
  assert.match(html, /class="redeem-intro-main"[\s\S]*Codex 邀请[\s\S]*<\/div>[\s\S]*redeem-availability/);
  // 样式里必须有放大与非溢出的规则。选择器要比 `.redeem-intro p`（正文 14px）
  // 更具体，否则放大不生效 —— 这条断言就是因为踩过一次才加的。
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
  const rule =
    css.match(/\.redeem-intro\s+\.redeem-availability\s*\{[^}]*\}/)?.[0] ||
    css.match(/p\.redeem-availability\s*\{[^}]*\}/)?.[0] ||
    '';
  assert.ok(rule, '必须有独立的可用账号数样式规则，且选择器要压过 .redeem-intro p');
  assert.match(rule, /font-size:\s*clamp\((?:1[89]|2\d)px/, '可用账号数的字号必须明显大于正文 14px');
  assert.match(rule, /max-width:\s*100%/, '不能超出容器');
  assert.match(rule, /overflow-wrap:\s*anywhere/);
  assert.match(css, /\.redeem-intro\s*\{[^}]*flex-wrap:\s*wrap/, '窄屏要换行而不是横向溢出');
});

test('数量未知时整块隐藏，绝不显示"不限量"', () => {
  for (const value of [-1, undefined, null, Number.NaN]) {
    const html = render(value);
    assert.doesNotMatch(html, /当前可用账号/, String(value));
    assert.doesNotMatch(html, /不限量/, String(value));
    assert.match(html, /Codex 邀请/, '标题必须还在');
  }
});

test('0 个也要如实显示（不是隐藏）', () => {
  const html = render(0);
  assert.match(html, /当前可用账号：<strong>0<\/strong> 个/);
});

test('可用账号数带最后更新时间，且缺省时不显示时间', () => {
  const updated = new Date('2026-10-11T10:20:30');
  const withTime = renderToStaticMarkup(
    React.createElement(RedeemIntro, {
      title: 'Codex 邀请',
      description: '用卡密领取邮箱，发送邀请后再开始。',
      poolCount: 4,
      poolUpdatedAt: updated,
    }),
  );
  // 审查 8.5：刷新之后要让用户看得出来这是什么时候的数字，否则过时快照会被当成实时。
  assert.match(withTime, /更新于/);
  assert.match(withTime, /当前可用账号：<strong>4<\/strong> 个/);

  // 没有更新时间时不得凭空造一个时间出来。
  assert.doesNotMatch(render(4), /更新于/);
  // 数量未知（整块隐藏）时也不该只剩一行时间。
  const hidden = renderToStaticMarkup(
    React.createElement(RedeemIntro, {
      title: 'Codex 邀请',
      description: '说明',
      poolCount: -1,
      poolUpdatedAt: updated,
    }),
  );
  assert.doesNotMatch(hidden, /更新于/);
  assert.doesNotMatch(hidden, /当前可用账号/);
});

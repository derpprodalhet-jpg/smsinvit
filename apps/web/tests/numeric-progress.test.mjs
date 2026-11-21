import { test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { progressComponents } from './components.mjs';
const render = props => renderToStaticMarkup(React.createElement(progressComponents.NumericProgress, { label: '兑换进度', ...props }));
test('only confirmed success reaches 100, invalid and failed values stay bounded', () => {
  for (const [value, expected] of [[undefined, 0], [NaN, 0], [Infinity, 0], [-12, 0], [44.3, 44], [100, 95], [999, 95]]) {
    assert.match(render({value}), new RegExp(`aria-valuenow="${expected}"`));
  }
  assert.match(render({value: 100, failed: true}), /aria-valuenow="95"/);
  assert.match(render({value: 30, success: true}), /aria-valuenow="100"/);
});
test('progress is a number in both visual and accessible output', () => {
  const html = render({value: 55, stageLabel: 'PRIVATE-STAGE', message: 'PRIVATE-DIAGNOSTIC'});
  assert.match(html, /55%/);
  assert.match(html, /role="progressbar"/);
  assert.doesNotMatch(html, /PRIVATE|排队|处理中|animate-spin/);
});

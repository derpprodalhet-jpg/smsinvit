import test from 'node:test';
import assert from 'node:assert/strict';
import { AppError, BusinessError, ErrorCodes } from '../dist/index.js';

test('AppError sets correct properties and formats standard error response', () => {
  const err = new AppError({
    code: ErrorCodes.INVENTORY_INVALID,
    message: '卡密状态异常',
    httpStatus: 409,
    details: { skuId: 'test-sku' },
  });

  assert.equal(err.code, ErrorCodes.INVENTORY_INVALID);
  assert.equal(err.httpStatus, 409);
  assert.equal(err.message, '卡密状态异常');

  const res = err.toResponse('req_123');
  assert.equal(res.success, false);
  assert.equal(res.error.code, ErrorCodes.INVENTORY_INVALID);
  assert.equal(res.error.message, '卡密状态异常');
  assert.equal(res.error.requestId, 'req_123');
  assert.deepEqual(res.error.details, { skuId: 'test-sku' });
});

test('BusinessError defaults to 409 conflict', () => {
  const err = new BusinessError(ErrorCodes.INVENTORY_INVALID, '卡密状态异常');
  assert.equal(err.httpStatus, 409);
  assert.equal(err.code, ErrorCodes.INVENTORY_INVALID);
});

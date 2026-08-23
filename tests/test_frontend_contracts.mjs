import assert from 'node:assert/strict';
import test from 'node:test';

import {isSeedWithinRange, MAX_SAFE_SEED} from '../webapp/static/frontend-contracts.mjs';

test('seed text stays inside the exact JSON integer range', () => {
  assert.equal(isSeedWithinRange(''), true);
  assert.equal(isSeedWithinRange('0'), true);
  assert.equal(isSeedWithinRange('00042'), true);
  assert.equal(isSeedWithinRange(MAX_SAFE_SEED), true);
  assert.equal(isSeedWithinRange('9007199254740992'), false);
  assert.equal(isSeedWithinRange('4.2'), false);
  assert.equal(isSeedWithinRange('-1'), false);
});

import assert from 'node:assert/strict';
import test from 'node:test';

import {showEmpty} from '../webapp/static/shared.js';
import {updateRefreshStatus} from '../webapp/static/refresh-status.mjs';

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.className = '';
    this.dataset = {};
    this.textContent = '';
  }

  append(...children) {
    this.children.push(...children);
  }
}

globalThis.document = {
  createElement: tagName => new FakeElement(tagName),
};

test('empty-state rendering mutates the target DOM explicitly', () => {
  const target = new FakeElement('section');
  showEmpty(target, 'nothing here');

  assert.equal(target.dataset.empty, 'true');
  assert.equal(target.children.length, 1);
  assert.equal(target.children[0].className, 'empty');
  assert.equal(target.children[0].textContent, 'nothing here');
});

test('refresh status retains independent failures until each recovers', () => {
  const failures = {};
  const status = new FakeElement();

  updateRefreshStatus(failures, status, 'chat', new Error('offline'));
  updateRefreshStatus(failures, status, 'references', new Error('denied'));
  assert.equal(status.textContent, 'refresh: chat: offline · references: denied');

  updateRefreshStatus(failures, status, 'chat', null);
  assert.equal(status.textContent, 'refresh: references: denied');
  updateRefreshStatus(failures, status, 'references', null);
  assert.equal(status.textContent, '');
});

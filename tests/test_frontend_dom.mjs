import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
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

test('responsive workspace exposes one control and panel per pane', () => {
  const html = readFileSync(
    new URL('../webapp/static/index.html', import.meta.url), 'utf8');
  assert.match(html, /id="workspace" data-pane="chat"/);
  for (const pane of ['projects', 'chat', 'media']) {
    assert.match(html, new RegExp(`data-workspace-pane="${pane}"`));
    assert.match(html, new RegExp(`data-workspace-panel="${pane}"`));
    assert.match(html, new RegExp(`aria-controls="workspace-${pane}"`));
  }
});

test('prompt and generation controls live in a desktop-open collapsible panel', () => {
  const html = readFileSync(
    new URL('../webapp/static/index.html', import.meta.url), 'utf8');
  const css = readFileSync(
    new URL('../webapp/styles.css', import.meta.url), 'utf8');
  assert.match(html, /<details class="panel prompt-panel" id="prompt-panel" open>/);
  assert.match(html, /<summary class="prompt-panel-summary">Prompt &amp; generation<\/summary>/);
  assert.match(html, /<pre class="prompt" id="prompt">/);
  assert.match(css, /--prompt-panel-header-height:1\.5rem/);
  assert.match(css, /\.prompt-panel-body pre\.prompt \{ max-height:12rem; overflow:auto; \}/);
});

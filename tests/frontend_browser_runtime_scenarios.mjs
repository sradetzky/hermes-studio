import assert from 'node:assert/strict';

import {browserTest} from './frontend_browser_harness.mjs';

browserTest(
  'Chromium rejects a stale chat submission after project navigation',
  async browser => {
    const chatRequests = [];
    browser.addDiagnostic('paused chat requests', () => chatRequests.length);
    browser.intercept(async params => {
      const url = new URL(params.request.url);
      if (params.request.method === 'POST' && url.pathname.endsWith('/chat')) {
        chatRequests.push(params.requestId);
        return true;
      }
      return false;
    });

    await browser.navigate();
    await browser.selectProject('Beta');
    await browser.evaluate(`(() => {
      document.querySelector('#chatinput').value = 'stale message';
      document.querySelector('#chat-form').requestSubmit();
    })()`);
    await browser.waitFor(() => chatRequests.length === 1, 'chat request');
    await browser.selectProject('Alpha');
    await browser.fulfill(chatRequests.pop(), {
      id: 'stale-job', project: 'beta', clip_id: 'clip-001',
      profile: 'studio', status: 'queued', message: 'stale message',
    });
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    assert.equal(await browser.evaluate(
      `document.querySelector('#status').textContent`), '');
    assert.notEqual(await browser.evaluate(
      `document.querySelector('#activity-text').textContent`), 'Studio queued');
  },
);

browserTest(
  'Chromium applies only the latest ComfyUI queue response',
  async browser => {
    const queueRequests = [];
    let queueCount = 0;
    browser.addDiagnostic('paused queue requests', () => queueRequests.length);
    browser.intercept(async (params, fixture) => {
      const url = new URL(params.request.url);
      if (url.pathname !== '/api/comfyui/queue') return false;
      queueCount += 1;
      if (queueCount === 1) {
        await fixture.fulfill(params.requestId, {
          available: true, running: [], pending: [], recent_completed: null,
        });
      } else {
        queueRequests.push(params.requestId);
      }
      return true;
    });

    await browser.navigate();
    await browser.evaluate(`document.querySelector('#comfy-queue').open = true`);
    await browser.waitFor(() => queueRequests.length === 1, 'older queue request');
    await browser.evaluate(
      `document.querySelector('#comfy-queue').dispatchEvent(new Event('toggle'))`);
    await browser.waitFor(() => queueRequests.length === 2, 'newer queue request');
    await browser.fulfill(queueRequests.pop(), {
      available: true,
      running: [{
        prompt_id: 'new-prompt', recipe: 'H3', mode: 'R2V', elapsed_seconds: 4,
      }],
      pending: [], recent_completed: null,
    });
    await browser.waitExpression(
      `document.querySelector('#comfy-queue-label').textContent.includes('H3 R2V')`,
      'new queue state');
    await browser.fulfill(queueRequests.pop(), {
      available: true, running: [], pending: [], recent_completed: null,
    });
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    assert.match(await browser.evaluate(
      `document.querySelector('#comfy-queue-label').textContent`), /H3 R2V/);
  },
);

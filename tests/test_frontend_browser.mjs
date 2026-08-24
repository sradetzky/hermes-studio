import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {test} from 'node:test';

const repo = resolve(import.meta.dirname, '..');

async function freePort() {
  const server = createServer();
  await new Promise((resolveListen, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolveListen);
  });
  const {port} = server.address();
  await new Promise(resolveClose => server.close(resolveClose));
  return port;
}

async function waitFor(predicate, message, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await new Promise(resolveWait => setTimeout(resolveWait, 25));
  }
  throw new Error(`timed out: ${message}`);
}

async function waitForHttp(url) {
  await waitFor(async () => {
    try {
      return (await fetch(url)).ok;
    } catch (_) {
      return false;
    }
  }, `HTTP readiness for ${url}`);
}

function chromiumExecutable() {
  for (const candidate of [
    process.env.CHROMIUM,
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome-stable',
  ]) {
    if (candidate) return candidate;
  }
  throw new Error('Chromium executable is unavailable');
}

class CdpClient {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result || {});
        return;
      }
      for (const handler of this.handlers.get(message.method) || []) {
        Promise.resolve(handler(message.params || {})).catch(error => {
          this.eventError = error;
        });
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    const promise = new Promise((resolveSend, reject) => {
      this.pending.set(id, {resolve: resolveSend, reject});
    });
    this.socket.send(JSON.stringify({id, method, params}));
    return promise;
  }

  on(method, handler) {
    const handlers = this.handlers.get(method) || [];
    handlers.push(handler);
    this.handlers.set(method, handlers);
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description ||
        result.exceptionDetails.text || 'browser evaluation failed');
    }
    return result.result?.value;
  }
}

async function connectCdp(port) {
  const page = await waitFor(async () => {
    try {
      const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      return tabs.find(tab => tab.type === 'page');
    } catch (_) {
      return null;
    }
  }, 'Chromium CDP page');
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolveOpen, reject) => {
    socket.addEventListener('open', resolveOpen, {once: true});
    socket.addEventListener('error', reject, {once: true});
  });
  return new CdpClient(socket);
}

function detailPayload(label, promoted = false) {
  return {
    gen: '001',
    files: ['video.mp4'],
    media: [{
      name: 'video.mp4', kind: 'video', size: 21,
      url: '/media/browser-fixture-video.mp4',
      promoted, reference: false,
    }],
    meta: {label, recipe: 'r2v'},
    review: {promoted: promoted ? ['video.mp4'] : [], references: []},
    prompt: `${label} prompt`,
    actions: promoted ? [{
      action: 'promote', source: 'video.mp4', target: 'final.mp4',
    }] : [],
  };
}

test('Chromium rejects stale UI work and preserves active media', {timeout: 30000}, async () => {
  const fixture = await mkdtemp(join(tmpdir(), 'hermes-studio-browser-'));
  const profile = await mkdtemp(join(tmpdir(), 'hermes-studio-chromium-'));
  const appPort = await freePort();
  const cdpPort = await freePort();
  const app = spawn(
    join(repo, '.venv/bin/python'),
    ['-m', 'tests.browser_fixture_app', String(appPort), fixture],
    {cwd: repo, stdio: ['ignore', 'pipe', 'pipe']},
  );
  let appLog = '';
  app.stdout.on('data', chunk => { appLog += chunk; });
  app.stderr.on('data', chunk => { appLog += chunk; });
  let browser;
  let client;
  const browserErrors = [];
  const queueRequests = [];
  const detailRequests = [];
  const metadataRequests = [];
  const chatRequests = [];
  try {
    await waitForHttp(`http://127.0.0.1:${appPort}/`);
    browser = spawn(chromiumExecutable(), [
      '--headless=new', '--no-sandbox', '--disable-gpu',
      `--remote-debugging-port=${cdpPort}`,
      `--user-data-dir=${profile}`,
      'about:blank',
    ], {stdio: ['ignore', 'ignore', 'pipe']});
    client = await connectCdp(cdpPort);
    client.on('Runtime.exceptionThrown', ({exceptionDetails}) => {
      browserErrors.push(exceptionDetails.exception?.description || exceptionDetails.text);
    });
    await client.send('Runtime.enable');
    await client.send('Page.enable');
    await client.send('Page.addScriptToEvaluateOnNewDocument', {
      source: 'window.setInterval = () => 0;',
    });


    let queueCount = 0;
    const fulfill = (requestId, value, responseCode = 200) => client.send(
      'Fetch.fulfillRequest', {
        requestId,
        responseCode,
        responseHeaders: [{name: 'Content-Type', value: 'application/json'}],
        body: Buffer.from(JSON.stringify(value)).toString('base64'),
      });
    await client.send('Fetch.enable', {patterns: [{urlPattern: '*'}]});
    client.on('Fetch.requestPaused', async params => {
      const url = new URL(params.request.url);
      const method = params.request.method;
      if (url.pathname === '/api/comfyui/queue') {
        queueCount += 1;
        if (queueCount === 1) {
          await fulfill(params.requestId, {
            available: true, running: [], pending: [], recent_completed: null,
          });
        } else queueRequests.push(params.requestId);
        return;
      }
      if (method === 'GET' && url.pathname.endsWith('/generations/001')) {
        detailRequests.push(params.requestId);
        return;
      }
      if (method === 'POST' && url.pathname.endsWith('/generations/001/promote')) {
        await fulfill(params.requestId, {ok: true, result: {target: 'final.mp4'}});
        return;
      }
      if (method === 'PATCH' && /^\/api\/project\/[^/]+$/.test(url.pathname)) {
        metadataRequests.push(params.requestId);
        return;
      }
      if (method === 'POST' && url.pathname.endsWith('/chat')) {
        chatRequests.push(params.requestId);
        return;
      }
      await client.send('Fetch.continueRequest', {requestId: params.requestId});
    });

    await client.send('Page.navigate', {url: `http://127.0.0.1:${appPort}/`});
    const evaluate = expression => client.evaluate(expression);
    const waitExpression = (expression, message) => waitFor(
      () => evaluate(`Boolean(${expression})`), message);
    await waitExpression(
      `document.querySelectorAll('.proj').length === 2`, 'project list');
    await evaluate(`[
      ...document.querySelectorAll('.proj')
    ].find(button => button.querySelector('.proj-title')?.textContent === 'Alpha').click()`);
    await waitExpression(
      `document.querySelector('.proj.active .proj-title')?.textContent === 'Alpha'`,
      'Alpha project selection');
    await waitExpression(`document.querySelector('.generation-preview')`, 'generation card');

    await evaluate(`document.querySelector('.generation-preview').click()`);
    await waitFor(() => detailRequests.length === 1, 'first take request');
    await evaluate(`document.querySelector('#generation-close').click()`);
    await evaluate(`document.querySelector('.generation-preview').click()`);
    await waitFor(() => detailRequests.length === 2, 'second take request');
    await fulfill(detailRequests.pop(), detailPayload('new'));
    await waitExpression(
      `document.querySelector('#generation-prompt')?.textContent === 'new prompt'`,
      'new take response');
    await fulfill(detailRequests.pop(), detailPayload('old'));
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    assert.equal(await evaluate(
      `document.querySelector('#generation-prompt').textContent`), 'new prompt');

    await evaluate(`(() => {
      const media = document.querySelector('#generation-media .detail-media');
      media.__playbackSentinel = 17;
      window.__activeMedia = media;
      document.querySelector('#promote-generation').click();
    })()`);
    await waitFor(() => detailRequests.length === 1, 'post-promote detail refresh');
    await fulfill(detailRequests.pop(), detailPayload('promoted', true));
    await waitExpression(
      `document.querySelector('#promote-generation')?.textContent === 'Promoted ✓'`,
      'promote refresh');
    assert.equal(await evaluate(
      `window.__activeMedia === document.querySelector('#generation-media .detail-media') && ` +
      `window.__activeMedia.__playbackSentinel === 17`), true);

    await evaluate(`document.querySelector('#generation-close').click()`);
    await evaluate(`document.querySelector('#edit-project').click()`);
    await evaluate(`(() => {
      document.querySelector('#project-metadata-display-title').value = 'Stale Alpha';
      document.querySelector('#project-metadata-form').requestSubmit();
    })()`);
    await waitFor(() => metadataRequests.length === 1, 'metadata save request');
    const alphaId = await evaluate(
      `document.querySelector('.proj.active .proj-id').textContent`);
    await evaluate(`[
      ...document.querySelectorAll('.proj')
    ].find(button => button.querySelector('.proj-title')?.textContent === 'Beta').click()`);
    await waitExpression(
      `document.querySelector('.proj.active .proj-title')?.textContent === 'Beta'`,
      'Beta project selection');
    await fulfill(metadataRequests.pop(), {
      project: {id: alphaId, title: 'Stale Alpha', brief: 'stale'},
    });
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    assert.equal(await evaluate(
      `document.querySelector('.proj.active .proj-title').textContent`), 'Beta');
    assert.equal(await evaluate(
      `document.querySelector('#project-metadata-dialog').open`), false);

    await evaluate(`(() => {
      document.querySelector('#chatinput').value = 'stale message';
      document.querySelector('#chat-form').requestSubmit();
    })()`);
    await waitFor(() => chatRequests.length === 1, 'chat request');
    await evaluate(`[
      ...document.querySelectorAll('.proj')
    ].find(button => button.querySelector('.proj-title')?.textContent === 'Alpha').click()`);
    await waitExpression(
      `document.querySelector('.proj.active .proj-title')?.textContent === 'Alpha'`,
      'return to Alpha');
    await fulfill(chatRequests.pop(), {
      id: 'stale-job', project: 'beta', clip_id: 'clip-001',
      profile: 'studio', status: 'queued', message: 'stale message',
    });
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    assert.equal(await evaluate(`document.querySelector('#status').textContent`), '');
    assert.notEqual(await evaluate(
      `document.querySelector('#activity-text').textContent`), 'Studio queued');

    await evaluate(`(() => {
      const queue = document.querySelector('#comfy-queue');
      queue.open = true;
    })()`);
    await waitFor(() => queueRequests.length === 1, 'older queue request');
    await evaluate(`document.querySelector('#comfy-queue').dispatchEvent(new Event('toggle'))`);
    await waitFor(() => queueRequests.length === 2, 'newer queue request');
    await fulfill(queueRequests.pop(), {
      available: true,
      running: [{prompt_id: 'new-prompt', recipe: 'H3', mode: 'R2V', elapsed_seconds: 4}],
      pending: [], recent_completed: null,
    });
    await waitExpression(
      `document.querySelector('#comfy-queue-label').textContent.includes('H3 R2V')`,
      'new queue state');
    await fulfill(queueRequests.pop(), {
      available: true, running: [], pending: [], recent_completed: null,
    });
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    assert.match(await evaluate(
      `document.querySelector('#comfy-queue-label').textContent`), /H3 R2V/);

    if (client.eventError) throw client.eventError;
    assert.deepEqual(browserErrors, []);
  } catch (error) {
    if (client) {
      try {
        const snapshot = await client.evaluate(`({
          title: document.title,
          ready: document.readyState,
          body: document.body?.innerText?.slice(0, 1000),
          projects: [...document.querySelectorAll('.proj-title')].map(node => node.textContent),
          status: document.querySelector('#status')?.textContent,
          dialogOpen: document.querySelector('#generation-dialog')?.open,
          generationTitle: document.querySelector('#generation-title')?.textContent,
          generationPrompt: document.querySelector('#generation-prompt')?.textContent,
          mediaStatus: document.querySelector('#media-action-status')?.textContent,
        })`);
        error.message += `\nbrowser snapshot:\n${JSON.stringify(snapshot, null, 2)}`;
      } catch (snapshotError) {
        error.message += `\nbrowser snapshot failed: ${snapshotError.message}`;
      }
      if (client.eventError) error.message += `\nCDP event error: ${client.eventError.stack}`;
    }
    if (browserErrors.length) {
      error.message += `\nbrowser errors:\n${browserErrors.join('\n')}`;
    }
    error.message += `\npaused requests: queue=${queueRequests?.length ?? 'n/a'} ` +
      `detail=${detailRequests?.length ?? 'n/a'} metadata=${metadataRequests?.length ?? 'n/a'} ` +
      `chat=${chatRequests?.length ?? 'n/a'}`;
    error.message += `\nfixture app log:\n${appLog}`;
    throw error;
  } finally {
    if (client) client.socket.close();
    if (browser && browser.exitCode === null) browser.kill('SIGTERM');
    if (app.exitCode === null) app.kill('SIGTERM');
    await Promise.all([
      browser && browser.exitCode === null
        ? new Promise(resolveExit => browser.once('exit', resolveExit)) : Promise.resolve(),
      app.exitCode === null
        ? new Promise(resolveExit => app.once('exit', resolveExit)) : Promise.resolve(),
    ]);
    await rm(fixture, {recursive: true, force: true});
    await rm(profile, {recursive: true, force: true});
  }
});

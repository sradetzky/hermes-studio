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

export async function waitFor(predicate, message, timeout = 8000) {
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

export function detailPayload(label, promoted = false) {
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

class BrowserFixture {
  static async create() {
    const fixture = await mkdtemp(join(tmpdir(), 'hermes-studio-browser-'));
    const profile = await mkdtemp(join(tmpdir(), 'hermes-studio-chromium-'));
    const appPort = await freePort();
    const cdpPort = await freePort();
    const app = spawn(
      process.env.PYTHON || join(repo, '.venv/bin/python'),
      ['-m', 'tests.browser_fixture_app', String(appPort), fixture],
      {cwd: repo, stdio: ['ignore', 'pipe', 'pipe']},
    );
    const harness = new BrowserFixture({fixture, profile, appPort, cdpPort, app});
    app.stdout.on('data', chunk => { harness.appLog += chunk; });
    app.stderr.on('data', chunk => { harness.appLog += chunk; });
    try {
      await harness.start();
      return harness;
    } catch (error) {
      await harness.close();
      throw error;
    }
  }

  constructor({fixture, profile, appPort, cdpPort, app}) {
    this.fixture = fixture;
    this.profile = profile;
    this.appPort = appPort;
    this.cdpPort = cdpPort;
    this.app = app;
    this.appLog = '';
    this.browser = null;
    this.client = null;
    this.browserErrors = [];
    this.requestHandlers = [];
    this.diagnostics = [];
  }

  async start() {
    await waitForHttp(`http://127.0.0.1:${this.appPort}/`);
    this.browser = spawn(chromiumExecutable(), [
      '--headless=new', '--no-sandbox', '--disable-gpu',
      `--remote-debugging-port=${this.cdpPort}`,
      `--user-data-dir=${this.profile}`,
      'about:blank',
    ], {stdio: ['ignore', 'ignore', 'pipe']});
    this.client = await connectCdp(this.cdpPort);
    this.client.on('Runtime.exceptionThrown', ({exceptionDetails}) => {
      this.browserErrors.push(
        exceptionDetails.exception?.description || exceptionDetails.text);
    });
    await this.client.send('Runtime.enable');
    await this.client.send('Page.enable');
    await this.client.send('Page.addScriptToEvaluateOnNewDocument', {
      source: 'window.setInterval = () => 0;',
    });
    await this.client.send('Fetch.enable', {patterns: [{urlPattern: '*'}]});
    this.client.on('Fetch.requestPaused', async params => {
      for (const handler of this.requestHandlers) {
        if (await handler(params, this)) return;
      }
      const url = new URL(params.request.url);
      if (url.pathname === '/api/comfyui/queue') {
        await this.fulfill(params.requestId, {
          available: true, running: [], pending: [], recent_completed: null,
        });
        return;
      }
      await this.continueRequest(params.requestId);
    });
  }

  intercept(handler) {
    this.requestHandlers.push(handler);
  }

  addDiagnostic(label, value) {
    this.diagnostics.push([label, value]);
  }

  fulfill(requestId, value, responseCode = 200) {
    return this.client.send('Fetch.fulfillRequest', {
      requestId,
      responseCode,
      responseHeaders: [{name: 'Content-Type', value: 'application/json'}],
      body: Buffer.from(JSON.stringify(value)).toString('base64'),
    });
  }

  continueRequest(requestId) {
    return this.client.send('Fetch.continueRequest', {requestId});
  }

  evaluate(expression) {
    return this.client.evaluate(expression);
  }

  waitFor(predicate, message, timeout) {
    return waitFor(predicate, message, timeout);
  }

  waitExpression(expression, message, timeout) {
    return waitFor(
      () => this.evaluate(`Boolean(${expression})`), message, timeout);
  }

  async navigate() {
    await this.client.send('Page.navigate', {
      url: `http://127.0.0.1:${this.appPort}/`,
    });
    await this.waitExpression(
      `document.querySelectorAll('.proj').length === 2`, 'project list');
  }

  async selectProject(title) {
    await this.evaluate(`[
      ...document.querySelectorAll('.proj')
    ].find(button => button.querySelector('.proj-title')?.textContent === ${JSON.stringify(title)}).click()`);
    await this.waitExpression(
      `document.querySelector('.proj.active .proj-title')?.textContent === ${JSON.stringify(title)}`,
      `${title} project selection`);
  }

  assertClean() {
    if (this.client.eventError) throw this.client.eventError;
    assert.deepEqual(this.browserErrors, []);
  }

  async enrichError(error) {
    if (this.client) {
      try {
        const snapshot = await this.evaluate(`({
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
      if (this.client.eventError) {
        error.message += `\nCDP event error: ${this.client.eventError.stack}`;
      }
    }
    if (this.browserErrors.length) {
      error.message += `\nbrowser errors:\n${this.browserErrors.join('\n')}`;
    }
    for (const [label, value] of this.diagnostics) {
      error.message += `\n${label}: ${typeof value === 'function' ? value() : value}`;
    }
    error.message += `\nfixture app log:\n${this.appLog}`;
  }

  async close() {
    if (this.client) this.client.socket.close();
    if (this.browser && this.browser.exitCode === null) this.browser.kill('SIGTERM');
    if (this.app.exitCode === null) this.app.kill('SIGTERM');
    await Promise.all([
      this.browser && this.browser.exitCode === null
        ? new Promise(resolveExit => this.browser.once('exit', resolveExit))
        : Promise.resolve(),
      this.app.exitCode === null
        ? new Promise(resolveExit => this.app.once('exit', resolveExit))
        : Promise.resolve(),
    ]);
    await rm(this.fixture, {recursive: true, force: true});
    await rm(this.profile, {recursive: true, force: true});
  }
}

export function browserTest(name, scenario) {
  test(name, {timeout: 30000}, async () => {
    let harness;
    try {
      harness = await BrowserFixture.create();
      await scenario(harness);
      harness.assertClean();
    } catch (error) {
      if (harness) await harness.enrichError(error);
      throw error;
    } finally {
      await harness?.close();
    }
  });
}

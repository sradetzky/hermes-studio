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
    process.env.PYTHON || join(repo, '.venv/bin/python'),
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
  const movieExportRequests = [];
  const conversationChatRequests = [];
  const conversationJobRequests = [];
  const conversationActivityRequests = [];
  let deferConversation = false;
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
      if (method === 'GET' && url.pathname.endsWith('/movie')) {
        await fulfill(params.requestId, {
          readiness: {
            ready: true, enabled_clip_count: 1, blocking: [],
            clips: [{
              id: 'clip-001', title: 'Clip 1', ready: true, reason: '',
              selected_take: {generation: '001', filename: 'video.mp4'},
            }],
          },
          movies: [{
            id: 'movie-001', filename: 'movie.mp4', size: 1024,
            url: '/media/browser-fixture-movie.mp4',
            download_url: '/media/browser-fixture-movie.mp4',
            created_at: '2026-08-24T10:00:00+00:00', clip_count: 1,
            duration_seconds: 1.25, assembly_mode: 'stream-copy',
            sha256: 'a'.repeat(64),
          }],
        });
        return;
      }
      if (method === 'POST' && url.pathname.endsWith('/movie')) {
        movieExportRequests.push(params.requestId);
        await fulfill(params.requestId, {
          id: 'movie-export-job', project: 'alpha', clip_id: null,
          chat_scope: 'project', kind: 'export_movie', message: '{}',
          profile: 'studio', status: 'queued', reply: '', error: null,
          session_id: null, owner_id: null, process_pid: null,
          process_start_time: null,
          created_at: '2026-08-24T10:00:00+00:00',
          started_at: null, finished_at: null,
          updated_at: '2026-08-24T10:00:00+00:00',
        }, 202);
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
      if (deferConversation && method === 'GET' && url.pathname.endsWith('/chat')) {
        conversationChatRequests.push(params.requestId);
        return;
      }
      if (deferConversation && method === 'GET' && url.pathname.endsWith('/jobs')) {
        conversationJobRequests.push(params.requestId);
        return;
      }
      if (deferConversation && method === 'GET' && url.pathname.endsWith('/events')) {
        conversationActivityRequests.push(params.requestId);
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
    await waitExpression(
      `document.querySelector('#export-movie')?.disabled === false`,
      'movie export readiness');
    assert.equal(movieExportRequests.length, 0);
    assert.equal(await evaluate(
      `document.querySelector('.movie-download')?.getAttribute('download')`),
    'movie-001.mp4');
    await evaluate(`(() => {
      const media = document.querySelector('.movie-media');
      media.__playbackSentinel = 23;
      window.__movieMedia = media;
      document.querySelector('#export-movie').click();
    })()`);
    await waitFor(() => movieExportRequests.length === 1, 'explicit movie export');
    await waitExpression(
      `document.querySelector('#export-movie')?.disabled === false`,
      'movie export refresh');
    assert.equal(await evaluate(
      `window.__movieMedia === document.querySelector('.movie-media') && ` +
      `window.__movieMedia.__playbackSentinel === 23`), true);

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

    deferConversation = true;
    await evaluate(`void import('/static/conversation-controller.js').then(
      module => {
        module.resetConversation('project');
        return module.refreshConversation(() => {});
      })`);
    await waitFor(() => conversationChatRequests.length === 1 &&
      conversationJobRequests.length === 1 &&
      conversationActivityRequests.length === 1, 'initial deferred conversation');
    const initialMessages = Array.from({length: 40}, (_, index) => ({
      id: index + 1,
      role: index === 0 ? 'user' : 'assistant',
      profile: 'studio',
      content: index === 39 ? 'initial latest output' : `history row ${index + 1}`,
      job_id: index === 0 ? 'job-scroll' : null,
    }));
    const initialEvents = [
      {
        id: 1, job_id: 'job-scroll', event_type: 'job.started',
        status: 'running', profile: 'studio', summary: 'Started', detail: {},
      },
      {
        id: 2, job_id: 'job-scroll', event_type: 'reasoning',
        status: 'running', profile: 'studio', summary: 'Thinking',
        detail: {text: 'stable selected reasoning text'},
      },
      ...Array.from({length: 34}, (_, index) => ({
        id: index + 3, job_id: 'job-scroll', event_type: 'commentary',
        status: 'running', profile: 'studio', summary: `activity row ${index + 1}`,
        detail: {text: `activity detail ${index + 1}`},
      })),
    ];
    await fulfill(conversationChatRequests.pop(), {
      messages: initialMessages, cursor: 40,
    });
    await fulfill(conversationJobRequests.pop(), {jobs: [{
      id: 'job-scroll', status: 'running', profile: 'studio', message: 'scroll test',
    }]});
    await fulfill(conversationActivityRequests.pop(), {
      events: initialEvents, cursor: 36,
    });
    await waitExpression(
      `document.querySelector('#chatlog').textContent.includes('initial latest output')`,
      'initial conversation output');
    const initialViewport = await evaluate(`(() => {
      const chat = document.querySelector('#chatlog');
      const list = document.querySelector('.job-event-list');
      return {
        chat: [chat.scrollHeight, chat.scrollTop, chat.clientHeight],
        list: [list.scrollHeight, list.scrollTop, list.clientHeight],
      };
    })()`);
    assert.ok(
      initialViewport.chat[0] - initialViewport.chat[1] - initialViewport.chat[2] <= 1,
      `initial chat did not follow latest: ${initialViewport.chat}`);
    assert.ok(
      initialViewport.list[0] - initialViewport.list[1] - initialViewport.list[2] <= 1,
      `initial activity did not follow latest: ${initialViewport.list}`);

    await evaluate(`(() => {
      const chat = document.querySelector('#chatlog');
      const card = document.querySelector('[data-activity-job="job-scroll"]');
      const list = card.querySelector('.job-event-list');
      const reasoning = card.querySelector('[data-event-detail="2"]');
      card.open = true;
      reasoning.open = true;
      chat.scrollTop = 0;
      list.scrollTop = 0;
      reasoning.querySelector('summary').focus();
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(reasoning.querySelector('pre'));
      selection.removeAllRanges();
      selection.addRange(range);
      window.__stableCard = card;
      window.__stableEvent = card.querySelector('[data-activity-event="2"]');
      window.__stableReasoning = reasoning;
      window.__awayChatTop = chat.scrollTop;
      window.__awayListTop = list.scrollTop;
    })()`);
    await evaluate(`void import('/static/conversation-controller.js').then(
      module => module.refreshConversation(() => {}))`);
    await waitFor(() => conversationChatRequests.length === 1 &&
      conversationJobRequests.length === 1 &&
      conversationActivityRequests.length === 1, 'incremental deferred conversation');
    await fulfill(conversationChatRequests.pop(), {
      messages: [{
        id: 41, role: 'assistant', profile: 'studio',
        content: 'output while scrolled away', job_id: 'job-scroll',
      }],
      cursor: 41,
    });
    await fulfill(conversationJobRequests.pop(), {jobs: [{
      id: 'job-scroll', status: 'completed', profile: 'studio', message: 'scroll test',
    }]});
    await fulfill(conversationActivityRequests.pop(), {
      events: [{
        id: 37, job_id: 'job-scroll', event_type: 'job.completed',
        status: 'completed', profile: 'studio', summary: 'Completed', detail: {},
      }],
      cursor: 37,
    });
    await waitExpression(
      `document.querySelector('#chatlog').textContent.includes('output while scrolled away')`,
      'scrolled-away output');
    assert.deepEqual(await evaluate(`(() => {
      const chat = document.querySelector('#chatlog');
      const card = document.querySelector('[data-activity-job="job-scroll"]');
      const list = card.querySelector('.job-event-list');
      return {
        chatTop: chat.scrollTop,
        listTop: list.scrollTop,
        cardStable: card === window.__stableCard,
        eventStable: card.querySelector('[data-activity-event="2"]') === window.__stableEvent,
        reasoningStable: card.querySelector('[data-event-detail="2"]') === window.__stableReasoning,
        cardOpen: card.open,
        reasoningOpen: window.__stableReasoning.open,
        selection: getSelection().toString(),
      };
    })()`), {
      chatTop: 0,
      listTop: 0,
      cardStable: true,
      eventStable: true,
      reasoningStable: true,
      cardOpen: true,
      reasoningOpen: true,
      selection: 'stable selected reasoning text',
    });

    await evaluate(`(() => {
      const chat = document.querySelector('#chatlog');
      const list = document.querySelector('.job-event-list');
      chat.scrollTop = chat.scrollHeight;
      list.scrollTop = list.scrollHeight;
    })()`);
    await evaluate(`void import('/static/conversation-controller.js').then(
      module => module.refreshConversation(() => {}))`);
    await waitFor(() => conversationChatRequests.length === 1 &&
      conversationJobRequests.length === 1 &&
      conversationActivityRequests.length === 1, 'follow-latest deferred conversation');
    await fulfill(conversationChatRequests.pop(), {
      messages: [{
        id: 42, role: 'assistant', profile: 'studio',
        content: 'followed latest output', job_id: 'job-scroll',
      }],
      cursor: 42,
    });
    await fulfill(conversationJobRequests.pop(), {jobs: []});
    await fulfill(conversationActivityRequests.pop(), {
      events: [{
        id: 38, job_id: 'job-scroll', event_type: 'commentary',
        status: 'completed', profile: 'studio', summary: 'Final note',
        detail: {text: 'final note'},
      }],
      cursor: 38,
    });
    await waitExpression(
      `document.querySelector('#chatlog').textContent.includes('followed latest output')`,
      'followed output');
    assert.equal(await evaluate(`(() => {
      const chat = document.querySelector('#chatlog');
      const list = document.querySelector('.job-event-list');
      return chat.scrollHeight - chat.scrollTop - chat.clientHeight <= 1 &&
        list.scrollHeight - list.scrollTop - list.clientHeight <= 1;
    })()`), true);

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
      `chat=${chatRequests?.length ?? 'n/a'} movie=${movieExportRequests?.length ?? 'n/a'} ` +
      `conversation=${conversationChatRequests?.length ?? 'n/a'}/` +
      `${conversationJobRequests?.length ?? 'n/a'}/` +
      `${conversationActivityRequests?.length ?? 'n/a'}`;
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

import assert from 'node:assert/strict';

import {
  browserTest,
  detailPayload,
} from './frontend_browser_harness.mjs';

browserTest(
  'Chromium preserves movie and take playback across refreshes',
  async browser => {
    const detailRequests = [];
    const movieExportRequests = [];
    browser.addDiagnostic(
      'paused requests',
      () => `detail=${detailRequests.length} movie=${movieExportRequests.length}`,
    );
    browser.intercept(async (params, fixture) => {
      const url = new URL(params.request.url);
      const method = params.request.method;
      if (method === 'GET' && url.pathname.endsWith('/movie')) {
        await fixture.fulfill(params.requestId, {
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
        return true;
      }
      if (method === 'POST' && url.pathname.endsWith('/movie')) {
        movieExportRequests.push(params.requestId);
        await fixture.fulfill(params.requestId, {
          id: 'movie-export-job', project: 'alpha', clip_id: null,
          chat_scope: 'project', kind: 'export_movie', message: '{}',
          profile: 'studio', status: 'queued', reply: '', error: null,
          session_id: null, owner_id: null, process_pid: null,
          process_start_time: null,
          created_at: '2026-08-24T10:00:00+00:00',
          started_at: null, finished_at: null,
          updated_at: '2026-08-24T10:00:00+00:00',
        }, 202);
        return true;
      }
      if (method === 'GET' && url.pathname.endsWith('/generations/001')) {
        detailRequests.push(params.requestId);
        return true;
      }
      if (method === 'POST' && url.pathname.endsWith('/generations/001/promote')) {
        await fixture.fulfill(
          params.requestId, {ok: true, result: {target: 'final.mp4'}});
        return true;
      }
      return false;
    });

    await browser.navigate();
    await browser.selectProject('Alpha');
    await browser.waitExpression(
      `document.querySelector('.generation-preview')`, 'generation card');
    await browser.waitExpression(
      `document.querySelector('#export-movie')?.disabled === false`,
      'movie export readiness');
    assert.equal(movieExportRequests.length, 0);
    assert.equal(await browser.evaluate(
      `document.querySelector('.movie-download')?.getAttribute('download')`),
    'movie-001.mp4');

    await browser.evaluate(`(() => {
      const media = document.querySelector('.movie-media');
      media.__playbackSentinel = 23;
      window.__movieMedia = media;
      document.querySelector('#export-movie').click();
    })()`);
    await browser.waitFor(
      () => movieExportRequests.length === 1, 'explicit movie export');
    await browser.waitExpression(
      `document.querySelector('#export-movie')?.disabled === false`,
      'movie export refresh');
    assert.equal(await browser.evaluate(
      `window.__movieMedia === document.querySelector('.movie-media') && ` +
      `window.__movieMedia.__playbackSentinel === 23`), true);

    await browser.evaluate(`document.querySelector('.generation-preview').click()`);
    await browser.waitFor(() => detailRequests.length === 1, 'take request');
    await browser.fulfill(detailRequests.pop(), detailPayload('current'));
    await browser.waitExpression(
      `document.querySelector('#generation-prompt')?.textContent === 'current prompt'`,
      'take response');
    await browser.evaluate(`(() => {
      const media = document.querySelector('#generation-media .detail-media');
      media.__playbackSentinel = 17;
      window.__activeMedia = media;
      document.querySelector('#promote-generation').click();
    })()`);
    await browser.waitFor(
      () => detailRequests.length === 1, 'post-promote detail refresh');
    await browser.fulfill(detailRequests.pop(), detailPayload('promoted', true));
    await browser.waitExpression(
      `document.querySelector('#promote-generation')?.textContent === 'Promoted ✓'`,
      'promote refresh');
    assert.equal(await browser.evaluate(
      `window.__activeMedia === document.querySelector('#generation-media .detail-media') && ` +
      `window.__activeMedia.__playbackSentinel === 17`), true);
  },
);

browserTest(
  'Chromium rejects stale generation and project-dialog responses',
  async browser => {
    const detailRequests = [];
    const metadataRequests = [];
    browser.addDiagnostic(
      'paused requests',
      () => `detail=${detailRequests.length} metadata=${metadataRequests.length}`,
    );
    browser.intercept(async params => {
      const url = new URL(params.request.url);
      const method = params.request.method;
      if (method === 'GET' && url.pathname.endsWith('/generations/001')) {
        detailRequests.push(params.requestId);
        return true;
      }
      if (method === 'PATCH' && /^\/api\/project\/[^/]+$/.test(url.pathname)) {
        metadataRequests.push(params.requestId);
        return true;
      }
      return false;
    });

    await browser.navigate();
    await browser.selectProject('Alpha');
    await browser.waitExpression(
      `document.querySelector('.generation-preview')`, 'generation card');
    await browser.evaluate(`document.querySelector('.generation-preview').click()`);
    await browser.waitFor(() => detailRequests.length === 1, 'first take request');
    await browser.evaluate(`document.querySelector('#generation-close').click()`);
    await browser.evaluate(`document.querySelector('.generation-preview').click()`);
    await browser.waitFor(() => detailRequests.length === 2, 'second take request');
    await browser.fulfill(detailRequests.pop(), detailPayload('new'));
    await browser.waitExpression(
      `document.querySelector('#generation-prompt')?.textContent === 'new prompt'`,
      'new take response');
    await browser.fulfill(detailRequests.pop(), detailPayload('old'));
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    assert.equal(await browser.evaluate(
      `document.querySelector('#generation-prompt').textContent`), 'new prompt');

    await browser.evaluate(`document.querySelector('#generation-close').click()`);
    await browser.evaluate(`document.querySelector('#edit-project').click()`);
    await browser.evaluate(`(() => {
      document.querySelector('#project-metadata-display-title').value = 'Stale Alpha';
      document.querySelector('#project-metadata-form').requestSubmit();
    })()`);
    await browser.waitFor(
      () => metadataRequests.length === 1, 'metadata save request');
    const alphaId = await browser.evaluate(
      `document.querySelector('.proj.active .proj-id').textContent`);
    await browser.selectProject('Beta');
    await browser.fulfill(metadataRequests.pop(), {
      project: {id: alphaId, title: 'Stale Alpha', brief: 'stale'},
    });
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    assert.equal(await browser.evaluate(
      `document.querySelector('.proj.active .proj-title').textContent`), 'Beta');
    assert.equal(await browser.evaluate(
      `document.querySelector('#project-metadata-dialog').open`), false);
  },
);

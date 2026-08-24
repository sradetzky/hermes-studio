import assert from 'node:assert/strict';
import test from 'node:test';

import {
  captureClipContext,
  captureProjectContext,
  isClipContextCurrent,
  isProjectContextCurrent,
  isSeedWithinRange,
  MAX_SAFE_SEED,
} from '../webapp/static/frontend-contracts.mjs';
import {apiPaths} from '../webapp/static/api-paths.mjs';
import {queuePresentation} from '../webapp/static/comfy-queue.mjs';
import {refreshLivePlane} from '../webapp/static/refresh-planes.mjs';
import {takeDeletionMessage} from '../webapp/static/media-review.js';
import {
  generationActionState,
  generationRequestPayload,
} from '../webapp/static/generation-settings.js';

test('seed text stays inside the exact JSON integer range', () => {
  assert.equal(isSeedWithinRange(''), true);
  assert.equal(isSeedWithinRange('0'), true);
  assert.equal(isSeedWithinRange('00042'), true);
  assert.equal(isSeedWithinRange(MAX_SAFE_SEED), true);
  assert.equal(isSeedWithinRange('9007199254740992'), false);
  assert.equal(isSeedWithinRange('4.2'), false);
  assert.equal(isSeedWithinRange('-1'), false);
});

test('project and clip contexts reject stale responses independently', () => {
  const state = {
    current: 'project-a', currentClip: 'clip-001',
    projectRevision: 3, clipRevision: 7,
  };
  const project = captureProjectContext(state);
  const clip = captureClipContext(state);
  assert.equal(isProjectContextCurrent(state, project), true);
  assert.equal(isClipContextCurrent(state, clip), true);

  state.currentClip = 'clip-002';
  state.clipRevision += 1;
  assert.equal(isProjectContextCurrent(state, project), true);
  assert.equal(isClipContextCurrent(state, clip), false);

  state.current = 'project-b';
  state.projectRevision += 1;
  assert.equal(isProjectContextCurrent(state, project), false);
});

test('API paths encode every external identifier once', () => {
  assert.equal(
    apiPaths.generationAction('project / one', 'clip-001', 'take #1', 'promote'),
    '/api/project/project%20%2F%20one/clips/clip-001/generations/take%20%231/promote',
  );
  assert.equal(
    apiPaths.generation('project / one', 'clip-001', 'take #1'),
    '/api/project/project%20%2F%20one/clips/clip-001/generations/take%20%231',
  );
  assert.equal(
    apiPaths.generate('project / one', 'clip #1'),
    '/api/project/project%20%2F%20one/clips/clip%20%231/generate',
  );
  assert.equal(apiPaths.chat('project', 42), '/api/project/project/chat?after=42');
  assert.equal(apiPaths.comfyQueue, '/api/comfyui/queue');
});

test('take deletion confirmation states irreversible scope and selected cleanup', () => {
  assert.equal(
    takeDeletionMessage('take-7', true),
    'Delete take take-7 and all of its archived files? This is the selected take; ' +
    'its selection will be cleared. Promoted final and reference copies are kept. ' +
    'This cannot be undone.',
  );
  assert.doesNotMatch(takeDeletionMessage('take-8', false), /selection will be cleared/);
});

test('generation action requires a ready current contract and idle enabled clip', () => {
  const contract = {
    readiness: {ready: true, reasons: []},
    manifest: {prompt_sha256: 'abc', updated_at: 'revision-1'},
  };
  assert.deepEqual(generationActionState(contract, true, false, false), {
    enabled: true, label: 'Generate with this prompt', reason: '',
  });
  assert.equal(generationActionState(contract, false, false, false).reason,
    'Enable this clip before generating');
  assert.equal(generationActionState(contract, true, true, false).reason,
    'Wait for the active Studio job to finish');
  assert.equal(generationActionState({
    readiness: {ready: false, reasons: ['Current prompt changed']},
    manifest: contract.manifest,
  }, true, false, false).reason, 'Current prompt changed');
  assert.deepEqual(generationRequestPayload(contract), {
    prompt_sha256: 'abc', settings_updated_at: 'revision-1',
  });
});

test('Comfy queue presentation distinguishes running, queued, idle, and offline', () => {
  assert.deepEqual(queuePresentation({
    available: true,
    running: [{prompt_id: 'running-id', position: 0}],
    pending: [{prompt_id: 'next-id', position: 1}],
  }), {state: 'running', label: 'Comfy 1 running · 1 queued'});
  assert.deepEqual(queuePresentation({
    available: true, running: [], pending: [{prompt_id: 'next-id', position: 1}],
  }), {state: 'queued', label: 'Comfy 1 queued'});
  assert.deepEqual(queuePresentation({available: true, running: [], pending: []}),
    {state: 'idle', label: 'Comfy idle'});
  assert.deepEqual(queuePresentation({available: false, running: [], pending: []}),
    {state: 'offline', label: 'Comfy unavailable'});
});

test('live refresh planes fail and apply independently', async () => {
  const applied = [];
  const reported = [];
  await refreshLivePlane({
    requestJson: async url => {
      if (url.includes('/chat?')) throw new Error('chat unavailable');
      if (url.includes('/jobs?')) return {jobs: ['job']};
      return {events: ['activity'], cursor: 12};
    },
    paths: apiPaths,
    context: {projectId: 'project'},
    cursors: {chat: 4, activity: 8},
    isCurrent: () => true,
    handlers: {
      chat: () => applied.push('chat'),
      jobs: value => applied.push(value.jobs[0]),
      activity: value => applied.push(value.events[0]),
    },
    report: (name, error) => reported.push([name, error?.message || null]),
  });

  assert.deepEqual(applied.sort(), ['activity', 'job']);
  assert.deepEqual(reported.sort(), [
    ['activity', null], ['chat', 'chat unavailable'], ['jobs', null],
  ]);
});

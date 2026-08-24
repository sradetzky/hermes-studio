import assert from 'node:assert/strict';
import test from 'node:test';

import {
  captureChatContext,
  captureClipContext,
  captureGenerationDialogContext,
  captureProjectDialogContext,
  captureProjectContext,
  isChatContextCurrent,
  isClipContextCurrent,
  isGenerationDialogContextCurrent,
  isProjectDialogContextCurrent,
  isProjectContextCurrent,
  isSeedWithinRange,
  MAX_SAFE_SEED,
} from '../webapp/static/frontend-contracts.mjs';
import {apiPaths} from '../webapp/static/api-paths.mjs';
import {
  formatQueueDuration,
  queueJobSpecs,
  queueJobTitle,
  queuePresentation,
} from '../webapp/static/comfy-queue.mjs';
import {refreshLivePlane} from '../webapp/static/refresh-planes.mjs';
import {
  movieExportState,
  takeDeletionMessage,
} from '../webapp/static/media-review.js';
import {
  generationActionState,
  generationRequestPayload,
} from '../webapp/static/generation-settings.js';
import {
  moveWorkspacePane,
  normalizeWorkspacePane,
  WORKSPACE_PANES,
} from '../webapp/static/workspace-panes.mjs';

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

test('chat context rejects scope and active-clip changes independently', () => {
  const state = {
    current: 'project-a', currentClip: 'clip-001', chatScope: 'clip',
    projectRevision: 3, clipRevision: 7, chatRevision: 2,
    chatRequestRevision: 0,
  };
  const clipChat = captureChatContext(state);
  assert.equal(isChatContextCurrent(state, clipChat), true);

  state.chatScope = 'project';
  state.chatRevision += 1;
  assert.equal(isChatContextCurrent(state, clipChat), false);

  const projectChat = captureChatContext(state);
  state.currentClip = 'clip-002';
  state.clipRevision += 1;
  assert.equal(isChatContextCurrent(state, projectChat), true);

  state.chatScope = 'clip';
  state.chatRevision += 1;
  const nextClipChat = captureChatContext(state);
  state.currentClip = 'clip-003';
  state.clipRevision += 1;
  assert.equal(isChatContextCurrent(state, nextClipChat), false);

  state.currentClip = 'clip-002';
  state.clipRevision = nextClipChat.clipRevision;
  const request = captureChatContext(state);
  state.chatRequestRevision += 1;
  assert.equal(isChatContextCurrent(state, request), false);
});

test('dialog contexts reject close-reopen and same-clip take navigation', () => {
  const state = {
    current: 'project-a', currentClip: 'clip-001',
    projectRevision: 3, clipRevision: 7,
    projectMetadataDialogRevision: 1,
    generationDialogRevision: 4,
  };
  const projectDialog = captureProjectDialogContext(state);
  const takeDialog = captureGenerationDialogContext(state, 'take-001');
  assert.equal(isProjectDialogContextCurrent(state, projectDialog), true);
  assert.equal(isGenerationDialogContextCurrent(state, takeDialog), true);

  state.projectMetadataDialogRevision += 1;
  assert.equal(isProjectDialogContextCurrent(state, projectDialog), false);

  state.generationDialogRevision += 1;
  const nextTake = captureGenerationDialogContext(state, 'take-002');
  assert.equal(isGenerationDialogContextCurrent(state, takeDialog), false);
  assert.equal(isGenerationDialogContextCurrent(state, nextTake), true);
  assert.equal(nextTake.generationId, 'take-002');
});

test('responsive workspace navigation defaults to chat and wraps predictably', () => {
  assert.deepEqual(WORKSPACE_PANES, ['projects', 'chat', 'media']);
  assert.equal(normalizeWorkspacePane('projects'), 'projects');
  assert.equal(normalizeWorkspacePane('unknown'), 'chat');
  assert.equal(moveWorkspacePane('projects', -1), 'media');
  assert.equal(moveWorkspacePane('media', 1), 'projects');
  assert.equal(moveWorkspacePane('chat', 1), 'media');
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
  assert.equal(apiPaths.projectChat('project / one'),
    '/api/project/project%20%2F%20one/chat');
  assert.equal(apiPaths.clipChat('project / one', 'clip #1', 7),
    '/api/project/project%20%2F%20one/clips/clip%20%231/chat?after=7');
  assert.equal(apiPaths.clipEvents('project / one', 'clip #1', 9),
    '/api/project/project%20%2F%20one/clips/clip%20%231/events?after=9');
  assert.equal(apiPaths.comfyQueue, '/api/comfyui/queue');
  assert.equal(apiPaths.project('project / one'), '/api/project/project%20%2F%20one');
  assert.equal(apiPaths.movie('project / one'),
    '/api/project/project%20%2F%20one/movie');
});

test('movie export action requires readiness and explicit idle ownership', () => {
  assert.deepEqual(movieExportState(null, false, false), {
    enabled: false, label: 'Export selected takes as movie',
    status: 'Movie readiness unavailable',
  });
  assert.equal(movieExportState({
    readiness: {ready: false, enabled_clip_count: 2, blocking: [
      {title: 'Clip 2', reason: 'Select a video take'},
    ]}, movies: [],
  }, false, false).status, 'Blocked: Clip 2 — Select a video take');
  assert.deepEqual(movieExportState({
    readiness: {ready: true, enabled_clip_count: 2, blocking: []}, movies: [],
  }, true, false), {
    enabled: false, label: 'Export selected takes as movie',
    status: 'Wait for the active Studio job to finish',
  });
  assert.deepEqual(movieExportState({
    readiness: {ready: true, enabled_clip_count: 2, blocking: []}, movies: [],
  }, false, false), {
    enabled: true, label: 'Export selected takes as movie',
    status: 'Ready · 2 selected clips · hard cuts',
  });
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
    running: [{
      prompt_id: 'running-id', position: 0, recipe: 'H3', mode: 'R2V',
      elapsed_seconds: 368,
    }],
    pending: [{prompt_id: 'next-id', position: 1}],
  }), {state: 'running', label: 'Comfy · H3 R2V · 6:08 · 1 queued'});
  assert.deepEqual(queuePresentation({
    available: true, running: [], pending: [{prompt_id: 'next-id', position: 1}],
  }), {state: 'queued', label: 'Comfy 1 queued'});
  assert.deepEqual(queuePresentation({available: true, running: [], pending: []}),
    {state: 'idle', label: 'Comfy idle'});
  assert.deepEqual(queuePresentation({available: false, running: [], pending: []}),
    {state: 'offline', label: 'Comfy unavailable'});
});

test('Comfy queue details format sanitized render metadata and timing', () => {
  const h3 = {
    recipe: 'H3', mode: 'R2V', width: 928, height: 544,
    media_seconds: 10, frames: 243, steps: 8, accel: true,
  };
  assert.equal(queueJobTitle(h3), 'H3 R2V');
  assert.equal(queueJobSpecs(h3), '928×544 · ~10s / 243f · 8 steps · accel');
  assert.equal(queueJobTitle({}), 'Comfy workflow');
  assert.equal(formatQueueDuration(0), '0:00');
  assert.equal(formatQueueDuration(582.274), '9:42');
  assert.equal(formatQueueDuration(3661), '1:01:01');
});

test('live refresh planes fail and apply independently', async () => {
  const applied = [];
  const reported = [];
  const requested = [];
  await refreshLivePlane({
    requestJson: async url => {
      requested.push(url);
      if (url.includes('/chat?')) throw new Error('chat unavailable');
      if (url.includes('/jobs?')) return {jobs: ['job']};
      return {events: ['activity'], cursor: 12};
    },
    paths: apiPaths,
    context: {projectId: 'project', chatScope: 'clip', clipId: 'clip-001'},
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
  assert.ok(requested.includes('/api/project/project/clips/clip-001/chat?after=4'));
  assert.ok(requested.includes('/api/project/project/clips/clip-001/events?after=8'));
});

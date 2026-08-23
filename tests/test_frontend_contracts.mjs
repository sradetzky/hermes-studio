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
import {refreshLivePlane} from '../webapp/static/refresh-planes.mjs';

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
  assert.equal(apiPaths.chat('project', 42), '/api/project/project/chat?after=42');
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

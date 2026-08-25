import assert from 'node:assert/strict';

import {browserTest} from './frontend_browser_harness.mjs';

browserTest(
  'Chromium preserves conversation viewport and stable nodes during deferred updates',
  async browser => {
    const chatRequests = [];
    const jobRequests = [];
    const activityRequests = [];
    let deferConversation = false;
    browser.addDiagnostic(
      'paused conversation requests',
      () => `${chatRequests.length}/${jobRequests.length}/${activityRequests.length}`,
    );
    browser.intercept(async params => {
      if (!deferConversation || params.request.method !== 'GET') return false;
      const url = new URL(params.request.url);
      if (url.pathname.endsWith('/chat')) {
        chatRequests.push(params.requestId);
        return true;
      }
      if (url.pathname.endsWith('/jobs')) {
        jobRequests.push(params.requestId);
        return true;
      }
      if (url.pathname.endsWith('/events')) {
        activityRequests.push(params.requestId);
        return true;
      }
      return false;
    });

    await browser.navigate();
    await browser.selectProject('Alpha');
    await browser.waitExpression(
      `document.querySelector('.generation-preview')`, 'initial project refresh');
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
    deferConversation = true;
    await browser.evaluate(`void import('/static/conversation-controller.js').then(
      module => {
        module.resetConversation('project');
        return module.refreshConversation(() => {});
      })`);
    await browser.waitFor(
      () => chatRequests.length === 1 && jobRequests.length === 1 &&
        activityRequests.length === 1,
      'initial deferred conversation',
    );

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
    await browser.fulfill(chatRequests.pop(), {
      messages: initialMessages, cursor: 40,
    });
    await browser.fulfill(jobRequests.pop(), {jobs: [{
      id: 'job-scroll', status: 'running', profile: 'studio', message: 'scroll test',
    }]});
    await browser.fulfill(activityRequests.pop(), {
      events: initialEvents, cursor: 36,
    });
    await browser.waitExpression(
      `document.querySelector('#chatlog').textContent.includes('initial latest output')`,
      'initial conversation output');
    const initialViewport = await browser.evaluate(`(() => {
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

    await browser.evaluate(`(() => {
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
    })()`);
    await browser.evaluate(`void import('/static/conversation-controller.js').then(
      module => module.refreshConversation(() => {}))`);
    await browser.waitFor(
      () => chatRequests.length === 1 && jobRequests.length === 1 &&
        activityRequests.length === 1,
      'incremental deferred conversation',
    );
    await browser.fulfill(chatRequests.pop(), {
      messages: [{
        id: 41, role: 'assistant', profile: 'studio',
        content: 'output while scrolled away', job_id: 'job-scroll',
      }],
      cursor: 41,
    });
    await browser.fulfill(jobRequests.pop(), {jobs: [{
      id: 'job-scroll', status: 'completed', profile: 'studio', message: 'scroll test',
    }]});
    await browser.fulfill(activityRequests.pop(), {
      events: [{
        id: 37, job_id: 'job-scroll', event_type: 'job.completed',
        status: 'completed', profile: 'studio', summary: 'Completed', detail: {},
      }],
      cursor: 37,
    });
    await browser.waitExpression(
      `document.querySelector('#chatlog').textContent.includes('output while scrolled away')`,
      'scrolled-away output');
    assert.deepEqual(await browser.evaluate(`(() => {
      const chat = document.querySelector('#chatlog');
      const card = document.querySelector('[data-activity-job="job-scroll"]');
      const list = card.querySelector('.job-event-list');
      return {
        chatTop: chat.scrollTop,
        listTop: list.scrollTop,
        cardStable: card === window.__stableCard,
        eventStable: card.querySelector('[data-activity-event="2"]') ===
          window.__stableEvent,
        reasoningStable: card.querySelector('[data-event-detail="2"]') ===
          window.__stableReasoning,
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

    await browser.evaluate(`(() => {
      const chat = document.querySelector('#chatlog');
      const list = document.querySelector('.job-event-list');
      chat.scrollTop = chat.scrollHeight;
      list.scrollTop = list.scrollHeight;
    })()`);
    await browser.evaluate(`void import('/static/conversation-controller.js').then(
      module => module.refreshConversation(() => {}))`);
    await browser.waitFor(
      () => chatRequests.length === 1 && jobRequests.length === 1 &&
        activityRequests.length === 1,
      'follow-latest deferred conversation',
    );
    await browser.fulfill(chatRequests.pop(), {
      messages: [{
        id: 42, role: 'assistant', profile: 'studio',
        content: 'followed latest output', job_id: 'job-scroll',
      }],
      cursor: 42,
    });
    await browser.fulfill(jobRequests.pop(), {jobs: []});
    await browser.fulfill(activityRequests.pop(), {
      events: [{
        id: 38, job_id: 'job-scroll', event_type: 'commentary',
        status: 'completed', profile: 'studio', summary: 'Final note',
        detail: {text: 'final note'},
      }],
      cursor: 38,
    });
    await browser.waitExpression(
      `document.querySelector('#chatlog').textContent.includes('followed latest output')`,
      'followed output');
    assert.equal(await browser.evaluate(`(() => {
      const chat = document.querySelector('#chatlog');
      const list = document.querySelector('.job-event-list');
      return chat.scrollHeight - chat.scrollTop - chat.clientHeight <= 1 &&
        list.scrollHeight - list.scrollTop - list.clientHeight <= 1;
    })()`), true);
  },
);

browserTest(
  'Chromium restores and answers durable clarify after reload',
  async browser => {
    let postBody = null;
    let interaction = {
      id: 'interaction-browser',
      revision: 1,
      job_id: 'job-clarify',
      project: 'alpha',
      clip_id: 'clip-001',
      chat_scope: 'clip',
      profile: 'studio',
      status: 'pending',
      batch: true,
      questions: [
        {id: 'pick', question: 'Pick one', choices: ['One', 'Two'],
          multi_select: false},
        {id: 'traits', question: 'Pick traits', choices: ['Warm', 'Fast'],
          multi_select: true},
        {id: 'notes', question: 'Notes?', choices: [], multi_select: false},
      ],
      created_at: '2026-08-25T00:00:00Z',
      answered_at: '',
    };
    browser.intercept(async params => {
      const url = new URL(params.request.url);
      if (!url.pathname.endsWith('/interaction') &&
          !url.pathname.includes('/interaction/')) return false;
      if (params.request.method === 'GET') {
        await browser.fulfill(params.requestId, {interaction});
        return true;
      }
      if (params.request.method === 'POST') {
        postBody = JSON.parse(params.request.postData);
        interaction = {...interaction, revision: 2, status: 'answered',
          answers: postBody.answers, answered_at: '2026-08-25T00:01:00Z'};
        await browser.fulfill(params.requestId, {interaction});
        return true;
      }
      return false;
    });

    await browser.navigate();
    await browser.selectProject('Alpha');
    await browser.waitExpression(
      `document.querySelector('#interaction-panel:not([hidden])')?.dataset.signature ===
        'interaction-browser:1:pending'`,
      'initial pending clarify');
    await browser.client.send('Page.reload');
    await browser.waitExpression(
      `document.querySelectorAll('.proj').length === 2`, 'project list after reload');
    await browser.selectProject('Alpha');
    await browser.waitExpression(
      `document.querySelector('#interaction-panel:not([hidden])')?.dataset.signature ===
        'interaction-browser:1:pending'`,
      'restored pending clarify');

    await browser.evaluate(`(() => {
      const panel = document.querySelector('#interaction-panel');
      panel.querySelector('[data-question-id="pick"] [data-choice][value="One"]').click();
      panel.querySelector('[data-question-id="traits"] [data-choice][value="Warm"]').click();
      panel.querySelector('[data-question-id="traits"] [data-other-text]').value = 'Custom';
      panel.querySelector('[data-question-id="notes"] [data-free-text]').value = 'No music';
      panel.querySelector('button[type="submit"]').click();
    })()`);
    await browser.waitFor(() => postBody !== null, 'clarify answer POST');
    assert.deepEqual(postBody, {
      revision: 1,
      answers: {pick: 'One', traits: ['Warm', 'Custom'], notes: 'No music'},
    });
    await browser.waitExpression(
      `document.querySelector('#interaction-panel')?.dataset.signature ===
        'interaction-browser:2:answered' &&
       document.querySelector('#interaction-panel')?.textContent.includes('Studio is continuing')`,
      'answered clarify state');
  },
);

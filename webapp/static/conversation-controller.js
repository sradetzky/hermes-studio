import {apiPaths} from './api-paths.mjs';
import {
  captureChatContext,
  isChatContextCurrent,
} from './frontend-contracts.mjs';
import {refreshLivePlane} from './refresh-planes.mjs';
import {
  clearInteractionPanel,
  renderInteractionPanel,
} from './interaction-form.mjs';
import {$, activeClip, requestJson, showEmpty, state} from './shared.js';

const FOLLOW_END_THRESHOLD = 80;

const conversation = {
  scope: 'clip',
  revision: 0,
  requestRevision: 0,
  chatCursor: 0,
  activityCursor: 0,
  activityByJob: new Map(),
  jobs: [],
  interaction: null,
  jobActive: false,
  showActivityDetails: true,
  initialLoad: true,
};

let refreshProject = async () => {};
let jobsChanged = () => {};
let initialized = false;

function contextState() {
  return {
    ...state,
    chatScope: conversation.scope,
    chatRevision: conversation.revision,
    chatRequestRevision: conversation.requestRevision,
  };
}

function captureContext() {
  return captureChatContext(contextState());
}

function isContextCurrent(context) {
  return isChatContextCurrent(contextState(), context);
}

function captureViewport(element) {
  return {
    following: conversation.initialLoad ||
      element.scrollHeight - element.scrollTop - element.clientHeight < FOLLOW_END_THRESHOLD,
    scrollTop: element.scrollTop,
  };
}

function restoreViewport(element, viewport) {
  element.scrollTop = viewport.following ? element.scrollHeight : viewport.scrollTop;
}

function activityCard(jobId) {
  return $('#chatlog').querySelector(
    `[data-activity-job="${CSS.escape(String(jobId))}"]`);
}

function renderScope() {
  const clip = activeClip();
  const clipScoped = conversation.scope === 'clip';
  const clipTab = $('#clip-chat-scope');
  const projectTab = $('#project-chat-scope');
  clipTab.disabled = !clip;
  clipTab.textContent = clip ? `Clip · ${clip.title}` : 'Clip';
  clipTab.classList.toggle('active', clipScoped);
  clipTab.setAttribute('aria-selected', String(clipScoped));
  projectTab.classList.toggle('active', !clipScoped);
  projectTab.setAttribute('aria-selected', String(!clipScoped));
  $('#chat-scope-help').textContent = clipScoped
    ? `Independent conversation for ${clip?.id || 'this clip'}`
    : 'Project-wide direction and cross-clip continuity';
  $('#active-clip-label').textContent = clipScoped
    ? (clip ? `Clip chat · ${clip.id}` : 'Clip chat')
    : 'Project chat';
  $('#active-clip-label').title = clipScoped ? (clip?.title || '')
    : 'Project-wide conversation';
  $('#chatinput').placeholder = clipScoped
    ? 'Message this clip agent…' : 'Message the project agent…';
  const unavailable = !state.current || (clipScoped && !clip);
  $('#chatinput').disabled = conversation.jobActive || unavailable;
  $('#send-button').disabled = conversation.jobActive || unavailable;
  $('#profile-select').disabled = conversation.jobActive || unavailable;
}

function renderJobs(jobs) {
  conversation.jobs = jobs;
  const latest = jobs[0];
  const active = jobs.find(job => job.status === 'queued' || job.status === 'running');
  conversation.jobActive = Boolean(active);
  const activityState = active && conversation.interaction
    ? 'waiting_for_user' : (active?.status || latest?.status || 'idle');
  $('#activity-dot').className =
    `activity-dot ${activityState === 'idle' ? '' : activityState}`;
  const labels = {
    idle: 'Idle',
    queued: 'Studio queued',
    running: 'Studio working…',
    waiting_for_user: 'Studio is waiting for you',
    completed: 'Last job completed',
    failed: 'Last job failed',
  };
  $('#activity-text').textContent = labels[activityState] || activityState;
  $('#activity').title = latest?.error || latest?.message || 'Studio activity';
  renderScope();
  jobsChanged(conversation.jobActive);
}

export function resetConversation(scope = conversation.scope) {
  conversation.scope = scope;
  conversation.revision += 1;
  conversation.requestRevision += 1;
  conversation.chatCursor = 0;
  conversation.activityCursor = 0;
  conversation.activityByJob.clear();
  conversation.interaction = null;
  conversation.initialLoad = true;
  const chat = $('#chatlog');
  chat.replaceChildren();
  delete chat.dataset.empty;
  $('#status').textContent = '';
  clearInteractionPanel();
  renderJobs([]);
}

export async function switchConversationScope(scope) {
  if (scope === conversation.scope ||
      (scope === 'clip' && !activeClip())) return;
  resetConversation(scope);
  await refreshProject();
}

export function ensureConversationScope() {
  if (!state.currentClip && conversation.scope === 'clip') {
    resetConversation('project');
    return true;
  }
  renderScope();
  return false;
}

export function conversationScope() {
  return conversation.scope;
}

export function conversationJobActive() {
  return conversation.jobActive;
}

export function queueConversationJob(job) {
  renderJobs([job, ...conversation.jobs.filter(item => item.id !== job.id)]);
}

function appendMessages(messages) {
  const chat = $('#chatlog');
  const viewport = captureViewport(chat);
  if (!messages.length && conversation.chatCursor === 0 && !chat.children.length) {
    showEmpty(chat, conversation.scope === 'clip'
      ? 'Start this clip conversation…'
      : 'Project history and cross-clip direction live here…');
    restoreViewport(chat, viewport);
    return;
  }
  if (messages.length && chat.dataset.empty) {
    chat.replaceChildren();
    delete chat.dataset.empty;
  }
  for (const message of messages) {
    const row = document.createElement('div');
    row.className = 'chat-row';
    if (message.job_id) row.dataset.jobId = message.job_id;
    const role = document.createElement('span');
    role.className = `role-${message.role} font-semibold`;
    role.textContent = message.role === 'user' ? 'you' : (message.profile || message.role);
    row.append(role, document.createTextNode(` ${message.content || ''}`));
    chat.append(row);
    if (message.job_id && message.role === 'user') {
      ensureActivityCard(message.job_id, row);
    }
  }
  restoreViewport(chat, viewport);
}

function ensureActivityCard(jobId, afterRow = null) {
  const chat = $('#chatlog');
  let card = activityCard(jobId);
  if (!card) {
    card = document.createElement('details');
    card.className = 'job-activity';
    card.dataset.activityJob = jobId;
    card.hidden = true;

    const summary = document.createElement('summary');
    summary.className = 'job-activity-summary';
    const profile = document.createElement('span');
    profile.className = 'profile-badge';
    const status = document.createElement('span');
    status.className = 'job-activity-status';
    summary.append(profile, status);

    const list = document.createElement('div');
    list.className = 'job-event-list';
    card.append(summary, list);
    chat.append(card);
  }
  if (afterRow && afterRow.nextElementSibling !== card) afterRow.after(card);
  return card;
}

function compactActivityEvents(events) {
  const pending = new Map();
  const pairedStarts = new Set();
  for (const event of events) {
    if (event.event_type === 'tool.started') {
      const key = `${event.profile}:${event.detail?.tool || event.summary}`;
      const queue = pending.get(key) || [];
      queue.push(event.id);
      pending.set(key, queue);
    } else if (event.event_type === 'tool.completed') {
      const key = `${event.profile}:${event.detail?.tool || event.summary}`;
      const queue = pending.get(key) || [];
      const started = queue.shift();
      if (started !== undefined) pairedStarts.add(started);
      pending.set(key, queue);
    }
  }
  return events.filter(event => !pairedStarts.has(event.id));
}

function createEventRow(event) {
  const item = document.createElement('div');
  item.dataset.activityEvent = event.id;
  item.className = `job-event ${event.status || ''}`;
  const badge = document.createElement('span');
  badge.className = 'profile-badge compact';
  badge.textContent = event.profile;
  const icon = document.createElement('span');
  icon.className = 'job-event-icon';
  icon.textContent = event.event_type === 'reasoning' ? '◇' :
    event.status === 'failed' ? '×' :
    event.event_type === 'tool.started' ? '…' : '✓';
  if (event.event_type === 'reasoning' || event.event_type === 'commentary') {
    const reasoning = document.createElement('details');
    reasoning.className = 'reasoning-event';
    reasoning.dataset.eventDetail = event.id;
    const heading = document.createElement('summary');
    heading.textContent = event.event_type === 'reasoning' ? 'Thinking' : 'Commentary';
    const text = document.createElement('pre');
    text.textContent = event.detail?.text || event.summary;
    reasoning.append(heading, text);
    item.append(icon, badge, reasoning);
  } else {
    const text = document.createElement('span');
    text.className = 'job-event-text';
    text.textContent = event.summary;
    item.append(icon, badge, text);
    if (event.detail?.duration !== undefined) {
      const duration = document.createElement('span');
      duration.className = 'job-event-duration';
      duration.textContent = `${event.detail.duration}s`;
      item.append(duration);
    }
  }
  return item;
}

function renderJobActivity(jobId) {
  const card = ensureActivityCard(jobId);
  const events = conversation.activityByJob.get(jobId) || [];
  if (!events.length) {
    card.hidden = true;
    return;
  }
  const wasHidden = card.hidden;
  const latestState = [...events].reverse().find(event =>
    ['queued', 'running', 'waiting_for_user', 'completed', 'failed']
      .includes(event.status));
  const status = latestState?.status || 'running';
  const profiles = [...new Set(events.map(event => event.profile).filter(Boolean))];
  const profile = card.querySelector('.job-activity-summary .profile-badge');
  const statusText = card.querySelector('.job-activity-status');
  profile.textContent = profiles.join(' → ') || 'studio';
  statusText.className = `job-activity-status ${status}`;
  statusText.textContent = status === 'running' ? 'Working…' :
    status === 'waiting_for_user' ? 'Waiting for you' : status;
  if (wasHidden) {
    card.hidden = false;
    card.open = ['running', 'queued', 'waiting_for_user'].includes(status);
  }

  let visible = compactActivityEvents(events);
  if (!conversation.showActivityDetails) {
    visible = visible.filter(event =>
      !event.event_type.startsWith('tool.') || event.event_type === 'tool.started');
  }
  const list = card.querySelector('.job-event-list');
  const viewport = captureViewport(list);
  const existing = new Map(
    [...list.querySelectorAll('[data-activity-event]')].map(
      row => [row.dataset.activityEvent, row]));
  let cursor = list.firstElementChild;
  for (const event of visible) {
    const key = String(event.id);
    let row = existing.get(key);
    if (!row) row = createEventRow(event);
    existing.delete(key);
    if (row === cursor) cursor = cursor.nextElementSibling;
    else list.insertBefore(row, cursor);
  }
  for (const row of existing.values()) row.remove();
  restoreViewport(list, viewport);
}

function appendActivities(events) {
  if (!events.length) return;
  const chat = $('#chatlog');
  const viewport = captureViewport(chat);
  const changed = new Set();
  for (const event of events) {
    const group = conversation.activityByJob.get(event.job_id) || [];
    if (!group.some(existing => existing.id === event.id)) group.push(event);
    conversation.activityByJob.set(event.job_id, group);
    changed.add(event.job_id);
  }
  for (const jobId of changed) renderJobActivity(jobId);
  restoreViewport(chat, viewport);
}

async function answerInteraction(interaction, answers) {
  const context = captureContext();
  const clipScoped = conversation.scope === 'clip';
  const path = clipScoped
    ? apiPaths.clipInteractionAnswer(
        context.projectId, context.clipId, interaction.id)
    : apiPaths.projectInteractionAnswer(context.projectId, interaction.id);
  const result = await requestJson(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({revision: interaction.revision, answers}),
  });
  if (!isContextCurrent(context) ||
      conversation.interaction?.id !== interaction.id ||
      conversation.interaction?.revision !== interaction.revision) return;
  renderInteraction(result.interaction);
  await refreshProject();
}

function renderInteraction(interaction) {
  conversation.interaction = interaction;
  renderInteractionPanel(
    interaction,
    answers => answerInteraction(interaction, answers));
  renderJobs(conversation.jobs);
}

export async function refreshConversation(report) {
  const context = captureContext();
  try {
    await refreshLivePlane({
      requestJson,
      paths: apiPaths,
      context,
      cursors: {
        chat: conversation.chatCursor,
        activity: conversation.activityCursor,
      },
      isCurrent: () => isContextCurrent(context),
      handlers: {
        chat: chat => {
          appendMessages(chat.messages);
          conversation.chatCursor = chat.cursor;
        },
        jobs: jobs => renderJobs(jobs.jobs),
        interaction: result => renderInteraction(result.interaction),
        activity: activity => {
          appendActivities(activity.events);
          conversation.activityCursor = activity.cursor;
        },
      },
      report,
    });
  } finally {
    if (isContextCurrent(context)) conversation.initialLoad = false;
  }
}

async function sendChat(event) {
  event.preventDefault();
  const input = $('#chatinput');
  const message = input.value.trim();
  const clipScoped = conversation.scope === 'clip';
  if (!message || !state.current || (clipScoped && !state.currentClip)) {
    alert(clipScoped ? 'Pick a project and clip first' : 'Pick a project first');
    return;
  }
  conversation.requestRevision += 1;
  const context = captureContext();
  input.value = '';
  $('#status').textContent = 'studio is thinking…';
  try {
    const job = await requestJson(
      clipScoped
        ? apiPaths.clipChat(context.projectId, context.clipId)
        : apiPaths.projectChat(context.projectId), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message, profile: $('#profile-select').value || 'studio'}),
      });
    if (!isContextCurrent(context)) return;
    $('#status').textContent = '';
    queueConversationJob(job);
    await refreshProject();
  } catch (error) {
    if (isContextCurrent(context)) {
      if (!input.value) input.value = message;
      $('#status').textContent = `error: ${error.message}`;
    }
  }
}

function toggleActivityDetails() {
  conversation.showActivityDetails = !conversation.showActivityDetails;
  const toggle = $('#activity-detail-toggle');
  toggle.textContent = conversation.showActivityDetails ? 'Details on' : 'Details off';
  toggle.setAttribute('aria-pressed', String(conversation.showActivityDetails));
  const chat = $('#chatlog');
  const viewport = captureViewport(chat);
  for (const jobId of conversation.activityByJob.keys()) renderJobActivity(jobId);
  restoreViewport(chat, viewport);
}

export function initializeConversationController(options) {
  refreshProject = options.refreshProject;
  jobsChanged = options.jobsChanged;
  if (initialized) return;
  initialized = true;
  $('#clip-chat-scope').addEventListener(
    'click', () => switchConversationScope('clip'));
  $('#project-chat-scope').addEventListener(
    'click', () => switchConversationScope('project'));
  $('#chat-form').addEventListener('submit', sendChat);
  $('#activity-detail-toggle').addEventListener('click', toggleActivityDetails);
  renderScope();
}

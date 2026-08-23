import {
  closeGenerationSettings,
  initializeGenerationSettings,
  renderGenerationReadiness,
} from './generation-settings.js';
import {
  closeGenerationDialog,
  initializeMediaReview,
  renderGenerations,
  updateGenerationRecipeFilter,
} from './media-review.js';
import {$, requestJson, showEmpty, state} from './shared.js';

async function loadProfiles() {
  const data = await requestJson('/api/profiles');
  state.profiles = data.profiles;
  const select = $('#profile-select');
  select.replaceChildren();
  for (const profile of state.profiles) {
    const option = document.createElement('option');
    option.value = profile.id;
    option.textContent = profile.label;
    select.append(option);
  }
}


async function loadProjects() {
  const data = await requestJson('/api/projects');
  state.projects = data.projects;
  renderProjects();
  if (!state.current && state.projects.length) {
    await selectProject(state.projects[0].id);
  }
}

function renderProjects() {
  const navigation = $('#projects');
  navigation.replaceChildren();
  if (!state.projects.length) {
    showEmpty(navigation, 'no projects yet');
    return;
  }
  for (const project of state.projects) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = `proj ${project.id === state.current ? 'active' : ''}`;
    item.textContent = project.id;
    item.addEventListener('click', () => selectProject(project.id));
    navigation.append(item);
  }
}

async function selectProject(projectId) {
  closeGenerationDialog(false);
  closeGenerationSettings(false);
  state.current = projectId;
  state.chatCount = 0;
  state.activityCursor = 0;
  state.activityByJob = {};
  state.generations = [];
  state.filteredGenerations = [];
  state.generationDetail = null;
  state.selectedGenerationFile = null;
  state.generationSettings = null;
  state.generationSettingsOptions = null;
  state.settingsReferences = [];
  state.generationSignature = '';
  state.referenceSignature = '';
  $('#chatlog').replaceChildren();
  delete $('#chatlog').dataset.empty;
  $('#gens').replaceChildren();
  $('#refs').replaceChildren();
  renderActivity([]);
  renderProjects();
  await refreshProject();
}

async function createProject() {
  const name = prompt('Project name:');
  if (!name) return;
  const brief = prompt('Brief (optional):') || '';
  const created = await requestJson('/api/projects', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, brief}),
  });
  state.current = created.id;
  await loadProjects();
  await selectProject(created.id);
}

async function refreshProject() {
  if (!state.current) return;
  if (state.refreshing) {
    state.refreshPending = true;
    return;
  }
  state.refreshing = true;
  const selected = state.current;
  const projectId = encodeURIComponent(selected);
  try {
    const [project, chat, generations, references, jobs, activity] = await Promise.all([
      requestJson(`/api/project/${projectId}`),
      requestJson(`/api/project/${projectId}/chat?after=${state.chatCount}`),
      requestJson(`/api/project/${projectId}/generations`),
      requestJson(`/api/project/${projectId}/references`),
      requestJson(`/api/project/${projectId}/jobs?limit=5`),
      requestJson(`/api/project/${projectId}/events?after=${state.activityCursor}`),
    ]);
    if (selected !== state.current) {
      state.refreshPending = true;
      return;
    }
    $('#prompt').textContent = project.current_prompt || '—';
    renderGenerationReadiness(project.generation_settings);
    document.title = `${project.id} — Hermes Studio`;
    if (chat.total < state.chatCount) {
      state.chatCount = 0;
      $('#chatlog').replaceChildren();
      const all = await requestJson(`/api/project/${projectId}/chat`);
      appendMessages(all.messages);
      state.chatCount = all.total;
    } else {
      appendMessages(chat.messages);
      state.chatCount = chat.total;
    }
    appendActivities(activity.events);
    state.activityCursor = activity.cursor;
    const generationSignature = JSON.stringify(generations.generations);
    if (generationSignature !== state.generationSignature) {
      state.generationSignature = generationSignature;
      state.generations = generations.generations;
      updateGenerationRecipeFilter();
      renderGenerations();
    }
    const referenceSignature = JSON.stringify(references.references);
    if (referenceSignature !== state.referenceSignature) {
      state.referenceSignature = referenceSignature;
      renderReferences(references.references);
    }
    renderActivity(jobs.jobs);
  } catch (error) {
    $('#status').textContent = `refresh error: ${error.message}`;
  } finally {
    state.refreshing = false;
    if (state.refreshPending) {
      state.refreshPending = false;
      queueMicrotask(refreshProject);
    }
  }
}

function appendMessages(messages) {
  const chat = $('#chatlog');
  const shouldScroll = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
  if (!messages.length && state.chatCount === 0 && !chat.children.length) {
    showEmpty(chat, 'Say hello to start planning…');
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
  if (messages.length && shouldScroll) chat.scrollTop = chat.scrollHeight;
}

function ensureActivityCard(jobId, afterRow = null) {
  const chat = $('#chatlog');
  let card = chat.querySelector(`[data-activity-job="${jobId}"]`);
  if (!card) {
    card = document.createElement('details');
    card.className = 'job-activity';
    card.dataset.activityJob = jobId;
    card.hidden = true;
    chat.append(card);
  }
  if (afterRow && afterRow.nextElementSibling !== card) afterRow.after(card);
  renderJobActivity(jobId);
  return card;
}

function appendActivities(events) {
  if (!events.length) return;
  const chat = $('#chatlog');
  const shouldScroll = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
  const changed = new Set();
  for (const event of events) {
    const group = state.activityByJob[event.job_id] || [];
    if (!group.some(existing => existing.id === event.id)) group.push(event);
    state.activityByJob[event.job_id] = group;
    changed.add(event.job_id);
  }
  for (const jobId of changed) {
    ensureActivityCard(jobId);
    renderJobActivity(jobId);
  }
  if (shouldScroll) chat.scrollTop = chat.scrollHeight;
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

function renderJobActivity(jobId) {
  const card = $('#chatlog').querySelector(`[data-activity-job="${jobId}"]`);
  if (!card) return;
  const events = state.activityByJob[jobId] || [];
  if (!events.length) {
    card.hidden = true;
    return;
  }
  const previousOpen = card.open;
  const lifecycle = [...events].reverse().find(event =>
    event.event_type.startsWith('job.'));
  const status = lifecycle?.status || 'running';
  const profiles = [...new Set(events.map(event => event.profile).filter(Boolean))];
  const summary = document.createElement('summary');
  summary.className = 'job-activity-summary';
  const profile = document.createElement('span');
  profile.className = 'profile-badge';
  profile.textContent = profiles.join(' → ') || 'studio';
  const statusText = document.createElement('span');
  statusText.className = `job-activity-status ${status}`;
  statusText.textContent = status === 'running' ? 'Working…' : status;
  summary.append(profile, statusText);

  const list = document.createElement('div');
  list.className = 'job-event-list';
  let visible = compactActivityEvents(events);
  if (!state.showActivityDetails) {
    visible = visible.filter(event =>
      !event.event_type.startsWith('tool.') || event.event_type === 'tool.started');
  }
  for (const event of visible) {
    const item = document.createElement('div');
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
    list.append(item);
  }
  card.replaceChildren(summary, list);
  card.hidden = false;
  card.open = status === 'running' || status === 'queued' || previousOpen;
}



function renderReferences(references) {
  const container = $('#refs');
  container.replaceChildren();
  for (const filename of references) {
    const source = `/media/projects/${encodeURIComponent(state.current)}/references/${encodeURIComponent(filename)}`;
    const card = document.createElement('div');
    card.className = 'ref-card';
    if (/\.(png|jpe?g|webp|gif)$/i.test(filename)) {
      const image = document.createElement('img');
      image.className = 'thumb';
      image.loading = 'lazy';
      image.src = source;
      image.alt = filename;
      card.append(image);
    } else if (/\.(mp4|mov|webm)$/i.test(filename)) {
      const video = document.createElement('video');
      video.className = 'thumb';
      video.src = source;
      video.muted = true;
      video.preload = 'metadata';
      card.append(video);
    } else {
      const icon = document.createElement('div');
      icon.className = 'audio-icon panel';
      icon.textContent = '♪';
      card.append(icon);
    }
    const name = document.createElement('div');
    name.className = 'ref-name';
    name.textContent = filename;
    name.title = filename;
    card.append(name);
    container.append(card);
  }
}

function renderActivity(jobs) {
  const latest = jobs[0];
  const active = jobs.find(job => job.status === 'queued' || job.status === 'running');
  const activityState = active?.status || latest?.status || 'idle';
  $('#activity-dot').className = `activity-dot ${activityState === 'idle' ? '' : activityState}`;
  const labels = {
    idle: 'Idle',
    queued: 'Studio queued',
    running: 'Studio working…',
    completed: 'Last job completed',
    failed: 'Last job failed',
  };
  $('#activity-text').textContent = labels[activityState] || activityState;
  $('#activity').title = latest?.error || latest?.message || 'Studio activity';
  $('#chatinput').disabled = Boolean(active);
  $('#send-button').disabled = Boolean(active);
  $('#profile-select').disabled = Boolean(active);
}


async function sendChat(event) {
  event.preventDefault();
  const input = $('#chatinput');
  const message = input.value.trim();
  if (!message || !state.current) {
    alert('Pick a project first');
    return;
  }
  input.value = '';
  $('#status').textContent = 'studio is thinking…';
  try {
    const job = await requestJson(`/api/chat?pid=${encodeURIComponent(state.current)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message, profile: $('#profile-select').value || 'studio'}),
    });
    $('#status').textContent = '';
    renderActivity([job]);
    await refreshProject();
  } catch (error) {
    if (!input.value) input.value = message;
    $('#status').textContent = `error: ${error.message}`;
  }
}

function uploadReferences(files) {
  if (!state.current || !files.length || state.uploading) return Promise.resolve();
  state.uploading = true;
  const form = new FormData();
  for (const file of files) form.append('files', file);
  const dropText = $('#drop-text');
  dropText.textContent = `Uploading ${files.length} file${files.length === 1 ? '' : 's'}…`;
  $('#dropzone').classList.add('uploading');
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', `/api/project/${encodeURIComponent(state.current)}/references`);
    request.upload.onprogress = event => {
      if (event.lengthComputable) {
        dropText.textContent = `Uploading… ${Math.round(event.loaded / event.total * 100)}%`;
      }
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) resolve();
      else {
        let message = request.status;
        try { message = JSON.parse(request.responseText).detail || message; } catch (_) {}
        reject(new Error(message));
      }
    };
    request.onerror = () => reject(new Error('upload network error'));
    request.send(form);
  }).then(async () => {
    state.referenceSignature = '';
    $('#status').textContent = '';
    await refreshProject();
  }).catch(error => {
    $('#status').textContent = `upload error: ${error.message}`;
  }).finally(() => {
    state.uploading = false;
    $('#dropzone').classList.remove('uploading');
    dropText.textContent = 'Drop references here or click to browse';
    $('#file-input').value = '';
  });
}

const dropzone = $('#dropzone');
const fileInput = $('#file-input');
$('#new-project').addEventListener('click', createProject);
$('#chat-form').addEventListener('submit', sendChat);
initializeGenerationSettings(refreshProject);
initializeMediaReview(refreshProject);
$('#activity-detail-toggle').addEventListener('click', () => {
  state.showActivityDetails = !state.showActivityDetails;
  const toggle = $('#activity-detail-toggle');
  toggle.textContent = state.showActivityDetails ? 'Details on' : 'Details off';
  toggle.setAttribute('aria-pressed', String(state.showActivityDetails));
  for (const jobId of Object.keys(state.activityByJob)) renderJobActivity(jobId);
});
dropzone.addEventListener('click', () => {
  if (state.current && !state.uploading) fileInput.click();
  else if (!state.current) alert('Pick a project first');
});
dropzone.addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') dropzone.click();
});
for (const eventName of ['dragenter', 'dragover']) {
  dropzone.addEventListener(eventName, event => {
    event.preventDefault();
    if (!state.uploading) dropzone.classList.add('drag');
  });
}
for (const eventName of ['dragleave', 'drop']) {
  dropzone.addEventListener(eventName, event => {
    event.preventDefault();
    dropzone.classList.remove('drag');
  });
}
dropzone.addEventListener('drop', event => uploadReferences(event.dataTransfer.files));
fileInput.addEventListener('change', () => uploadReferences(fileInput.files));

Promise.all([loadProfiles(), loadProjects()]).catch(error => {
  $('#status').textContent = `startup error: ${error.message}`;
});
setInterval(refreshProject, 2000);

const $ = selector => document.querySelector(selector);

const state = {
  current: null,
  projects: [],
  chatCount: 0,
  generationSignature: '',
  referenceSignature: '',
  refreshing: false,
  refreshPending: false,
  uploading: false,
};

async function requestJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.status;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
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
  state.current = projectId;
  state.chatCount = 0;
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
    const [project, chat, generations, references, jobs] = await Promise.all([
      requestJson(`/api/project/${projectId}`),
      requestJson(`/api/project/${projectId}/chat?after=${state.chatCount}`),
      requestJson(`/api/project/${projectId}/generations`),
      requestJson(`/api/project/${projectId}/references`),
      requestJson(`/api/project/${projectId}/jobs?limit=5`),
    ]);
    if (selected !== state.current) {
      state.refreshPending = true;
      return;
    }
    $('#prompt').textContent = project.current_prompt || '—';
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
    const generationSignature = JSON.stringify(generations.generations);
    if (generationSignature !== state.generationSignature) {
      state.generationSignature = generationSignature;
      renderGenerations(generations.generations);
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
    const role = document.createElement('span');
    role.className = `role-${message.role} font-semibold`;
    role.textContent = message.role;
    row.append(role, document.createTextNode(` ${message.content || ''}`));
    chat.append(row);
  }
  if (messages.length) chat.scrollTop = chat.scrollHeight;
}

function renderGenerations(generations) {
  const container = $('#gens');
  container.replaceChildren();
  if (!generations.length) {
    showEmpty(container, 'none yet');
    return;
  }
  let rendered = 0;
  for (const generation of generations) {
    const video = generation.files.find(file => file.toLowerCase().endsWith('.mp4'));
    const image = generation.files.find(file => /\.(png|jpe?g|webp)$/i.test(file));
    if (!video && !image) continue;
    const card = document.createElement('div');
    const filename = video || image;
    const source = [
      '/media/projects',
      encodeURIComponent(state.current),
      'generations',
      encodeURIComponent(generation.gen),
      encodeURIComponent(filename),
    ].join('/');
    const media = document.createElement(video ? 'video' : 'img');
    media.className = 'thumb';
    media.src = source;
    if (video) {
      media.controls = true;
      media.preload = 'metadata';
    } else {
      media.loading = 'lazy';
    }
    card.append(media);
    const label = document.createElement('div');
    label.className = 'generation-label';
    const meta = generation.meta || {};
    const details = [generation.gen];
    if (meta.seed !== undefined && meta.seed !== null) details.push(`seed ${meta.seed}`);
    if (meta.recipe || meta.kind) details.push(meta.recipe || meta.kind);
    label.textContent = details.join(' · ');
    card.append(label);
    container.append(card);
    rendered += 1;
  }
  if (!rendered) showEmpty(container, 'none yet');
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
}

function showEmpty(element, text) {
  const empty = document.createElement('div');
  empty.className = 'empty';
  empty.textContent = text;
  element.dataset.empty = 'true';
  element.append(empty);
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
      body: JSON.stringify({message}),
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

loadProjects().catch(error => {
  $('#status').textContent = `startup error: ${error.message}`;
});
setInterval(refreshProject, 5000);

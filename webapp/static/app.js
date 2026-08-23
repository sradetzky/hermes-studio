const $ = selector => document.querySelector(selector);

const state = {
  current: null,
  projects: [],
  profiles: [],
  chatCount: 0,
  activityCursor: 0,
  activityByJob: {},
  showActivityDetails: true,
  generations: [],
  filteredGenerations: [],
  generationDetail: null,
  selectedGenerationFile: null,
  generationOpener: null,
  mediaActioning: false,
  generationSettings: null,
  generationSettingsOptions: null,
  settingsReferences: [],
  settingsOpener: null,
  generationSignature: '',
  referenceSignature: '',
  refreshing: false,
  refreshPending: false,
  uploading: false,
};

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

function renderGenerationReadiness(contract) {
  state.generationSettings = contract;
  const readiness = contract?.readiness || {
    ready: false, status: 'not-configured', reasons: ['Generation settings unavailable'],
    warnings: [], resolution: {}, timing: {},
  };
  const labels = {
    ready: 'Ready',
    'empty-prompt': 'Prompt empty',
    'not-configured': 'Not configured',
    stale: 'Prompt changed',
    blocked: 'Needs attention',
  };
  const badge = $('#generation-readiness-badge');
  badge.className = `readiness-badge ${readiness.status}`;
  badge.textContent = labels[readiness.status] || readiness.status;
  const settings = contract?.settings;
  if (settings) {
    const resolution = readiness.resolution || {};
    const flags = [];
    if (settings.accel) flags.push('accel');
    if (settings.turbo) flags.push('turbo');
    if (settings.upscale) flags.push('upscale');
    $('#generation-readiness-summary').textContent = [
      settings.mode.toUpperCase(),
      `${settings.duration}s`,
      `${resolution.width || '?'}×${resolution.height || '?'}`,
      `${settings.steps} steps`,
      ...flags,
    ].join(' · ');
  } else {
    $('#generation-readiness-summary').textContent = 'Save settings for this prompt';
  }
  const details = $('#generation-readiness-details');
  details.replaceChildren();
  for (const reason of readiness.reasons || []) {
    const item = document.createElement('span');
    item.className = 'readiness-reason';
    item.textContent = reason;
    details.append(item);
  }
  for (const warning of readiness.warnings || []) {
    const item = document.createElement('span');
    item.className = 'readiness-warning';
    item.textContent = warning;
    details.append(item);
  }
}

function setSelectOptions(select, values, current, emptyLabel) {
  select.replaceChildren();
  if (emptyLabel !== null) {
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = emptyLabel;
    select.append(empty);
  }
  const options = [...values];
  if (current && !options.includes(current)) options.unshift(current);
  for (const value of options) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
  select.value = current || '';
}

async function openGenerationSettings(opener = null) {
  if (!state.current) return;
  state.settingsOpener = opener || state.settingsOpener;
  const dialog = $('#generation-settings-dialog');
  $('#generation-settings-status').textContent = 'Loading settings…';
  if (!dialog.open) dialog.showModal();
  const selectedProject = state.current;
  try {
    const contract = await requestJson(
      `/api/project/${encodeURIComponent(selectedProject)}/generation-settings`);
    if (selectedProject !== state.current || !dialog.open) return;
    state.generationSettings = contract;
    state.generationSettingsOptions = contract.options;
    state.settingsReferences = [...contract.settings.references];
    populateGenerationSettings(contract);
    $('#generation-settings-status').textContent = '';
  } catch (error) {
    $('#generation-settings-status').textContent = `Unable to load: ${error.message}`;
  }
}

function populateGenerationSettings(contract) {
  const settings = contract.settings;
  const options = contract.options;
  $('#setting-mode').value = settings.mode;
  $('#setting-duration').value = settings.duration;
  $('#setting-aspect').value = settings.aspect;
  $('#setting-mp').value = settings.mp;
  $('#setting-size-mode').value = settings.width === null ? 'mp' : 'explicit';
  $('#setting-width').value = settings.width ?? '';
  $('#setting-height').value = settings.height ?? '';
  $('#setting-seed').value = settings.seed ?? '';
  $('#setting-steps').value = settings.steps;
  $('#setting-accel').checked = settings.accel;
  $('#setting-turbo').checked = settings.turbo;
  $('#setting-w4a8').checked = settings.w4a8;
  $('#setting-ref-image-size').value = settings.ref_image_size;
  $('#setting-turbo-strength').value = settings.turbo_strength;
  $('#setting-upscale').checked = settings.upscale;
  $('#setting-upscale-scale').value = settings.upscale_scale;
  $('#setting-upscale-color').value = settings.upscale_color;
  $('#setting-upscale-chunk').checked = settings.upscale_chunk;
  setSelectOptions(
    $('#setting-turbo-lora'), options.turbo_loras,
    settings.turbo_lora, 'Runner default');
  setSelectOptions($('#setting-unet'), options.unets, settings.unet, 'Automatic');
  updateGenerationSettingsForm();
}

function updateGenerationSettingsForm() {
  const explicit = $('#setting-size-mode').value === 'explicit';
  $('#setting-mp-field').hidden = explicit;
  for (const field of document.querySelectorAll('.explicit-size-field')) {
    field.hidden = !explicit;
  }
  $('#setting-mp').required = !explicit;
  $('#setting-width').required = explicit;
  $('#setting-height').required = explicit;
  const turbo = $('#setting-turbo').checked;
  $('#setting-turbo-lora').disabled = !turbo;
  $('#setting-turbo-strength').disabled = !turbo;
  const upscale = $('#setting-upscale').checked;
  $('#setting-upscale-scale').disabled = !upscale;
  $('#setting-upscale-color').disabled = !upscale;
  $('#setting-upscale-chunk').disabled = !upscale;
  const mode = $('#setting-mode').value;
  const referenceHelp = {
    t2va: 'T2VA uses no references.',
    i2va: 'Select exactly one opening-frame image.',
    fl2va: 'Select exactly two images in first-frame → last-frame order.',
    r2v: 'Select 1–9 references in <Picture N> order.',
  };
  $('#settings-reference-help').textContent = referenceHelp[mode] || '';
  renderSettingsReferences();
  updateComputedSettings();
}

function renderSettingsReferences() {
  const container = $('#settings-references');
  container.replaceChildren();
  const available = state.generationSettingsOptions?.references || [];
  if (!available.length) {
    showEmpty(container, 'No image references in this project');
    return;
  }
  const selected = document.createElement('div');
  selected.className = 'selected-references';
  for (const [index, name] of state.settingsReferences.entries()) {
    const row = document.createElement('div');
    row.className = 'settings-reference selected';
    const label = document.createElement('span');
    label.textContent = `${index + 1}. ${name}`;
    const controls = document.createElement('div');
    for (const [text, action, disabled] of [
      ['↑', () => moveSettingsReference(index, -1), index === 0],
      ['↓', () => moveSettingsReference(index, 1), index === state.settingsReferences.length - 1],
      ['×', () => removeSettingsReference(index), false],
    ]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = text;
      button.disabled = disabled;
      button.addEventListener('click', action);
      controls.append(button);
    }
    row.append(label, controls);
    selected.append(row);
  }
  if (state.settingsReferences.length) container.append(selected);
  const unselected = available.filter(name => !state.settingsReferences.includes(name));
  if (unselected.length) {
    const choices = document.createElement('div');
    choices.className = 'available-references';
    for (const name of unselected) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'settings-reference available';
      button.textContent = `+ ${name}`;
      button.addEventListener('click', () => {
        state.settingsReferences.push(name);
        renderSettingsReferences();
      });
      choices.append(button);
    }
    container.append(choices);
  }
}

function moveSettingsReference(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.settingsReferences.length) return;
  [state.settingsReferences[index], state.settingsReferences[target]] =
    [state.settingsReferences[target], state.settingsReferences[index]];
  renderSettingsReferences();
}

function removeSettingsReference(index) {
  state.settingsReferences.splice(index, 1);
  renderSettingsReferences();
}

function settingsResolution() {
  if ($('#setting-size-mode').value === 'explicit') {
    return [Number($('#setting-width').value), Number($('#setting-height').value)];
  }
  const ratios = {'16:9': 16 / 9, '9:16': 9 / 16, '1:1': 1,
    '4:3': 4 / 3, '3:4': 3 / 4, '21:9': 21 / 9};
  const ratio = ratios[$('#setting-aspect').value] || 16 / 9;
  const pixels = Number($('#setting-mp').value) * 1_000_000;
  const height = Math.sqrt(pixels / ratio);
  const width = ratio * height;
  return [Math.max(32, Math.round(width / 32) * 32),
    Math.max(32, Math.round(height / 32) * 32)];
}

function updateComputedSettings() {
  const [width, height] = settingsResolution();
  const seconds = Number($('#setting-duration').value);
  let frames = Math.max(5, Math.round(seconds * 24));
  while (frames % 17 !== 5) frames += 1;
  const mp = width && height ? (width * height / 1_000_000).toFixed(3) : '?';
  $('#settings-computed').textContent = Number.isFinite(width) && Number.isFinite(height)
    ? `${width}×${height} · ${mp}MP · ${frames} frames · ${(frames / 24).toFixed(3)}s actual`
    : 'Enter a valid canvas and duration';
}

function generationSettingsPayload() {
  const explicit = $('#setting-size-mode').value === 'explicit';
  const seedValue = $('#setting-seed').value.trim();
  return {
    mode: $('#setting-mode').value,
    duration: Number($('#setting-duration').value),
    aspect: $('#setting-aspect').value,
    mp: Number($('#setting-mp').value),
    width: explicit ? Number($('#setting-width').value) : null,
    height: explicit ? Number($('#setting-height').value) : null,
    seed: seedValue || null,
    steps: Number($('#setting-steps').value),
    accel: $('#setting-accel').checked,
    turbo: $('#setting-turbo').checked,
    turbo_lora: $('#setting-turbo-lora').value || null,
    turbo_strength: Number($('#setting-turbo-strength').value),
    w4a8: $('#setting-w4a8').checked,
    unet: $('#setting-unet').value || null,
    ref_image_size: $('#setting-ref-image-size').value,
    upscale: $('#setting-upscale').checked,
    upscale_scale: Number($('#setting-upscale-scale').value),
    upscale_color: $('#setting-upscale-color').value,
    upscale_chunk: $('#setting-upscale-chunk').checked,
    references: [...state.settingsReferences],
  };
}

async function saveGenerationSettings(event) {
  event.preventDefault();
  const form = $('#generation-settings-form');
  if (!form.reportValidity()) return;
  const status = $('#generation-settings-status');
  status.textContent = 'Saving settings…';
  const selectedProject = state.current;
  try {
    const contract = await requestJson(
      `/api/project/${encodeURIComponent(selectedProject)}/generation-settings`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(generationSettingsPayload()),
      });
    if (selectedProject !== state.current) return;
    renderGenerationReadiness(contract);
    closeGenerationSettings();
    await refreshProject();
  } catch (error) {
    status.textContent = `Save failed: ${error.message}`;
  }
}

function applySettingsPreset(mp, steps) {
  $('#setting-size-mode').value = 'mp';
  $('#setting-mp').value = mp;
  $('#setting-steps').value = steps;
  $('#setting-accel').checked = true;
  updateGenerationSettingsForm();
}

function closeGenerationSettings(restoreFocus = true) {
  const dialog = $('#generation-settings-dialog');
  const opener = state.settingsOpener;
  state.settingsOpener = null;
  state.generationSettingsOptions = null;
  state.settingsReferences = [];
  if (dialog.open) dialog.close();
  if (restoreFocus && opener?.isConnected) queueMicrotask(() => opener.focus());
}

function generationRecipe(generation) {
  const meta = generation.meta || {};
  return String(meta.recipe || meta.kind || 'unknown');
}

function updateGenerationRecipeFilter() {
  const select = $('#generation-recipe-filter');
  const selected = select.value || 'all';
  const recipes = [...new Set(state.generations.map(generationRecipe))].sort();
  select.replaceChildren();
  const all = document.createElement('option');
  all.value = 'all';
  all.textContent = 'All recipes';
  select.append(all);
  for (const recipe of recipes) {
    const option = document.createElement('option');
    option.value = recipe;
    option.textContent = recipe;
    select.append(option);
  }
  select.value = recipes.includes(selected) ? selected : 'all';
}

function generationMatchesFilters(generation) {
  const kind = $('#generation-kind-filter').value;
  const recipe = $('#generation-recipe-filter').value;
  const review = $('#generation-review-filter').value;
  const media = generation.media || [];
  if (kind !== 'all' && !media.some(item => item.kind === kind)) return false;
  if (recipe !== 'all' && generationRecipe(generation) !== recipe) return false;
  const promoted = Boolean(generation.review?.promoted?.length);
  if (review === 'promoted' && !promoted) return false;
  if (review === 'unpromoted' && promoted) return false;
  return media.length > 0;
}

function renderGenerations() {
  const container = $('#gens');
  container.replaceChildren();
  state.filteredGenerations = state.generations.filter(generationMatchesFilters);
  $('#generation-count').textContent = state.generations.length
    ? `${state.filteredGenerations.length}/${state.generations.length}` : '';
  if (!state.generations.length) {
    showEmpty(container, 'none yet');
    return;
  }
  if (!state.filteredGenerations.length) {
    showEmpty(container, 'no matching generations');
    return;
  }
  for (const generation of state.filteredGenerations) {
    const mediaItems = generation.media || [];
    const primary = mediaItems.find(item => item.kind === 'video') ||
      mediaItems.find(item => item.kind === 'image') || mediaItems[0];
    const card = document.createElement('div');
    card.className = 'generation-card';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'generation-preview';
    open.setAttribute('aria-label', `Review generation ${generation.gen}`);
    const media = document.createElement(
      primary.kind === 'video' ? 'video' : primary.kind === 'image' ? 'img' : 'div');
    media.className = 'thumb';
    if (primary.kind === 'video') {
      media.src = primary.url;
      media.muted = true;
      media.playsInline = true;
      media.preload = 'metadata';
    } else if (primary.kind === 'image') {
      media.src = primary.url;
      media.loading = 'lazy';
      media.alt = `Generation ${generation.gen}`;
    } else {
      media.className = 'audio-icon panel';
      media.textContent = '♪';
    }
    open.append(media);
    open.addEventListener('click', () => openGeneration(generation.gen, open));
    card.append(open);
    const label = document.createElement('div');
    label.className = 'generation-label';
    const meta = generation.meta || {};
    const details = [generation.gen];
    if (meta.seed !== undefined && meta.seed !== null) details.push(`seed ${meta.seed}`);
    if (meta.recipe || meta.kind) details.push(meta.recipe || meta.kind);
    const text = document.createElement('span');
    text.textContent = details.join(' · ');
    label.append(text);
    if (generation.review?.promoted?.length) {
      const badge = document.createElement('span');
      badge.className = 'media-state-badge promoted';
      badge.textContent = 'final';
      label.append(badge);
    }
    card.append(label);
    container.append(card);
  }
}

function showGenerationLoading(generationId) {
  $('#generation-title').textContent = `Generation ${generationId}`;
  $('#generation-media').replaceChildren();
  showEmpty($('#generation-media'), 'Loading generation…');
  $('#generation-files').replaceChildren();
  $('#generation-filename').textContent = '—';
  $('#generation-file-state').textContent = '';
  $('#generation-prompt').textContent = '—';
  $('#generation-meta').textContent = '{}';
  $('#generation-actions').replaceChildren();
  $('#media-action-status').textContent = '';
  $('#promote-generation').disabled = true;
  $('#reference-generation').disabled = true;
}

async function openGeneration(generationId, opener = null) {
  const dialog = $('#generation-dialog');
  state.generationOpener = opener || state.generationOpener;
  state.generationDetail = null;
  state.selectedGenerationFile = null;
  showGenerationLoading(generationId);
  if (!dialog.open) dialog.showModal();
  const selectedProject = state.current;
  try {
    const detail = await requestJson(
      `/api/project/${encodeURIComponent(selectedProject)}/generations/${encodeURIComponent(generationId)}`);
    if (selectedProject !== state.current || !dialog.open) return;
    state.generationDetail = detail;
    state.selectedGenerationFile = detail.media[0]?.name || null;
    renderGenerationDetail();
  } catch (error) {
    $('#media-action-status').textContent = `Unable to load: ${error.message}`;
    $('#generation-media').replaceChildren();
    showEmpty($('#generation-media'), 'generation unavailable');
  }
}

function renderGenerationDetail() {
  const detail = state.generationDetail;
  if (!detail) return;
  $('#generation-title').textContent = `Generation ${detail.gen}`;
  $('#generation-prompt').textContent = detail.prompt || '—';
  $('#generation-meta').textContent = JSON.stringify(detail.meta || {}, null, 2);
  const files = $('#generation-files');
  files.replaceChildren();
  for (const item of detail.media) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `media-file ${item.name === state.selectedGenerationFile ? 'active' : ''}`;
    button.textContent = `${item.kind} · ${item.name}`;
    button.title = item.name;
    button.addEventListener('click', () => {
      state.selectedGenerationFile = item.name;
      renderGenerationDetail();
    });
    files.append(button);
  }
  const actions = $('#generation-actions');
  actions.replaceChildren();
  if (!detail.actions.length) {
    showEmpty(actions, 'No review actions yet');
  } else {
    for (const action of detail.actions) {
      const row = document.createElement('div');
      row.className = 'generation-action-row';
      row.textContent = `${action.action} · ${action.source} → ${action.target}`;
      actions.append(row);
    }
  }
  renderSelectedGenerationMedia();
  updateGenerationNavigation();
}

function selectedGenerationMedia() {
  return state.generationDetail?.media.find(
    item => item.name === state.selectedGenerationFile) || null;
}

function renderSelectedGenerationMedia() {
  const item = selectedGenerationMedia();
  const stage = $('#generation-media');
  stage.replaceChildren();
  if (!item) {
    showEmpty(stage, 'No supported media in this generation');
    $('#generation-filename').textContent = '—';
    $('#promote-generation').disabled = true;
    $('#reference-generation').disabled = true;
    return;
  }
  let media;
  if (item.kind === 'video') {
    media = document.createElement('video');
    media.controls = true;
    media.preload = 'metadata';
    media.playsInline = true;
  } else if (item.kind === 'image') {
    media = document.createElement('img');
    media.alt = item.name;
  } else {
    media = document.createElement('audio');
    media.controls = true;
  }
  media.src = item.url;
  media.className = 'detail-media';
  stage.append(media);
  $('#generation-filename').textContent = `${item.name} · ${formatBytes(item.size)}`;
  const states = [];
  if (item.promoted) states.push('Promoted to final');
  if (item.reference) states.push('Reference');
  $('#generation-file-state').textContent = states.join(' · ');
  const promote = $('#promote-generation');
  const reference = $('#reference-generation');
  promote.textContent = item.promoted ? 'Promoted ✓' : 'Promote to final';
  reference.textContent = item.reference ? 'Reference ✓' : 'Use as reference';
  promote.disabled = state.mediaActioning || item.promoted;
  reference.disabled = state.mediaActioning || item.reference;
}

function formatBytes(size) {
  if (!Number.isFinite(size)) return 'unknown size';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

async function performMediaAction(action) {
  const detail = state.generationDetail;
  const item = selectedGenerationMedia();
  if (!detail || !item || state.mediaActioning) return;
  state.mediaActioning = true;
  renderSelectedGenerationMedia();
  const status = $('#media-action-status');
  status.textContent = action === 'promote' ? 'Promoting…' : 'Copying reference…';
  const endpoint = action === 'promote' ? 'promote' : 'use-as-reference';
  const selectedProject = state.current;
  try {
    const response = await requestJson(
      `/api/project/${encodeURIComponent(selectedProject)}/generations/${encodeURIComponent(detail.gen)}/${endpoint}`,
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({filename: item.name}),
      });
    status.textContent = action === 'promote'
      ? `Promoted as ${response.result.target}`
      : `Reference saved as ${response.result.target}`;
    const refreshed = await requestJson(
      `/api/project/${encodeURIComponent(selectedProject)}/generations/${encodeURIComponent(detail.gen)}`);
    if (selectedProject === state.current) {
      state.generationDetail = refreshed;
      state.selectedGenerationFile = item.name;
      state.generationSignature = '';
      if (action === 'reference') state.referenceSignature = '';
      renderGenerationDetail();
      await refreshProject();
    }
  } catch (error) {
    status.textContent = `Action failed: ${error.message}`;
  } finally {
    state.mediaActioning = false;
    renderSelectedGenerationMedia();
  }
}

function updateGenerationNavigation() {
  const current = state.generationDetail?.gen;
  const index = state.filteredGenerations.findIndex(item => item.gen === current);
  $('#generation-previous').disabled = index < 0 || index >= state.filteredGenerations.length - 1;
  $('#generation-next').disabled = index <= 0;
}

function navigateGeneration(direction) {
  const current = state.generationDetail?.gen;
  const index = state.filteredGenerations.findIndex(item => item.gen === current);
  const nextIndex = index + direction;
  if (index < 0 || nextIndex < 0 || nextIndex >= state.filteredGenerations.length) return;
  openGeneration(state.filteredGenerations[nextIndex].gen);
}

function closeGenerationDialog(restoreFocus = true) {
  const dialog = $('#generation-dialog');
  const opener = state.generationOpener;
  state.generationDetail = null;
  state.selectedGenerationFile = null;
  state.generationOpener = null;
  state.mediaActioning = false;
  if (dialog.open) dialog.close();
  if (restoreFocus && opener?.isConnected) queueMicrotask(() => opener.focus());
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
const generationDialog = $('#generation-dialog');
const generationSettingsDialog = $('#generation-settings-dialog');
$('#new-project').addEventListener('click', createProject);
$('#chat-form').addEventListener('submit', sendChat);
$('#edit-generation-settings').addEventListener('click', event =>
  openGenerationSettings(event.currentTarget));
$('#generation-settings-form').addEventListener('submit', saveGenerationSettings);
$('#generation-settings-close').addEventListener('click', () => closeGenerationSettings());
$('#generation-settings-cancel').addEventListener('click', () => closeGenerationSettings());
generationSettingsDialog.addEventListener('close', () => closeGenerationSettings());
generationSettingsDialog.addEventListener('click', event => {
  if (event.target === generationSettingsDialog) closeGenerationSettings();
});
for (const field of [
  $('#setting-mode'), $('#setting-duration'), $('#setting-aspect'),
  $('#setting-size-mode'), $('#setting-mp'), $('#setting-width'),
  $('#setting-height'), $('#setting-turbo'), $('#setting-upscale'),
]) {
  field.addEventListener('input', updateGenerationSettingsForm);
  field.addEventListener('change', updateGenerationSettingsForm);
}
$('#settings-preview-preset').addEventListener('click', () =>
  applySettingsPreset(0.5, 8));
$('#settings-final-preset').addEventListener('click', () =>
  applySettingsPreset(0.9, 20));
for (const filter of [
  $('#generation-kind-filter'),
  $('#generation-recipe-filter'),
  $('#generation-review-filter'),
]) {
  filter.addEventListener('change', renderGenerations);
}
$('#generation-close').addEventListener('click', () => closeGenerationDialog());
$('#generation-previous').addEventListener('click', () => navigateGeneration(1));
$('#generation-next').addEventListener('click', () => navigateGeneration(-1));
$('#promote-generation').addEventListener('click', () => performMediaAction('promote'));
$('#reference-generation').addEventListener('click', () => performMediaAction('reference'));
generationDialog.addEventListener('close', () => closeGenerationDialog());
generationDialog.addEventListener('click', event => {
  if (event.target === generationDialog) closeGenerationDialog();
});
generationDialog.addEventListener('keydown', event => {
  if (event.key === 'ArrowLeft') navigateGeneration(1);
  if (event.key === 'ArrowRight') navigateGeneration(-1);
});
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

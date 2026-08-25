import {
  closeGenerationSettings,
  initializeGenerationSettings,
  rerenderGenerationReadiness,
  renderGenerationReadiness,
  resetGenerationSettings,
} from './generation-settings.js';
import {
  applyGenerations,
  closeGenerationDialog,
  initializeMediaReview,
  renderMovieProject,
  resetMediaReview,
  resetMovieReview,
  updateMovieExportControls,
} from './media-review.js';
import {apiPaths} from './api-paths.mjs';
import {
  formatQueueDuration,
  queueJobSpecs,
  queueJobTitle,
  queuePresentation,
} from './comfy-queue.mjs';
import {
  captureClipContext,
  captureProjectDialogContext,
  captureProjectContext,
  isClipContextCurrent,
  isProjectDialogContextCurrent,
  isProjectContextCurrent,
} from './frontend-contracts.mjs';
import {
  refreshClipPlane,
} from './refresh-planes.mjs';
import {
  conversationJobActive,
  conversationScope,
  ensureConversationScope,
  initializeConversationController,
  refreshConversation,
  resetConversation,
  switchConversationScope,
} from './conversation-controller.js';
import {
  initializeReferenceController,
  refreshReferences,
  resetReferences,
} from './reference-controller.js';
import {updateRefreshStatus} from './refresh-status.mjs';
import {$, activeClip, requestJson, showEmpty, state} from './shared.js';
import {
  moveWorkspacePane,
  normalizeWorkspacePane,
} from './workspace-panes.mjs';

const appState = {
  comfyQueueRequestRevision: 0,
  refreshing: false,
  refreshPending: false,
  refreshErrors: {},
  projectMetadataSaving: false,
  projectMetadataOpener: null,
  projectMetadataDialogRevision: 0,
  projectMetadataDialogContext: null,
};

function projectMetadataContextState() {
  return {
    ...state,
    projectMetadataDialogRevision: appState.projectMetadataDialogRevision,
  };
}

function isProjectMetadataContextCurrent(context) {
  return isProjectDialogContextCurrent(projectMetadataContextState(), context);
}

async function loadProfiles() {
  const data = await requestJson(apiPaths.profiles);
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

function queueRow(label, job) {
  const row = document.createElement('div');
  row.className = 'comfy-queue-row';
  const heading = document.createElement('div');
  heading.className = 'comfy-queue-row-heading';
  const stateLabel = document.createElement('span');
  stateLabel.className = 'comfy-queue-state';
  stateLabel.textContent = label;
  const title = document.createElement('strong');
  title.className = 'comfy-queue-title';
  title.textContent = queueJobTitle(job);
  heading.append(stateLabel, title);

  const specs = document.createElement('div');
  specs.className = 'comfy-queue-specs';
  specs.textContent = queueJobSpecs(job);

  const metadata = document.createElement('div');
  metadata.className = 'comfy-queue-meta';
  const timingValue = label === 'Running' ? job.elapsed_seconds
    : label === 'Last completed' ? job.execution_seconds : job.queued_seconds;
  const timing = formatQueueDuration(timingValue);
  if (timing) {
    const timingLabel = label === 'Running' ? 'Elapsed'
      : label === 'Last completed' ? 'Completed in' : 'Waiting';
    const timingText = document.createElement('span');
    timingText.textContent = `${timingLabel} ${timing}`;
    metadata.append(timingText);
  }
  if (job.seed !== undefined) {
    const seed = document.createElement('span');
    seed.textContent = `Seed ${job.seed}`;
    metadata.append(seed);
  }
  const promptId = document.createElement('span');
  promptId.className = 'comfy-queue-id';
  promptId.textContent = `Prompt ${job.prompt_id}`;
  promptId.title = job.prompt_id;
  metadata.append(promptId);
  row.append(heading);
  if (specs.textContent) row.append(specs);
  row.append(metadata);
  return row;
}

function renderComfyQueue(snapshot) {
  const presentation = queuePresentation(snapshot);
  $('#comfy-queue-dot').className = `comfy-queue-dot ${presentation.state}`;
  $('#comfy-queue-label').textContent = presentation.label;
  const list = $('#comfy-queue-list');
  list.replaceChildren();
  if (!snapshot?.available) {
    const message = document.createElement('div');
    message.className = 'comfy-queue-empty offline';
    message.textContent = snapshot?.error || 'ComfyUI queue unavailable';
    list.append(message);
    return;
  }
  for (const job of snapshot.running || []) list.append(queueRow('Running', job));
  for (const job of snapshot.pending || []) {
    list.append(queueRow(job.position === 1 ? 'Next' : `Queued ${job.position}`, job));
  }
  if (!(snapshot.running?.length || snapshot.pending?.length)) {
    const empty = document.createElement('div');
    empty.className = 'comfy-queue-empty';
    empty.textContent = 'No running or queued jobs';
    list.append(empty);
  }
  if (snapshot.recent_completed) {
    list.append(queueRow('Last completed', snapshot.recent_completed));
  }
}

async function refreshComfyQueue() {
  appState.comfyQueueRequestRevision += 1;
  const requestRevision = appState.comfyQueueRequestRevision;
  try {
    const includeRecent = $('#comfy-queue').open ? '?include_recent=true' : '';
    const snapshot = await requestJson(
      `${apiPaths.comfyQueue}${includeRecent}`, {cache: 'no-store'});
    if (requestRevision !== appState.comfyQueueRequestRevision) return;
    renderComfyQueue(snapshot);
  } catch (error) {
    if (requestRevision !== appState.comfyQueueRequestRevision) return;
    renderComfyQueue({
      available: false, running: [], pending: [], error: error.message,
    });
  }
}

const narrowWorkspace = window.matchMedia('(max-width: 1099px)');
if (narrowWorkspace.matches) $('#prompt-panel').open = false;

function renderWorkspacePane() {
  const pane = normalizeWorkspacePane(state.workspacePane);
  state.workspacePane = pane;
  $('#workspace').dataset.pane = pane;
  for (const button of document.querySelectorAll('[data-workspace-pane]')) {
    const active = button.dataset.workspacePane === pane;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  }
  for (const panel of document.querySelectorAll('[data-workspace-panel]')) {
    const hidden = narrowWorkspace.matches && panel.dataset.workspacePanel !== pane;
    panel.setAttribute('aria-hidden', String(hidden));
    panel.inert = hidden;
  }
}

function setWorkspacePane(value, focus = false) {
  state.workspacePane = normalizeWorkspacePane(value);
  renderWorkspacePane();
  if (focus) {
    document.querySelector(
      `[data-workspace-pane="${state.workspacePane}"]`)?.focus();
  }
}

function handleWorkspaceNavigation(event) {
  const pane = event.target.closest('[data-workspace-pane]')?.dataset.workspacePane;
  if (!pane) return;
  let next;
  if (event.key === 'ArrowLeft') next = moveWorkspacePane(pane, -1);
  else if (event.key === 'ArrowRight') next = moveWorkspacePane(pane, 1);
  else if (event.key === 'Home') next = 'projects';
  else if (event.key === 'End') next = 'media';
  else return;
  event.preventDefault();
  setWorkspacePane(next, true);
}


async function loadProjects() {
  const data = await requestJson(apiPaths.projects);
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
    const title = document.createElement('span');
    title.className = 'proj-title';
    title.textContent = project.title || project.id;
    const id = document.createElement('span');
    id.className = 'proj-id';
    id.textContent = project.id;
    item.append(title, id);
    item.title = project.brief || project.title || project.id;
    item.addEventListener('click', () => selectProject(project.id));
    navigation.append(item);
  }
  renderProjectMetadataControls();
}

function renderProjectMetadataControls() {
  const unavailable = !state.project || conversationJobActive() ||
    appState.projectMetadataSaving;
  $('#edit-project').disabled = unavailable;
  $('#project-metadata-display-title').disabled = appState.projectMetadataSaving;
  $('#project-metadata-brief').disabled = appState.projectMetadataSaving;
  $('#project-metadata-save').disabled = unavailable;
  $('#project-metadata-cancel').disabled = appState.projectMetadataSaving;
  $('#project-metadata-close').disabled = appState.projectMetadataSaving;
}

function openProjectMetadata() {
  if (!state.project || conversationJobActive() || appState.projectMetadataSaving) return;
  appState.projectMetadataDialogRevision += 1;
  appState.projectMetadataDialogContext =
    captureProjectDialogContext(projectMetadataContextState());
  appState.projectMetadataOpener = document.activeElement;
  $('#project-metadata-id').textContent = state.project.id;
  $('#project-metadata-display-title').value = state.project.title;
  $('#project-metadata-brief').value = state.project.brief;
  $('#project-metadata-status').textContent = '';
  $('#project-metadata-dialog').showModal();
  $('#project-metadata-display-title').focus();
}

function closeProjectMetadata(restoreFocus = true, force = false) {
  const dialog = $('#project-metadata-dialog');
  if (appState.projectMetadataSaving && !force) return;
  const opener = appState.projectMetadataOpener;
  appState.projectMetadataDialogRevision += 1;
  appState.projectMetadataDialogContext = null;
  appState.projectMetadataSaving = false;
  appState.projectMetadataOpener = null;
  if (dialog.open) dialog.close();
  if (restoreFocus && opener?.isConnected) {
    opener.focus();
  }
  renderProjectMetadataControls();
}

async function saveProjectMetadata(event) {
  event.preventDefault();
  if (!state.project || conversationJobActive() || appState.projectMetadataSaving) return;
  const context = appState.projectMetadataDialogContext;
  if (!context || !isProjectMetadataContextCurrent(context)) return;
  const title = $('#project-metadata-display-title').value.trim();
  const brief = $('#project-metadata-brief').value;
  if (!title) {
    $('#project-metadata-status').textContent = 'Display title is required';
    return;
  }
  appState.projectMetadataSaving = true;
  $('#project-metadata-status').textContent = 'Saving project…';
  renderProjectMetadataControls();
  try {
    const response = await requestJson(apiPaths.project(context.projectId), {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title, brief}),
    });
    if (!isProjectMetadataContextCurrent(context)) return;
    state.project = response.project;
    appState.projectMetadataSaving = false;
    closeProjectMetadata(false);
    await loadProjects();
    if (!isProjectContextCurrent(state, context)) return;
    await refreshProject();
  } catch (error) {
    if (isProjectMetadataContextCurrent(context)) {
      $('#project-metadata-status').textContent = error.message;
    }
  } finally {
    if (isProjectMetadataContextCurrent(context)) {
      appState.projectMetadataSaving = false;
      renderProjectMetadataControls();
    }
  }
}

function resetClipState() {
  $('#prompt').textContent = '—';
  resetMediaReview();
  resetGenerationSettings();
}

function renderClips() {
  const navigation = $('#clips');
  navigation.replaceChildren();
  if (!state.clips.length) {
    showEmpty(navigation, state.current ? 'no clips' : 'pick a project');
  } else {
    state.clips.forEach((clip, index) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = [
        'clip', clip.id === state.currentClip ? 'active' : '',
        clip.enabled ? '' : 'disabled',
      ].filter(Boolean).join(' ');
      const order = document.createElement('span');
      order.className = 'clip-index';
      order.textContent = String(index + 1).padStart(2, '0');
      const title = document.createElement('span');
      title.className = 'clip-title';
      title.textContent = clip.title;
      title.title = clip.id;
      item.append(order, title);
      if (clip.selected_take) {
        const take = document.createElement('span');
        take.className = 'clip-take';
        take.textContent = 'take';
        take.title = `${clip.selected_take.generation}/${clip.selected_take.filename}`;
        item.append(take);
      }
      item.addEventListener('click', () => selectClip(clip.id));
      navigation.append(item);
    });
  }
  const clip = activeClip();
  const index = state.clips.findIndex(entry => entry.id === state.currentClip);
  const jobActive = conversationJobActive();
  $('#new-clip').disabled = !state.current || jobActive;
  $('#rename-clip').disabled = !clip || jobActive;
  $('#move-clip-up').disabled = !clip || index <= 0 || jobActive;
  $('#move-clip-down').disabled = !clip || index < 0 ||
    index >= state.clips.length - 1 || jobActive;
  $('#toggle-clip').disabled = !clip || jobActive;
  $('#toggle-clip').textContent = clip?.enabled ? 'Disable' : 'Enable';
  ensureConversationScope();
}

async function selectClip(clipId) {
  if (!state.clips.some(clip => clip.id === clipId)) return;
  setWorkspacePane('chat');
  if (clipId === state.currentClip) {
    await switchConversationScope('clip');
    return;
  }
  closeGenerationDialog(false);
  closeGenerationSettings(false);
  state.currentClip = clipId;
  state.clipRevision += 1;
  resetConversation('clip');
  resetClipState();
  renderClips();
  await refreshProject();
}

async function runClipAction(action) {
  if (!state.current || conversationJobActive()) return;
  try {
    $('#status').textContent = 'Updating clip…';
    await action();
    $('#status').textContent = '';
    await refreshProject();
  } catch (error) {
    $('#status').textContent = `clip error: ${error.message}`;
  }
}

async function createClip() {
  const title = prompt('Clip title:');
  if (!title) return;
  await runClipAction(async () => {
    const created = await requestJson(
      apiPaths.clips(state.current), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title}),
      });
    state.currentClip = created.clip.id;
    state.clipRevision += 1;
    resetConversation('clip');
    resetClipState();
  });
}

async function renameClip() {
  const clip = activeClip();
  if (!clip) return;
  const title = prompt('Clip title:', clip.title);
  if (!title || title === clip.title) return;
  await runClipAction(() => requestJson(
    apiPaths.clip(state.current, clip.id), {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title}),
    }));
}

async function moveClip(offset) {
  const index = state.clips.findIndex(clip => clip.id === state.currentClip);
  const destination = index + offset;
  if (index < 0 || destination < 0 || destination >= state.clips.length) return;
  const clipIds = state.clips.map(clip => clip.id);
  [clipIds[index], clipIds[destination]] = [clipIds[destination], clipIds[index]];
  await runClipAction(() => requestJson(
    apiPaths.clipOrder(state.current), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({clip_ids: clipIds}),
    }));
}

async function toggleClip() {
  const clip = activeClip();
  if (!clip) return;
  await runClipAction(() => requestJson(
    apiPaths.clip(state.current, clip.id), {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: !clip.enabled}),
    }));
}

async function selectProject(projectId) {
  setWorkspacePane('chat');
  closeProjectMetadata(false, true);
  closeGenerationDialog(false);
  closeGenerationSettings(false);
  state.current = projectId;
  state.project = null;
  state.projectRevision += 1;
  state.currentClip = null;
  state.clipRevision += 1;
  state.clips = [];
  appState.refreshErrors = {};
  resetConversation('clip');
  resetClipState();
  resetReferences();
  resetMovieReview();
  renderProjects();
  renderClips();
  await refreshProject();
}

async function createProject() {
  const name = prompt('Project name:');
  if (!name) return;
  const brief = prompt('Brief (optional):') || '';
  const created = await requestJson(apiPaths.projects, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, brief}),
  });
  state.current = created.id;
  await loadProjects();
  await selectProject(created.id);
}

function reportRefreshPlane(name, error) {
  updateRefreshStatus(appState.refreshErrors, $('#status'), name, error);
}

function applyProjectNavigation(project) {
  const previousClip = state.currentClip;
  state.project = project;
  state.clips = project.clips || [];
  if (!state.clips.some(clip => clip.id === state.currentClip)) {
    state.currentClip = state.clips[0]?.id || null;
  }
  if (previousClip !== state.currentClip) {
    state.clipRevision += 1;
    closeGenerationDialog(false);
    closeGenerationSettings(false);
    resetClipState();
    if (conversationScope() === 'clip') {
      resetConversation('clip');
      appState.refreshPending = true;
    }
  }
  if (!state.currentClip && conversationScope() === 'clip') {
    resetConversation('project');
    appState.refreshPending = true;
  }
  renderProjects();
  renderClips();
  document.title = `${project.title} — Hermes Studio`;
}

async function refreshNavigationPlane(context) {
  let project;
  try {
    project = await requestJson(apiPaths.project(context.projectId));
  } catch (error) {
    if (isProjectContextCurrent(state, context)) {
      reportRefreshPlane('project', error);
    }
    return;
  }
  if (!isProjectContextCurrent(state, context)) return;
  reportRefreshPlane('project', null);
  applyProjectNavigation(project);
  if (!state.currentClip) {
    resetClipState();
    return;
  }
  const clipContext = captureClipContext(state);
  await refreshClipPlane({
    requestJson,
    paths: apiPaths,
    context: clipContext,
    isCurrent: () => isClipContextCurrent(state, clipContext),
    handlers: {
      clip: clip => {
        $('#prompt').textContent = clip.current_prompt || '—';
        renderGenerationReadiness(clip.generation_settings);
        document.title = `${clip.title} — ${project.title} — Hermes Studio`;
      },
      generations: applyGenerations,
    },
    report: reportRefreshPlane,
  });
}

async function refreshProject() {
  if (!state.current) return;
  if (appState.refreshing) {
    appState.refreshPending = true;
    return;
  }
  appState.refreshing = true;
  const projectContext = captureProjectContext(state);
  const isProjectCurrent = () =>
    isProjectContextCurrent(state, projectContext);
  try {
    await Promise.all([
      refreshNavigationPlane(projectContext),
      refreshConversation(reportRefreshPlane),
      refreshReferences(projectContext, isProjectCurrent, reportRefreshPlane),
      requestJson(apiPaths.movie(projectContext.projectId)).then(movieProject => {
        if (!isProjectCurrent()) return;
        reportRefreshPlane('movie', null);
        renderMovieProject(movieProject);
      }).catch(error => {
        if (isProjectCurrent()) reportRefreshPlane('movie', error);
      }),
    ]);
  } finally {
    appState.refreshing = false;
    if (appState.refreshPending) {
      appState.refreshPending = false;
      queueMicrotask(refreshProject);
    }
  }
}

for (const button of document.querySelectorAll('[data-workspace-pane]')) {
  button.addEventListener('click', () => setWorkspacePane(button.dataset.workspacePane));
}
$('#workspace-nav').addEventListener('keydown', handleWorkspaceNavigation);
narrowWorkspace.addEventListener('change', renderWorkspacePane);
renderWorkspacePane();
$('#new-project').addEventListener('click', createProject);
$('#edit-project').addEventListener('click', openProjectMetadata);
$('#new-clip').addEventListener('click', createClip);
$('#rename-clip').addEventListener('click', renameClip);
$('#move-clip-up').addEventListener('click', () => moveClip(-1));
$('#move-clip-down').addEventListener('click', () => moveClip(1));
$('#toggle-clip').addEventListener('click', toggleClip);
$('#project-metadata-form').addEventListener('submit', saveProjectMetadata);
$('#project-metadata-close').addEventListener(
  'click', () => closeProjectMetadata());
$('#project-metadata-cancel').addEventListener(
  'click', () => closeProjectMetadata());
$('#project-metadata-dialog').addEventListener('cancel', event => {
  if (appState.projectMetadataSaving) event.preventDefault();
});
$('#project-metadata-dialog').addEventListener('close', () => {
  if ($('#project-metadata-dialog').open) return;
  const opener = appState.projectMetadataOpener;
  if (appState.projectMetadataDialogContext) {
    appState.projectMetadataDialogRevision += 1;
    appState.projectMetadataDialogContext = null;
    appState.projectMetadataSaving = false;
  }
  if (opener?.isConnected) opener.focus();
  appState.projectMetadataOpener = null;
  renderProjectMetadataControls();
});
initializeGenerationSettings(refreshProject);
initializeMediaReview(refreshProject);
initializeReferenceController(refreshProject);
initializeConversationController({
  refreshProject,
  jobsChanged: () => {
    renderProjectMetadataControls();
    renderClips();
    rerenderGenerationReadiness();
    updateMovieExportControls();
  },
});
$('#comfy-queue').addEventListener('toggle', event => {
  if (event.currentTarget.open) refreshComfyQueue();
});

Promise.all([loadProfiles(), loadProjects(), refreshComfyQueue()]).catch(error => {
  $('#status').textContent = `startup error: ${error.message}`;
});
setInterval(refreshProject, 2000);
setInterval(refreshComfyQueue, 2000);

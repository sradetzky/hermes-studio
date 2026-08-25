import {$, activeClip, requestJson, showEmpty, state} from './shared.js';
import {
  conversationJobActive,
  queueConversationJob,
} from './conversation-controller.js';
import {invalidateReferences} from './reference-controller.js';
import {apiPaths} from './api-paths.mjs';
import {
  captureProjectContext,
  captureGenerationDialogContext,
  isGenerationDialogContextCurrent,
  isProjectContextCurrent,
} from './frontend-contracts.mjs';

let refreshProject = async () => {};
let initialized = false;

const media = {
  movieProject: null,
  movieSubmitting: false,
  generations: [],
  filteredGenerations: [],
  generationDetail: null,
  selectedGenerationFile: null,
  generationOpener: null,
  dialogRevision: 0,
  dialogContext: null,
  actioning: false,
  generationSignature: '',
};

function dialogContextState() {
  return {...state, generationDialogRevision: media.dialogRevision};
}

function isDialogCurrent(context) {
  return isGenerationDialogContextCurrent(dialogContextState(), context);
}

export function movieExportState(movieProject, jobActive, submitting) {
  const label = 'Export selected takes as movie';
  if (!movieProject) {
    return {enabled: false, label, status: 'Movie readiness unavailable'};
  }
  if (submitting) {
    return {enabled: false, label, status: 'Preparing immutable movie contract…'};
  }
  if (jobActive) {
    return {enabled: false, label, status: 'Wait for the active Studio job to finish'};
  }
  const readiness = movieProject.readiness;
  if (!readiness?.ready) {
    const blockers = readiness?.blocking || [];
    const status = blockers.length
      ? 'Blocked: ' + blockers.map(
        item => `${item.title} — ${item.reason}`).join(' · ')
      : 'Movie readiness unavailable';
    return {enabled: false, label, status};
  }
  return {
    enabled: true,
    label,
    status: `Ready · ${readiness.enabled_clip_count} selected clips · hard cuts`,
  };
}

function createMovieCard(movie) {
  const card = document.createElement('article');
  card.className = 'movie-card';
  card.dataset.movieId = movie.id;
  card.dataset.movieUrl = movie.url;
  const media = document.createElement('video');
  media.className = 'movie-media';
  media.src = movie.url;
  media.controls = true;
  media.preload = 'metadata';
  media.playsInline = true;
  const details = document.createElement('div');
  details.className = 'movie-details';
  const name = document.createElement('strong');
  name.textContent = movie.id;
  const meta = document.createElement('span');
  meta.textContent = `${movie.clip_count} clips · ${movie.assembly_mode} · ` +
    `${Number(movie.duration_seconds).toFixed(1)}s · ${formatBytes(movie.size)}`;
  const download = document.createElement('a');
  download.className = 'btn movie-download';
  download.href = movie.url;
  download.download = `${movie.id}.mp4`;
  download.textContent = 'Download MP4';
  details.append(name, meta, download);
  card.append(media, details);
  return card;
}

function reconcileMovies(movies) {
  const container = $('#movies');
  const existing = new Map(
    [...container.querySelectorAll('.movie-card')].map(
      card => [card.dataset.movieId, card]));
  let cursor = container.firstElementChild;
  for (const movie of movies) {
    let card = existing.get(movie.id);
    if (!card || card.dataset.movieUrl !== movie.url) {
      card = createMovieCard(movie);
    }
    existing.delete(movie.id);
    if (card === cursor) cursor = cursor.nextElementSibling;
    else container.insertBefore(card, cursor);
  }
  for (const card of existing.values()) card.remove();
  if (!movies.length) {
    container.replaceChildren();
    showEmpty(container, 'No project movies yet');
  } else {
    delete container.dataset.empty;
    container.querySelector('.empty')?.remove();
  }
}

export function updateMovieExportControls() {
  const action = movieExportState(
    media.movieProject, conversationJobActive(), media.movieSubmitting);
  const button = $('#export-movie');
  button.textContent = action.label;
  button.disabled = !action.enabled;
  $('#movie-readiness').textContent = action.status;
  const blockers = $('#movie-blockers');
  blockers.replaceChildren();
  for (const blocker of media.movieProject?.readiness?.blocking || []) {
    const item = document.createElement('li');
    item.textContent = `${blocker.title}: ${blocker.reason}`;
    blockers.append(item);
  }
}

export function renderMovieProject(movieProject) {
  media.movieProject = movieProject;
  reconcileMovies(movieProject?.movies || []);
  updateMovieExportControls();
}

async function exportMovie() {
  const context = captureProjectContext(state);
  const action = movieExportState(
    media.movieProject, conversationJobActive(), media.movieSubmitting);
  if (!action.enabled || !context.projectId) return;
  media.movieSubmitting = true;
  updateMovieExportControls();
  let failure = '';
  try {
    const job = await requestJson(apiPaths.movie(context.projectId), {method: 'POST'});
    if (!isProjectContextCurrent(state, context)) return;
    queueConversationJob(job);
    await refreshProject();
  } catch (error) {
    failure = `Export failed: ${error.message}`;
  } finally {
    if (isProjectContextCurrent(state, context)) {
      media.movieSubmitting = false;
      updateMovieExportControls();
      if (failure) $('#movie-readiness').textContent = failure;
    }
  }
}

export function takeDeletionMessage(generationId, selected) {
  const selectedWarning = selected
    ? ' This is the selected take; its selection will be cleared.' : '';
  return `Delete take ${generationId} and all of its archived files?` +
    `${selectedWarning} Promoted final and reference copies are kept. ` +
    'This cannot be undone.';
}

function isSelectedTake(generationId, filename) {
  const selected = activeClip()?.selected_take;
  return selected?.generation === generationId && selected?.filename === filename;
}

function generationRecipe(generation) {
  const meta = generation.meta || {};
  return String(meta.recipe || meta.kind || 'unknown');
}

function updateGenerationRecipeFilter() {
  const select = $('#generation-recipe-filter');
  const selected = select.value || 'all';
  const recipes = [...new Set(media.generations.map(generationRecipe))].sort();
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
  media.filteredGenerations = media.generations.filter(generationMatchesFilters);
  $('#generation-count').textContent = media.generations.length
    ? `${media.filteredGenerations.length}/${media.generations.length}` : '';
  if (!media.generations.length) {
    showEmpty(container, 'none yet');
    return;
  }
  if (!media.filteredGenerations.length) {
    showEmpty(container, 'no matching generations');
    return;
  }
  for (const generation of media.filteredGenerations) {
    const mediaItems = generation.media || [];
    const primary = mediaItems.find(item => item.kind === 'video') ||
      mediaItems.find(item => item.kind === 'image') || mediaItems[0];
    const card = document.createElement('div');
    card.className = 'generation-card';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'generation-preview';
    open.setAttribute('aria-label', `Review take ${generation.gen}`);
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
    if (mediaItems.some(item => isSelectedTake(generation.gen, item.name))) {
      const badge = document.createElement('span');
      badge.className = 'media-state-badge selected';
      badge.textContent = 'take';
      label.append(badge);
    }
    card.append(label);
    container.append(card);
  }
}

function showGenerationLoading(generationId) {
  $('#generation-title').textContent = `Take ${generationId}`;
  $('#generation-media').replaceChildren();
  showEmpty($('#generation-media'), 'Loading take…');
  $('#generation-files').replaceChildren();
  $('#generation-filename').textContent = '—';
  $('#generation-file-state').textContent = '';
  $('#generation-prompt').textContent = '—';
  $('#generation-meta').textContent = '{}';
  $('#generation-actions').replaceChildren();
  $('#media-action-status').textContent = '';
  $('#select-generation').disabled = true;
  $('#promote-generation').disabled = true;
  $('#reference-generation').disabled = true;
  $('#delete-generation').disabled = true;
}

async function openGeneration(generationId, opener = null) {
  const dialog = $('#generation-dialog');
  media.dialogRevision += 1;
  const context = captureGenerationDialogContext(dialogContextState(), generationId);
  media.dialogContext = context;
  media.generationOpener = opener || media.generationOpener;
  media.generationDetail = null;
  media.selectedGenerationFile = null;
  media.actioning = false;
  showGenerationLoading(generationId);
  if (!dialog.open) dialog.showModal();
  try {
    const detail = await requestJson(
      apiPaths.generation(context.projectId, context.clipId, generationId));
    if (!isDialogCurrent(context) || !dialog.open) return;
    media.generationDetail = detail;
    media.selectedGenerationFile = detail.media[0]?.name || null;
    renderGenerationDetail();
  } catch (error) {
    if (!isDialogCurrent(context) || !dialog.open) return;
    $('#media-action-status').textContent = `Unable to load: ${error.message}`;
    $('#generation-media').replaceChildren();
    showEmpty($('#generation-media'), 'take unavailable');
  }
}

function renderGenerationDetail() {
  const detail = media.generationDetail;
  if (!detail) return;
  $('#generation-title').textContent = `Take ${detail.gen}`;
  $('#generation-prompt').textContent = detail.prompt || '—';
  $('#generation-meta').textContent = JSON.stringify(detail.meta || {}, null, 2);
  const files = $('#generation-files');
  files.replaceChildren();
  for (const item of detail.media) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className =
      `media-file ${item.name === media.selectedGenerationFile ? 'active' : ''}`;
    button.textContent = `${item.kind} · ${item.name}`;
    button.title = item.name;
    button.addEventListener('click', () => {
      media.selectedGenerationFile = item.name;
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
  return media.generationDetail?.media.find(
    item => item.name === media.selectedGenerationFile) || null;
}

function renderSelectedGenerationMedia() {
  const item = selectedGenerationMedia();
  $('#delete-generation').disabled = !media.generationDetail || media.actioning;
  const stage = $('#generation-media');
  if (!item) {
    stage.replaceChildren();
    showEmpty(stage, 'No supported media in this take');
    $('#generation-filename').textContent = '—';
    $('#select-generation').disabled = true;
    $('#promote-generation').disabled = true;
    $('#reference-generation').disabled = true;
    return;
  }
  delete stage.dataset.empty;
  const mediaKey = JSON.stringify([item.kind, item.url]);
  let mediaNode = stage.querySelector('.detail-media');
  if (mediaNode?.dataset.mediaKey !== mediaKey) {
    if (item.kind === 'video') {
      mediaNode = document.createElement('video');
      mediaNode.controls = true;
      mediaNode.preload = 'metadata';
      mediaNode.playsInline = true;
    } else if (item.kind === 'image') {
      mediaNode = document.createElement('img');
      mediaNode.alt = item.name;
    } else {
      mediaNode = document.createElement('audio');
      mediaNode.controls = true;
    }
    mediaNode.src = item.url;
    mediaNode.className = 'detail-media';
    mediaNode.dataset.mediaKey = mediaKey;
    stage.replaceChildren(mediaNode);
  }
  $('#generation-filename').textContent = `${item.name} · ${formatBytes(item.size)}`;
  const states = [];
  const selectedTake = isSelectedTake(media.generationDetail.gen, item.name);
  if (item.promoted) states.push('Promoted to final');
  if (item.reference) states.push('Reference');
  if (selectedTake) states.push('Selected take');
  $('#generation-file-state').textContent = states.join(' · ');
  const select = $('#select-generation');
  const promote = $('#promote-generation');
  const reference = $('#reference-generation');
  select.textContent = selectedTake ? 'Selected take ✓' : 'Select take';
  promote.textContent = item.promoted ? 'Promoted ✓' : 'Promote to final';
  reference.textContent = item.reference ? 'Reference ✓' : 'Use as reference';
  select.disabled = media.actioning || item.kind !== 'video' ||
    selectedTake || !activeClip()?.enabled;
  promote.disabled = media.actioning || item.promoted;
  reference.disabled = media.actioning || item.reference;
}

function formatBytes(size) {
  if (!Number.isFinite(size)) return 'unknown size';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

async function performMediaAction(action) {
  const detail = media.generationDetail;
  const item = selectedGenerationMedia();
  if (!detail || !item || media.actioning) return;
  const context = media.dialogContext;
  if (!context || context.generationId !== detail.gen ||
      !isDialogCurrent(context)) return;
  media.actioning = true;
  renderSelectedGenerationMedia();
  const status = $('#media-action-status');
  status.textContent = action === 'promote' ? 'Promoting…' : 'Copying reference…';
  const endpoint = action === 'promote' ? 'promote' : 'use-as-reference';
  try {
    const response = await requestJson(
      apiPaths.generationAction(
        context.projectId, context.clipId, detail.gen, endpoint),
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({filename: item.name}),
      });
    if (!isDialogCurrent(context)) return;
    status.textContent = action === 'promote'
      ? `Promoted as ${response.result.target}`
      : `Reference saved as ${response.result.target}`;
    const refreshed = await requestJson(
      apiPaths.generation(context.projectId, context.clipId, detail.gen));
    if (!isDialogCurrent(context)) return;
    media.generationDetail = refreshed;
    media.selectedGenerationFile = item.name;
    media.generationSignature = '';
    if (action === 'reference') invalidateReferences();
    renderGenerationDetail();
    await refreshProject();
  } catch (error) {
    if (!isDialogCurrent(context)) return;
    status.textContent = `Action failed: ${error.message}`;
  } finally {
    if (isDialogCurrent(context)) {
      media.actioning = false;
      renderSelectedGenerationMedia();
    }
  }
}

async function selectTake() {
  const detail = media.generationDetail;
  const item = selectedGenerationMedia();
  const clip = activeClip();
  if (!detail || !item || item.kind !== 'video' || !clip?.enabled ||
      media.actioning) return;
  const context = media.dialogContext;
  if (!context || context.generationId !== detail.gen ||
      !isDialogCurrent(context)) return;
  media.actioning = true;
  renderSelectedGenerationMedia();
  const status = $('#media-action-status');
  status.textContent = 'Selecting take…';
  try {
    const response = await requestJson(
      apiPaths.selectedTake(context.projectId, context.clipId), {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({generation: detail.gen, filename: item.name}),
      });
    if (!isDialogCurrent(context)) return;
    state.clips = state.clips.map(entry =>
      entry.id === context.clipId ? response.clip : entry);
    media.generationSignature = '';
    status.textContent = `Selected ${detail.gen}/${item.name}`;
    renderGenerationDetail();
    renderGenerations();
    await refreshProject();
  } catch (error) {
    if (!isDialogCurrent(context)) return;
    status.textContent = `Selection failed: ${error.message}`;
  } finally {
    if (isDialogCurrent(context)) {
      media.actioning = false;
      renderSelectedGenerationMedia();
    }
  }
}

async function deleteTake() {
  const detail = media.generationDetail;
  const clip = activeClip();
  if (!detail || media.actioning) return;
  if (!globalThis.confirm(
    takeDeletionMessage(
      detail.gen, clip?.selected_take?.generation === detail.gen),
  )) return;

  const context = media.dialogContext;
  if (!context || context.generationId !== detail.gen ||
      !isDialogCurrent(context)) return;
  media.actioning = true;
  renderSelectedGenerationMedia();
  const status = $('#media-action-status');
  status.textContent = 'Deleting take…';
  try {
    const response = await requestJson(
      apiPaths.generation(context.projectId, context.clipId, detail.gen),
      {method: 'DELETE'},
    );
    if (!isDialogCurrent(context)) return;
    state.clips = state.clips.map(entry =>
      entry.id === context.clipId ? response.clip : entry);
    media.generationSignature = '';
    closeGenerationDialog(false);
    await refreshProject();
  } catch (error) {
    if (!isDialogCurrent(context)) return;
    status.textContent = `Delete failed: ${error.message}`;
  } finally {
    if (isDialogCurrent(context)
        && media.generationDetail?.gen === detail.gen) {
      media.actioning = false;
      renderSelectedGenerationMedia();
    }
  }
}

function updateGenerationNavigation() {
  const current = media.generationDetail?.gen;
  const index = media.filteredGenerations.findIndex(item => item.gen === current);
  $('#generation-previous').disabled =
    index < 0 || index >= media.filteredGenerations.length - 1;
  $('#generation-next').disabled = index <= 0;
}

function navigateGeneration(direction) {
  const current = media.generationDetail?.gen;
  const index = media.filteredGenerations.findIndex(item => item.gen === current);
  const nextIndex = index + direction;
  if (index < 0 || nextIndex < 0 || nextIndex >= media.filteredGenerations.length) return;
  openGeneration(media.filteredGenerations[nextIndex].gen);
}

function closeGenerationDialog(restoreFocus = true) {
  const dialog = $('#generation-dialog');
  const opener = media.generationOpener;
  if (media.dialogContext || dialog.open) {
    media.dialogRevision += 1;
  }
  media.dialogContext = null;
  media.generationDetail = null;
  media.selectedGenerationFile = null;
  media.generationOpener = null;
  media.actioning = false;
  if (dialog.open) dialog.close();
  if (restoreFocus && opener?.isConnected) queueMicrotask(() => opener.focus());
}

export function applyGenerations(generations) {
  const signature = JSON.stringify(generations.generations);
  if (signature === media.generationSignature) return;
  media.generationSignature = signature;
  media.generations = generations.generations;
  updateGenerationRecipeFilter();
  renderGenerations();
}

export function resetMediaReview() {
  closeGenerationDialog(false);
  media.generations = [];
  media.filteredGenerations = [];
  media.generationSignature = '';
  $('#gens').replaceChildren();
  $('#generation-count').textContent = '';
}

export function resetMovieReview() {
  media.movieSubmitting = false;
  renderMovieProject(null);
}

export {closeGenerationDialog, renderGenerations, updateGenerationRecipeFilter};

export function initializeMediaReview(refresh) {
  refreshProject = refresh;
  if (initialized) return;
  initialized = true;
  const dialog = $('#generation-dialog');
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
  $('#select-generation').addEventListener('click', selectTake);
  $('#promote-generation').addEventListener('click', () => performMediaAction('promote'));
  $('#reference-generation').addEventListener('click', () => performMediaAction('reference'));
  $('#delete-generation').addEventListener('click', deleteTake);
  $('#export-movie').addEventListener('click', exportMovie);
  dialog.addEventListener('close', () => {
    if (!dialog.open) closeGenerationDialog();
  });
  dialog.addEventListener('click', event => {
    if (event.target === dialog) closeGenerationDialog();
  });
  dialog.addEventListener('keydown', event => {
    if (event.target.closest(
      'video,audio,input,select,textarea,button,a,[contenteditable="true"]')) return;
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      navigateGeneration(1);
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      navigateGeneration(-1);
    }
  });
}

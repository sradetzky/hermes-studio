import {$, requestJson, showEmpty, state} from './shared.js';

let refreshProject = async () => {};

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

export {closeGenerationDialog, renderGenerations, updateGenerationRecipeFilter};

export function initializeMediaReview(refresh) {
  refreshProject = refresh;
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
  $('#promote-generation').addEventListener('click', () => performMediaAction('promote'));
  $('#reference-generation').addEventListener('click', () => performMediaAction('reference'));
  dialog.addEventListener('close', () => closeGenerationDialog());
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

import {$, activeClip, requestJson, state} from './shared.js';
import {
  conversationJobActive,
  queueConversationJob,
} from './conversation-controller.js';
import {apiPaths} from './api-paths.mjs';
import {
  captureClipContext,
  isClipContextCurrent,
  isSeedWithinRange,
  MAX_SAFE_SEED,
} from './frontend-contracts.mjs';

let refreshProject = async () => {};

export function generationActionState(
  contract, clipEnabled, jobActive, submitting,
) {
  if (submitting) {
    return {enabled: false, label: 'Submitting…', reason: 'Submitting generation request'};
  }
  if (jobActive) {
    return {
      enabled: false,
      label: 'Generate with this prompt',
      reason: 'Wait for the active Studio job to finish',
    };
  }
  if (clipEnabled === null) {
    return {
      enabled: false, label: 'Generate with this prompt', reason: 'Pick a clip first',
    };
  }
  if (!clipEnabled) {
    return {
      enabled: false,
      label: 'Generate with this prompt',
      reason: 'Enable this clip before generating',
    };
  }
  if (!contract?.readiness?.ready) {
    return {
      enabled: false,
      label: 'Generate with this prompt',
      reason: contract?.readiness?.reasons?.[0] || 'Generation settings are not ready',
    };
  }
  if (!contract.manifest?.prompt_sha256 || !contract.manifest?.updated_at) {
    return {
      enabled: false,
      label: 'Generate with this prompt',
      reason: 'Generation settings revision is unavailable',
    };
  }
  return {enabled: true, label: 'Generate with this prompt', reason: ''};
}

export function generationRequestPayload(contract) {
  return {
    prompt_sha256: contract.manifest.prompt_sha256,
    settings_updated_at: contract.manifest.updated_at,
  };
}

function renderGenerationAction() {
  const action = generationActionState(
    state.generationSettings,
    activeClip()?.enabled ?? null,
    conversationJobActive(),
    state.generationSubmitting,
  );
  const button = $('#generate-current-prompt');
  button.disabled = !action.enabled;
  button.textContent = action.label;
  button.title = action.reason;
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
    const seconds = readiness.timing?.requested_seconds;
    $('#generation-readiness-summary').textContent = [
      settings.mode.toUpperCase(),
      seconds ? `${seconds}s` : 'length from prompt',
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
  const actionStatus = $('#generation-action-status');
  if ((!contract && !state.generationSubmitting)
      || (!conversationJobActive() && !state.generationSubmitting
          && actionStatus.textContent === 'Generation queued.')) {
    actionStatus.textContent = '';
  }
  renderGenerationAction();
}

async function submitGeneration() {
  const action = generationActionState(
    state.generationSettings,
    activeClip()?.enabled ?? null,
    conversationJobActive(),
    state.generationSubmitting,
  );
  if (!action.enabled) return;
  const context = captureClipContext(state);
  const contract = state.generationSettings;
  state.generationSubmitting = true;
  renderGenerationAction();
  const status = $('#generation-action-status');
  status.textContent = 'Submitting generation request…';
  try {
    const job = await requestJson(apiPaths.generate(context.projectId, context.clipId), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(generationRequestPayload(contract)),
    });
    if (!isClipContextCurrent(state, context)) return;
    queueConversationJob(job);
    status.textContent = 'Generation queued.';
    await refreshProject();
  } catch (error) {
    if (!isClipContextCurrent(state, context)) return;
    status.textContent = `Generate failed: ${error.message}`;
    await refreshProject();
  } finally {
    if (isClipContextCurrent(state, context)) {
      state.generationSubmitting = false;
      renderGenerationAction();
    }
  }
}

async function openGenerationSettings(opener = null) {
  if (!state.current || !state.currentClip) return;
  state.settingsOpener = opener || state.settingsOpener;
  const dialog = $('#generation-settings-dialog');
  const save = $('#generation-settings-save');
  save.disabled = true;
  state.generationSettingsOptions = null;
  $('#generation-settings-status').textContent = 'Loading settings…';
  if (!dialog.open) dialog.showModal();
  const context = captureClipContext(state);
  try {
    const contract = await requestJson(
      apiPaths.generationSettings(context.projectId, context.clipId));
    if (!isClipContextCurrent(state, context) || !dialog.open) return;
    state.generationSettings = contract;
    state.generationSettingsOptions = contract.options;
    populateGenerationSettings(contract);
    save.disabled = false;
    $('#generation-settings-status').textContent = '';
  } catch (error) {
    if (!isClipContextCurrent(state, context)) return;
    $('#generation-settings-status').textContent = `Unable to load: ${error.message}`;
  }
}

function populateGenerationSettings(contract) {
  const settings = contract.settings;
  $('#setting-mode').value = settings.mode;
  $('#setting-aspect').value = settings.aspect;
  $('#setting-mp').value = settings.mp;
  $('#setting-size-mode').value = settings.width === null ? 'mp' : 'explicit';
  $('#setting-width').value = settings.width ?? '';
  $('#setting-height').value = settings.height ?? '';
  $('#setting-seed').value = settings.seed ?? '';
  $('#setting-steps').value = settings.steps;
  $('#setting-accel').checked = settings.accel;
  updateSeedValidity();
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
  updateComputedSettings();
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
  const mp = width && height ? (width * height / 1_000_000).toFixed(3) : '?';
  $('#settings-computed').textContent = Number.isFinite(width) && Number.isFinite(height)
    ? `${width}×${height} · ${mp}MP · length and references come from the prompt`
    : 'Enter a valid canvas';
}

function updateSeedValidity() {
  const input = $('#setting-seed');
  const maxSeed = state.generationSettingsOptions?.max_seed || MAX_SAFE_SEED;
  input.setCustomValidity(isSeedWithinRange(input.value.trim(), maxSeed)
    ? '' : `Seed must be decimal digits from 0 to ${maxSeed}`);
}

function generationSettingsPayload() {
  const explicit = $('#setting-size-mode').value === 'explicit';
  const seedValue = $('#setting-seed').value.trim();
  return {
    mode: $('#setting-mode').value,
    aspect: $('#setting-aspect').value,
    mp: Number($('#setting-mp').value),
    width: explicit ? Number($('#setting-width').value) : null,
    height: explicit ? Number($('#setting-height').value) : null,
    seed: seedValue || null,
    steps: Number($('#setting-steps').value),
    accel: $('#setting-accel').checked,
  };
}

async function saveGenerationSettings(event) {
  event.preventDefault();
  const form = $('#generation-settings-form');
  updateSeedValidity();
  if (!form.reportValidity()) return;
  const status = $('#generation-settings-status');
  const save = $('#generation-settings-save');
  if (save.disabled || !state.generationSettingsOptions) return;
  save.disabled = true;
  status.textContent = 'Saving settings…';
  const context = captureClipContext(state);
  try {
    const contract = await requestJson(
      apiPaths.generationSettings(context.projectId, context.clipId), {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(generationSettingsPayload()),
      });
    if (!isClipContextCurrent(state, context)) return;
    renderGenerationReadiness(contract);
    $('#generation-action-status').textContent = '';
    closeGenerationSettings();
    await refreshProject();
  } catch (error) {
    if (!isClipContextCurrent(state, context)) return;
    save.disabled = false;
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
  $('#generation-settings-save').disabled = true;
  if (dialog.open) dialog.close();
  if (restoreFocus && opener?.isConnected) queueMicrotask(() => opener.focus());
}

export {closeGenerationSettings, renderGenerationReadiness};

export function initializeGenerationSettings(refresh) {
  refreshProject = refresh;
  const dialog = $('#generation-settings-dialog');
  $('#generate-current-prompt').addEventListener('click', submitGeneration);
  $('#edit-generation-settings').addEventListener('click', event =>
    openGenerationSettings(event.currentTarget));
  $('#generation-settings-form').addEventListener('submit', saveGenerationSettings);
  $('#setting-seed').addEventListener('input', updateSeedValidity);
  $('#generation-settings-close').addEventListener('click', () => closeGenerationSettings());
  $('#generation-settings-cancel').addEventListener('click', () => closeGenerationSettings());
  dialog.addEventListener('close', () => closeGenerationSettings());
  dialog.addEventListener('click', event => {
    if (event.target === dialog) closeGenerationSettings();
  });
  for (const field of [
    $('#setting-mode'), $('#setting-aspect'),
    $('#setting-size-mode'), $('#setting-mp'), $('#setting-width'),
    $('#setting-height'),
  ]) {
    field.addEventListener('input', updateGenerationSettingsForm);
    field.addEventListener('change', updateGenerationSettingsForm);
  }
  $('#settings-preview-preset').addEventListener('click', () =>
    applySettingsPreset(0.5, 8));
  $('#settings-final-preset').addEventListener('click', () =>
    applySettingsPreset(0.9, 20));
}

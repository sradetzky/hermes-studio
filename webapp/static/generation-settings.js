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
let initialized = false;

const generation = {
  contract: null,
  options: null,
  submitting: false,
  opener: null,
  previousTakeSignature: '',
};

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

export function generationRequestPayload(contract, usePreviousTakeLastFrame = false) {
  return {
    prompt_sha256: contract.manifest.prompt_sha256,
    settings_updated_at: contract.manifest.updated_at,
    use_previous_take_last_frame: usePreviousTakeLastFrame,
  };
}

export function previousTakeInputState(contract) {
  const input = contract?.previous_selected_take_input;
  if (!input?.eligible) return {visible: false, enabled: false, reason: ''};
  if (contract?.settings?.mode !== 'r2v') {
    return {
      visible: true,
      enabled: false,
      reason: 'Switch generation mode to R2V to use continuity',
    };
  }
  return {visible: true, enabled: true, reason: ''};
}

function renderPreviousTakeInput(contract) {
  const option = $('#previous-take-input-option');
  const checkbox = $('#use-previous-take-last-frame');
  const input = contract?.previous_selected_take_input;
  const state = previousTakeInputState(contract);
  const signature = input?.eligible ? [
    input.source_clip_id,
    input.source_generation_id,
    input.source_filename,
    input.picture_number,
    contract?.settings?.mode,
  ].join('\u0000') : '';
  if (signature !== generation.previousTakeSignature) checkbox.checked = false;
  generation.previousTakeSignature = signature;
  option.hidden = !state.visible;
  checkbox.disabled = !state.enabled;
  checkbox.title = state.reason;
  if (!state.visible) return;
  $('#previous-take-input-label').textContent =
    `Use previous selected take’s last frame as <Picture ${input.picture_number}>`;
  $('#previous-take-input-source').textContent =
    state.reason ||
    `${input.source_clip_id} · take ${input.source_generation_id} · ${input.source_filename}`;
}

function renderGenerationAction() {
  const action = generationActionState(
    generation.contract,
    activeClip()?.enabled ?? null,
    conversationJobActive(),
    generation.submitting,
  );
  const button = $('#generate-current-prompt');
  button.disabled = !action.enabled;
  button.textContent = action.label;
  button.title = action.reason;
}

function renderGenerationReadiness(contract) {
  generation.contract = contract;
  renderPreviousTakeInput(contract);
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
  if ((!contract && !generation.submitting)
      || (!conversationJobActive() && !generation.submitting
          && actionStatus.textContent === 'Generation queued.')) {
    actionStatus.textContent = '';
  }
  renderGenerationAction();
}

async function submitGeneration() {
  const action = generationActionState(
    generation.contract,
    activeClip()?.enabled ?? null,
    conversationJobActive(),
    generation.submitting,
  );
  if (!action.enabled) return;
  const context = captureClipContext(state);
  const contract = generation.contract;
  generation.submitting = true;
  renderGenerationAction();
  const status = $('#generation-action-status');
  status.textContent = 'Submitting generation request…';
  try {
    const job = await requestJson(apiPaths.generate(context.projectId, context.clipId), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(generationRequestPayload(
        contract,
        $('#use-previous-take-last-frame').checked,
      )),
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
      generation.submitting = false;
      renderGenerationAction();
    }
  }
}

async function openGenerationSettings(opener = null) {
  if (!state.current || !state.currentClip) return;
  generation.opener = opener || generation.opener;
  const dialog = $('#generation-settings-dialog');
  const save = $('#generation-settings-save');
  save.disabled = true;
  generation.options = null;
  $('#generation-settings-status').textContent = 'Loading settings…';
  if (!dialog.open) dialog.showModal();
  const context = captureClipContext(state);
  try {
    const contract = await requestJson(
      apiPaths.generationSettings(context.projectId, context.clipId));
    if (!isClipContextCurrent(state, context) || !dialog.open) return;
    generation.contract = contract;
    generation.options = contract.options;
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
  const maxSeed = generation.options?.max_seed || MAX_SAFE_SEED;
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
  if (save.disabled || !generation.options) return;
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
  const opener = generation.opener;
  generation.opener = null;
  generation.options = null;
  $('#generation-settings-save').disabled = true;
  if (dialog.open) dialog.close();
  if (restoreFocus && opener?.isConnected) queueMicrotask(() => opener.focus());
}

export function rerenderGenerationReadiness() {
  renderGenerationReadiness(generation.contract);
}

export function resetGenerationSettings() {
  closeGenerationSettings(false);
  generation.contract = null;
  generation.submitting = false;
  generation.previousTakeSignature = '';
  $('#use-previous-take-last-frame').checked = false;
  renderGenerationReadiness(null);
}

export {closeGenerationSettings, renderGenerationReadiness};

export function initializeGenerationSettings(refresh) {
  refreshProject = refresh;
  if (initialized) return;
  initialized = true;
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

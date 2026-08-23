import {$, requestJson, showEmpty, state} from './shared.js';

let refreshProject = async () => {};

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

export {closeGenerationSettings, renderGenerationReadiness};

export function initializeGenerationSettings(refresh) {
  refreshProject = refresh;
  const dialog = $('#generation-settings-dialog');
  $('#edit-generation-settings').addEventListener('click', event =>
    openGenerationSettings(event.currentTarget));
  $('#generation-settings-form').addEventListener('submit', saveGenerationSettings);
  $('#generation-settings-close').addEventListener('click', () => closeGenerationSettings());
  $('#generation-settings-cancel').addEventListener('click', () => closeGenerationSettings());
  dialog.addEventListener('close', () => closeGenerationSettings());
  dialog.addEventListener('click', event => {
    if (event.target === dialog) closeGenerationSettings();
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
}

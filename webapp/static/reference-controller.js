import {apiPaths} from './api-paths.mjs';
import {
  captureProjectContext,
  isProjectContextCurrent,
} from './frontend-contracts.mjs';
import {refreshReferencePlane} from './refresh-planes.mjs';
import {$, requestJson, state} from './shared.js';

let referenceSignature = '';
let uploading = false;
let refreshProject = async () => {};
let initialized = false;

function renderReferences(projectId, references) {
  const container = $('#refs');
  container.replaceChildren();
  for (const filename of references) {
    const source = `/media/projects/${encodeURIComponent(projectId)}/references/` +
      encodeURIComponent(filename);
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

export function invalidateReferences() {
  referenceSignature = '';
}

export function resetReferences() {
  invalidateReferences();
  $('#refs').replaceChildren();
}

export async function refreshReferences(context, isCurrent, report) {
  await refreshReferencePlane({
    requestJson,
    paths: apiPaths,
    context,
    isCurrent,
    apply: references => {
      const signature = JSON.stringify(references.references);
      if (signature === referenceSignature) return;
      referenceSignature = signature;
      renderReferences(context.projectId, references.references);
    },
    report,
  });
}

function uploadReferences(files) {
  if (!state.current || !files.length || uploading) return Promise.resolve();
  const context = captureProjectContext(state);
  uploading = true;
  const form = new FormData();
  for (const file of files) form.append('files', file);
  const dropText = $('#drop-text');
  dropText.textContent = `Uploading ${files.length} file${files.length === 1 ? '' : 's'}…`;
  $('#dropzone').classList.add('uploading');
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', apiPaths.references(context.projectId));
    request.upload.onprogress = event => {
      if (event.lengthComputable && isProjectContextCurrent(state, context)) {
        dropText.textContent =
          `Uploading… ${Math.round(event.loaded / event.total * 100)}%`;
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
    if (!isProjectContextCurrent(state, context)) return;
    invalidateReferences();
    $('#status').textContent = '';
    await refreshProject();
  }).catch(error => {
    if (isProjectContextCurrent(state, context)) {
      $('#status').textContent = `upload error: ${error.message}`;
    }
  }).finally(() => {
    uploading = false;
    $('#dropzone').classList.remove('uploading');
    dropText.textContent = 'Drop references here or click to browse';
    $('#file-input').value = '';
  });
}

export function initializeReferenceController(refresh) {
  refreshProject = refresh;
  if (initialized) return;
  initialized = true;
  const dropzone = $('#dropzone');
  const fileInput = $('#file-input');
  dropzone.addEventListener('click', () => {
    if (state.current && !uploading) fileInput.click();
    else if (!state.current) alert('Pick a project first');
  });
  dropzone.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') dropzone.click();
  });
  for (const eventName of ['dragenter', 'dragover']) {
    dropzone.addEventListener(eventName, event => {
      event.preventDefault();
      if (!uploading) dropzone.classList.add('drag');
    });
  }
  for (const eventName of ['dragleave', 'drop']) {
    dropzone.addEventListener(eventName, event => {
      event.preventDefault();
      dropzone.classList.remove('drag');
    });
  }
  dropzone.addEventListener(
    'drop', event => uploadReferences(event.dataTransfer.files));
  fileInput.addEventListener('change', () => uploadReferences(fileInput.files));
}

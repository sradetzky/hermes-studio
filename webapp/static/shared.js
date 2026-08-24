export const $ = selector => document.querySelector(selector);

export const state = {
  current: null,
  project: null,
  currentClip: null,
  workspacePane: 'chat',
  projectRevision: 0,
  clipRevision: 0,
  chatRevision: 0,
  chatScope: 'clip',
  clips: [],
  projects: [],
  profiles: [],
  chatCursor: 0,
  activityCursor: 0,
  activityByJob: {},
  jobs: [],
  jobActive: false,
  showActivityDetails: true,
  generations: [],
  filteredGenerations: [],
  generationDetail: null,
  selectedGenerationFile: null,
  generationOpener: null,
  mediaActioning: false,
  generationSettings: null,
  generationSettingsOptions: null,
  generationSubmitting: false,
  settingsOpener: null,
  generationSignature: '',
  referenceSignature: '',
  refreshing: false,
  refreshPending: false,
  refreshErrors: {},
  uploading: false,
  projectMetadataSaving: false,
  projectMetadataOpener: null,
};

export function activeClip() {
  return state.clips.find(clip => clip.id === state.currentClip) || null;
}

export async function requestJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.status;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

export function showEmpty(element, text) {
  const empty = document.createElement('div');
  empty.className = 'empty';
  empty.textContent = text;
  element.dataset.empty = 'true';
  element.append(empty);
}

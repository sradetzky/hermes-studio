export const $ = selector => document.querySelector(selector);

export const state = {
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
  settingsOpener: null,
  generationSignature: '',
  referenceSignature: '',
  refreshing: false,
  refreshPending: false,
  uploading: false,
};;

export async function requestJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.status;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
};

export function showEmpty(element, text) {
  const empty = document.createElement('div');
  empty.className = 'empty';
  empty.textContent = text;
  element.dataset.empty = 'true';
  element.append(empty);
};

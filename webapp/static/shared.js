export const $ = selector => document.querySelector(selector);

export const state = {
  current: null,
  project: null,
  currentClip: null,
  workspacePane: 'chat',
  projectRevision: 0,
  clipRevision: 0,
  clips: [],
  projects: [],
  profiles: [],
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

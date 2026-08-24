export const WORKSPACE_PANES = Object.freeze(['projects', 'chat', 'media']);

export function normalizeWorkspacePane(value) {
  return WORKSPACE_PANES.includes(value) ? value : 'chat';
}

export function moveWorkspacePane(value, offset) {
  const current = WORKSPACE_PANES.indexOf(normalizeWorkspacePane(value));
  const next = (current + offset + WORKSPACE_PANES.length) % WORKSPACE_PANES.length;
  return WORKSPACE_PANES[next];
}

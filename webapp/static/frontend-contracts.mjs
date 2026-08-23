export const MAX_SAFE_SEED = '9007199254740991';

export function isSeedWithinRange(value, maxSeed = MAX_SAFE_SEED) {
  if (value === '') return true;
  if (!/^[0-9]+$/.test(value)) return false;
  const normalized = value.replace(/^0+(?=\d)/, '');
  return normalized.length < maxSeed.length ||
    (normalized.length === maxSeed.length && normalized <= maxSeed);
}

export function captureProjectContext(state) {
  return {projectId: state.current, revision: state.projectRevision};
}

export function captureClipContext(state) {
  return {
    ...captureProjectContext(state),
    clipId: state.currentClip,
    clipRevision: state.clipRevision,
  };
}

export function isProjectContextCurrent(state, context) {
  return context.projectId === state.current &&
    context.revision === state.projectRevision;
}

export function isClipContextCurrent(state, context) {
  return isProjectContextCurrent(state, context) &&
    context.clipId === state.currentClip &&
    context.clipRevision === state.clipRevision;
}

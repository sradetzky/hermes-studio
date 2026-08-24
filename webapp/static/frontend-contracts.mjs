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

export function captureProjectDialogContext(state) {
  return {
    ...captureProjectContext(state),
    dialogRevision: state.projectMetadataDialogRevision,
  };
}

export function captureGenerationDialogContext(state, generationId) {
  return {
    ...captureClipContext(state),
    generationId,
    dialogRevision: state.generationDialogRevision,
  };
}

export function captureChatContext(state) {
  const clipScoped = state.chatScope === 'clip';
  return {
    ...captureProjectContext(state),
    chatScope: clipScoped ? 'clip' : 'project',
    chatRevision: state.chatRevision,
    requestRevision: state.chatRequestRevision,
    clipId: clipScoped ? state.currentClip : null,
    clipRevision: clipScoped ? state.clipRevision : null,
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

export function isProjectDialogContextCurrent(state, context) {
  return isProjectContextCurrent(state, context) &&
    context.dialogRevision === state.projectMetadataDialogRevision;
}

export function isGenerationDialogContextCurrent(state, context) {
  return isClipContextCurrent(state, context) &&
    context.dialogRevision === state.generationDialogRevision;
}

export function isChatContextCurrent(state, context) {
  if (!isProjectContextCurrent(state, context) ||
      context.chatScope !== state.chatScope ||
      context.chatRevision !== state.chatRevision ||
      context.requestRevision !== state.chatRequestRevision) return false;
  if (context.chatScope !== 'clip') return true;
  return context.clipId === state.currentClip &&
    context.clipRevision === state.clipRevision;
}

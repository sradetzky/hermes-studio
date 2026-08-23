async function settlePlane(name, load, isCurrent, apply, report) {
  try {
    const value = await load();
    if (!isCurrent()) return;
    apply(value);
    report(name, null);
  } catch (error) {
    if (isCurrent()) report(name, error);
  }
}

export async function refreshLivePlane({
  requestJson, paths, context, cursors, isCurrent, handlers, report,
}) {
  await Promise.all([
    settlePlane(
      'chat', () => requestJson(paths.chat(context.projectId, cursors.chat)),
      isCurrent, handlers.chat, report),
    settlePlane(
      'jobs', () => requestJson(paths.jobs(context.projectId)),
      isCurrent, handlers.jobs, report),
    settlePlane(
      'activity',
      () => requestJson(paths.events(context.projectId, cursors.activity)),
      isCurrent, handlers.activity, report),
  ]);
}

export async function refreshReferencePlane({
  requestJson, paths, context, isCurrent, apply, report,
}) {
  await settlePlane(
    'references', () => requestJson(paths.references(context.projectId)),
    isCurrent, apply, report);
}

export async function refreshClipPlane({
  requestJson, paths, context, isCurrent, handlers, report,
}) {
  if (!context.clipId) return;
  await Promise.all([
    settlePlane(
      'clip', () => requestJson(paths.clip(context.projectId, context.clipId)),
      isCurrent, handlers.clip, report),
    settlePlane(
      'generations',
      () => requestJson(paths.generations(context.projectId, context.clipId)),
      isCurrent, handlers.generations, report),
  ]);
}

export {settlePlane};

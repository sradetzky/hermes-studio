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
  const clipScoped = context.chatScope === 'clip' && context.clipId;
  const chatPath = clipScoped
    ? paths.clipChat(context.projectId, context.clipId, cursors.chat)
    : paths.chat(context.projectId, cursors.chat);
  const activityPath = clipScoped
    ? paths.clipEvents(context.projectId, context.clipId, cursors.activity)
    : paths.events(context.projectId, cursors.activity);
  await Promise.all([
    settlePlane(
      'chat', () => requestJson(chatPath),
      isCurrent, handlers.chat, report),
    settlePlane(
      'jobs', () => requestJson(paths.jobs(context.projectId)),
      isCurrent, handlers.jobs, report),
    settlePlane(
      'activity',
      () => requestJson(activityPath),
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

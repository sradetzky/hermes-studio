export function queuePresentation(snapshot) {
  if (!snapshot?.available) {
    return {state: 'offline', label: 'Comfy unavailable'};
  }
  const running = snapshot.running?.length || 0;
  const pending = snapshot.pending?.length || 0;
  if (running) {
    const queued = pending ? ` · ${pending} queued` : '';
    return {state: 'running', label: `Comfy ${running} running${queued}`};
  }
  if (pending) return {state: 'queued', label: `Comfy ${pending} queued`};
  return {state: 'idle', label: 'Comfy idle'};
}

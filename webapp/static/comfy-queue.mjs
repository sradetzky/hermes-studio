export function formatQueueDuration(value) {
  if (!Number.isFinite(value) || value < 0) return '';
  const seconds = Math.floor(value);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
  }
  return `${minutes}:${String(remainder).padStart(2, '0')}`;
}

export function queueJobTitle(job) {
  const parts = [job?.recipe, job?.mode].filter(Boolean);
  return parts.length ? parts.join(' ') : 'Comfy workflow';
}

export function queueJobSpecs(job) {
  const specs = [];
  if (Number.isInteger(job?.width) && Number.isInteger(job?.height)) {
    specs.push(`${job.width}×${job.height}`);
  }
  if (Number.isInteger(job?.media_seconds) && Number.isInteger(job?.frames)) {
    specs.push(`~${job.media_seconds}s / ${job.frames}f`);
  } else if (Number.isInteger(job?.frames)) {
    specs.push(`${job.frames}f`);
  }
  if (Number.isInteger(job?.steps)) {
    specs.push(`${job.steps} ${job.steps === 1 ? 'step' : 'steps'}`);
  }
  if (job?.accel === true) specs.push('accel');
  return specs.join(' · ');
}

export function queuePresentation(snapshot) {
  if (!snapshot?.available) {
    return {state: 'offline', label: 'Comfy unavailable'};
  }
  const runningJobs = snapshot.running || [];
  const running = runningJobs.length;
  const pending = snapshot.pending?.length || 0;
  if (running) {
    const primary = runningJobs[0];
    const title = queueJobTitle(primary);
    const elapsed = formatQueueDuration(primary.elapsed_seconds);
    const queued = pending ? ` · ${pending} queued` : '';
    const timing = elapsed ? ` · ${elapsed}` : '';
    return {state: 'running', label: `Comfy · ${title}${timing}${queued}`};
  }
  if (pending) return {state: 'queued', label: `Comfy ${pending} queued`};
  return {state: 'idle', label: 'Comfy idle'};
}

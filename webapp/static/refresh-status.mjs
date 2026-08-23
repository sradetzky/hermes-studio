export function updateRefreshStatus(refreshErrors, status, name, error) {
  if (error) refreshErrors[name] = error.message;
  else delete refreshErrors[name];
  const failures = Object.entries(refreshErrors);
  if (failures.length) {
    status.textContent = `refresh: ${failures.map(
      ([plane, message]) => `${plane}: ${message}`).join(' · ')}`;
  } else if (status.textContent.startsWith('refresh:')) {
    status.textContent = '';
  }
}

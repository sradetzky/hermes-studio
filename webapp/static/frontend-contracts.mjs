export const MAX_SAFE_SEED = '9007199254740991';

export function isSeedWithinRange(value, maxSeed = MAX_SAFE_SEED) {
  if (value === '') return true;
  if (!/^[0-9]+$/.test(value)) return false;
  const normalized = value.replace(/^0+(?=\d)/, '');
  return normalized.length < maxSeed.length ||
    (normalized.length === maxSeed.length && normalized <= maxSeed);
}

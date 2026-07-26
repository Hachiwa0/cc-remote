import type { Engine, EngineCapabilityItem, Space } from "./protocol";

export const SKILL_CATALOG_TTL_MS = 5 * 60_000;
export const MAX_SKILL_CATALOGS = 32;

export interface SkillCatalogCacheEntry {
  items: EngineCapabilityItem[];
  fetchedAt: number;
}

export const skillCatalogKey = (
  machineId: string,
  engine: Engine,
  space: Space,
  cwd: string,
): string => [machineId, engine, space, cwd || "."].join("\u0000");

export const skillCatalogFresh = (
  entry: SkillCatalogCacheEntry | undefined,
  now = Date.now(),
): boolean => !!entry && now - entry.fetchedAt < SKILL_CATALOG_TTL_MS;

export function cacheSkillCatalog(
  current: Record<string, SkillCatalogCacheEntry>,
  key: string,
  items: EngineCapabilityItem[],
  fetchedAt = Date.now(),
): Record<string, SkillCatalogCacheEntry> {
  const next = { ...current, [key]: { items, fetchedAt } };
  const keys = Object.keys(next);
  if (keys.length <= MAX_SKILL_CATALOGS) return next;
  keys.sort((a, b) => next[a].fetchedAt - next[b].fetchedAt);
  for (const oldKey of keys.slice(0, keys.length - MAX_SKILL_CATALOGS)) {
    delete next[oldKey];
  }
  return next;
}

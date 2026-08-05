import type { ThreadGoal } from "./protocol";

export const GOAL_UI_PREFERENCES_KEY = "cc-remote-goal-ui-v1";
const MAX_GOAL_UI_PREFERENCES = 256;

export interface GoalUiPreference {
  known: true;
  hiddenGoal?: string;
  seenAt: number;
}

export type GoalUiPreferences = Record<string, GoalUiPreference>;

export interface GoalEventOwnership {
  machineId: string;
  engine: "claude" | "codex";
  space: "code" | "work";
}

export function goalUiScopeKey(
  machineId: string,
  space: "code" | "work",
  engine: "claude" | "codex",
  sid: string,
): string {
  return JSON.stringify([machineId, space, engine, sid]);
}

export function goalUiScopeForEvent(
  sid: string,
  ownership?: GoalEventOwnership,
): string | null {
  if (!ownership) return null;
  return goalUiScopeKey(
    ownership.machineId,
    ownership.space,
    ownership.engine,
    sid,
  );
}

export function goalStableIdentity(goal: ThreadGoal | null | undefined): string | null {
  if (!goal) return null;
  const marker = goal.createdAt ?? goal.setAt;
  if (typeof marker !== "number" || !Number.isFinite(marker) || marker < 0) {
    return null;
  }
  return JSON.stringify([goal.engine, goal.threadId, marker]);
}

function boundPreferences(preferences: GoalUiPreferences): GoalUiPreferences {
  const entries = Object.entries(preferences)
    .filter(([, value]) => value?.known === true
      && Number.isFinite(value.seenAt) && value.seenAt >= 0)
    .sort(([, left], [, right]) => right.seenAt - left.seenAt)
    .slice(0, MAX_GOAL_UI_PREFERENCES);
  return Object.fromEntries(entries);
}

export function readGoalUiPreferences(
  storage: Pick<Storage, "getItem">,
): GoalUiPreferences {
  try {
    const raw = storage.getItem(GOAL_UI_PREFERENCES_KEY);
    if (!raw) return {};
    const decoded = JSON.parse(raw) as unknown;
    if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) {
      return {};
    }
    const preferences: GoalUiPreferences = {};
    for (const [key, candidate] of Object.entries(decoded)) {
      if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
        continue;
      }
      const value = candidate as Partial<GoalUiPreference>;
      if (value.known !== true || typeof value.seenAt !== "number"
          || !Number.isFinite(value.seenAt) || value.seenAt < 0) {
        continue;
      }
      preferences[key] = {
        known: true,
        seenAt: value.seenAt,
        ...(typeof value.hiddenGoal === "string" && value.hiddenGoal
          ? { hiddenGoal: value.hiddenGoal } : {}),
      };
    }
    return boundPreferences(preferences);
  } catch {
    return {};
  }
}

export function writeGoalUiPreferences(
  storage: Pick<Storage, "setItem">,
  preferences: GoalUiPreferences,
): GoalUiPreferences {
  const bounded = boundPreferences(preferences);
  try {
    storage.setItem(GOAL_UI_PREFERENCES_KEY, JSON.stringify(bounded));
  } catch {
    // Private browsing and managed devices may block storage. The caller keeps
    // the in-memory copy so Goal remains usable for the current page lifetime.
  }
  return bounded;
}

export function rememberGoalUi(
  preferences: GoalUiPreferences,
  key: string,
  now = Date.now(),
): GoalUiPreferences {
  return boundPreferences({
    ...preferences,
    [key]: { known: true, seenAt: now },
  });
}

export function dismissGoalUi(
  preferences: GoalUiPreferences,
  key: string,
  goal: ThreadGoal | null | undefined,
  now = Date.now(),
): GoalUiPreferences {
  const hiddenGoal = goalStableIdentity(goal);
  return boundPreferences({
    ...preferences,
    [key]: {
      known: true,
      seenAt: now,
      ...(hiddenGoal ? { hiddenGoal } : {}),
    },
  });
}

export function reconcileGoalUiPreference(
  preferences: GoalUiPreferences,
  key: string,
  goal: ThreadGoal | null | undefined,
  now = Date.now(),
): { preferences: GoalUiPreferences; revealed: boolean } {
  const current = preferences[key];
  if (!current?.known) return { preferences, revealed: false };
  const identity = goalStableIdentity(goal);
  const hidden = !!identity && current.hiddenGoal === identity;
  const next = boundPreferences({
    ...preferences,
    [key]: {
      known: true,
      seenAt: now,
      ...(hidden ? { hiddenGoal: current.hiddenGoal } : {}),
    },
  });
  return { preferences: next, revealed: !!goal && !hidden };
}

export function rekeyGoalUiPreference(
  preferences: GoalUiPreferences,
  oldKey: string,
  nextKey: string,
): GoalUiPreferences {
  if (oldKey === nextKey || !preferences[oldKey]) return preferences;
  const next = { ...preferences };
  const previous = next[oldKey];
  delete next[oldKey];
  const existing = next[nextKey];
  next[nextKey] = !existing || previous.seenAt >= existing.seenAt
    ? previous : existing;
  return boundPreferences(next);
}

/** Browser-memory bounds for long-lived multi-session tabs.
 *
 * Completed history is recoverable from the authoritative transcript, so it is
 * safe to evict old completed turns and inactive runtimes. Work that has not
 * reached a durable boundary (focused, queued, outbox-targeted, replaying, or a
 * currently-synchronised active turn) is deliberately retained.
 */

export const MAX_RUNTIME_SESSIONS = 64;
export const MAX_RUNTIME_TURNS = 200;
export const MAX_RUNTIME_COMPLETED_UNITS = 16 * 1024 * 1024;

interface SizedImage { media_type?: string; data?: string }
interface SizedFile { filename?: string; data?: string }
interface SizedTextBlock { kind: "text"; message_id?: string; text?: string }
interface SizedToolBlock {
  kind: "tool";
  message_id?: string;
  tool_use_id?: string;
  tool?: string;
  input?: unknown;
  result?: { content?: string };
}

export interface BoundedTurn {
  done: boolean;
  id?: string;
  prompt?: string;
  error?: string;
  progress?: string;
  images?: SizedImage[];
  files?: SizedFile[];
  blocks?: Array<SizedTextBlock | SizedToolBlock>;
}

function textUnits(value: string | undefined): number {
  return value?.length ?? 0;
}

function turnUnits(turn: BoundedTurn, stopAfter: number): number {
  let units = 256 + textUnits(turn.id) + textUnits(turn.prompt)
    + textUnits(turn.error) + textUnits(turn.progress);
  for (const image of turn.images ?? []) {
    units += 64 + textUnits(image.media_type) + textUnits(image.data);
    if (units > stopAfter) return units;
  }
  for (const file of turn.files ?? []) {
    units += 64 + textUnits(file.filename) + textUnits(file.data);
    if (units > stopAfter) return units;
  }
  for (const block of turn.blocks ?? []) {
    units += 128 + textUnits(block.message_id);
    if (block.kind === "text") {
      units += textUnits(block.text);
    } else {
      units += textUnits(block.tool_use_id) + textUnits(block.tool)
        + textUnits(block.result?.content);
      try { units += JSON.stringify(block.input ?? {}).length; }
      catch { units = stopAfter + 1; }
    }
    if (units > stopAfter) return units;
  }
  return units;
}

/** Keep every in-flight turn and the newest bounded window of completed turns.
 * The newest completed turn is retained even if it alone exceeds the soft byte
 * budget, so completing a large image/tool turn never makes it disappear. */
export function boundRuntimeTurns<T extends BoundedTurn>(
  turns: readonly T[],
  maxTurns = MAX_RUNTIME_TURNS,
  maxCompletedUnits = MAX_RUNTIME_COMPLETED_UNITS,
): T[] {
  if (!turns.length) return turns as T[];
  const activeCount = turns.reduce((count, turn) => count + (turn.done ? 0 : 1), 0);
  const completedSlots = Math.max(0, maxTurns - activeCount);
  const keep = new Array<boolean>(turns.length).fill(false);
  let completedKept = 0;
  let completedUnits = 0;

  for (let index = turns.length - 1; index >= 0; index--) {
    const turn = turns[index];
    if (!turn.done) {
      keep[index] = true;
      continue;
    }
    if (completedKept >= completedSlots) continue;
    const size = turnUnits(turn, maxCompletedUnits);
    if (completedKept > 0 && completedUnits + size > maxCompletedUnits) continue;
    keep[index] = true;
    completedKept += 1;
    completedUnits += size;
  }

  if (keep.every(Boolean)) return turns as T[];
  return turns.filter((_, index) => keep[index]);
}

export interface RetainedRuntime {
  state: string;
  syncReady: boolean;
  replaying: boolean;
  turns: Array<{ done: boolean }>;
  queue: unknown[];
  pendingSend: unknown | null;
  pendingQuestion: unknown | null;
}

function hasLiveWork(runtime: RetainedRuntime): boolean {
  if (runtime.queue.length || runtime.pendingSend || runtime.replaying) return true;
  if (!runtime.syncReady) return false;
  return runtime.state !== "idle" || runtime.pendingQuestion !== null
    || runtime.turns.some((turn) => !turn.done);
}

/** Oldest-first reclamation by insertion order. The normal idle pool stays at `maxSessions`;
 * explicitly protected/outstanding work may overflow it, but those sources are
 * independently bounded (outbox 256, queued queries 32, wrapper residents 64). */
export function pruneRuntimeMap<T extends RetainedRuntime>(
  runtimes: Record<string, T>,
  explicitlyProtected: ReadonlySet<string>,
  maxSessions = MAX_RUNTIME_SESSIONS,
): Record<string, T> {
  const keys = Object.keys(runtimes);
  if (keys.length <= maxSessions) return runtimes;
  const next = { ...runtimes };
  let remaining = keys.length;

  // Idle/inactive entries are cheapest to reconstruct and go first.
  for (const sid of keys) {
    if (remaining <= maxSessions) break;
    if (explicitlyProtected.has(sid) || hasLiveWork(runtimes[sid])) continue;
    delete next[sid];
    remaining -= 1;
  }
  // A disconnected runtime can retain stale "running" state from an old wrapper
  // generation. If protected entries still force an overflow, discard only these
  // unconfirmed remnants; ReplayStart/Snapshot recreates current residents.
  for (const sid of keys) {
    if (remaining <= maxSessions) break;
    const runtime = runtimes[sid];
    if (!Object.hasOwn(next, sid) || explicitlyProtected.has(sid) || runtime.syncReady
        || runtime.replaying || runtime.queue.length || runtime.pendingSend) continue;
    delete next[sid];
    remaining -= 1;
  }
  return remaining === keys.length ? runtimes : next;
}

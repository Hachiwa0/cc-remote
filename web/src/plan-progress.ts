import type { Block, ProcessBlock, Turn } from "./domain/conversation.ts";
import type { ThreadGoal } from "./protocol.ts";
import { mergeDetailWithLiveTail } from "./history-merge.ts";

export interface TurnPlanProgress {
  turnId: string;
  block: ProcessBlock;
  detailLoading: boolean;
  needsDetail: boolean;
  /** Millisecond start time of the user turn that owns this Plan. */
  turnStartedAt?: number;
  /** False only when a durable snapshot was rebound to a later unrelated turn. */
  ownerMatchesTurn?: boolean;
}

export type TurnPlanProgressSource = "runtime" | "history";

export interface ScopedTurnPlanProgress extends TurnPlanProgress {
  source: TurnPlanProgressSource;
}

const MAX_SESSION_PLAN_PROGRESS = 128;

function turnOwnsProgress(turn: Turn, turnId: string): boolean {
  return [
    turn.id,
    turn.clientMsgId,
    turn.historyTurnId,
    turn.forkPointId,
  ].some((candidate) => candidate === turnId);
}

function copyProgress(
  progress: TurnPlanProgress,
  source: TurnPlanProgressSource,
): ScopedTurnPlanProgress {
  return {
    ...progress,
    source,
    block: {
      ...progress.block,
      plan: progress.block.plan?.map((entry) => ({ ...entry })),
    },
  };
}

/** Keep the latest structured Plan independently for each visited session.
 *
 * Codex's full turn detail currently omits update_plan. The summary page and
 * live stream still carry the Plan, so retaining one bounded display snapshot
 * prevents a detail read followed by A -> B -> A navigation from losing the
 * session-level control. Entries are discarded after their owning turn is
 * authoritatively absent, when a completed Plan is followed by a new user
 * turn, or when an invalidation explicitly clears the sid.
 */
export class SessionPlanProgressCache {
  private readonly entries = new Map<string, ScopedTurnPlanProgress>();
  private readonly maxEntries: number;

  constructor(maxEntries = MAX_SESSION_PLAN_PROGRESS) {
    this.maxEntries = maxEntries;
  }

  resolve({
    sid,
    runtime,
    history,
    runtimeTurns,
    historyTurns,
    recovering,
    runtimeLoading,
  }: {
    sid: string;
    runtime: TurnPlanProgress | null;
    history: TurnPlanProgress | null;
    runtimeTurns: readonly Turn[];
    historyTurns: readonly Turn[];
    recovering: boolean;
    runtimeLoading: boolean;
  }): ScopedTurnPlanProgress | null {
    const selected = runtime ?? history;
    if (selected) {
      const selectedTurns = runtime ? runtimeTurns : historyTurns;
      const newerTurns = runtime ? [] : runtimeTurns;
      if (completedPlanHasNewerTurn(
        selected, selectedTurns, newerTurns)) {
        this.entries.delete(sid);
        return null;
      }
      const entry = copyProgress(
        selected, runtime ? "runtime" : "history");
      this.entries.delete(sid);
      this.entries.set(sid, entry);
      while (this.entries.size > this.maxEntries) {
        const oldest = this.entries.keys().next().value;
        if (typeof oldest !== "string") break;
        this.entries.delete(oldest);
      }
      return entry;
    }

    const retained = this.entries.get(sid);
    if (!retained) return null;
    const turns = retained.source === "history" ? historyTurns : runtimeTurns;
    const owner = turns.find((turn) => turnOwnsProgress(
      turn, retained.turnId));
    const newerTurns = retained.source === "history" ? runtimeTurns : [];
    if (owner && completedPlanHasNewerTurn(
      retained, turns, newerTurns)) {
      this.entries.delete(sid);
      return null;
    }
    if (!owner && !recovering && !runtimeLoading) {
      this.entries.delete(sid);
      return null;
    }
    // Touch on focus so the bounded cache evicts genuinely old sessions first.
    this.entries.delete(sid);
    const resolved = {
      ...retained,
      detailLoading: owner?.detailLoading === true,
      needsDetail: owner
        ? !owner.detailLoaded && (owner.detailEventCount ?? 0) > 0
        : retained.needsDetail,
    };
    this.entries.set(sid, resolved);
    return resolved;
  }

  clear(sid: string): void {
    this.entries.delete(sid);
  }

  rekey(oldSid: string, sid: string): void {
    if (oldSid === sid) return;
    const retained = this.entries.get(oldSid);
    if (!retained) return;
    this.entries.delete(oldSid);
    this.entries.delete(sid);
    this.entries.set(sid, retained);
  }
}

/** Resolve the newest plan-bearing turn in a conversation projection.
 *
 * Codex records a task plan on the turn which created it, while later steer or
 * follow-up turns can continue an unfinished task without repeating that plan.
 * Search turns newest-first so old sessions keep their latest active monitor,
 * but retire a completed Plan as soon as the next user turn begins.
 */
export function latestPlanProgress(
  turns: readonly Turn[],
): TurnPlanProgress | null {
  for (let index = turns.length - 1; index >= 0; index--) {
    const progress = turnPlanProgress(turns[index]);
    if (!progress) continue;
    return completedPlanHasNewerTurn(progress, turns) ? null : progress;
  }
  return null;
}

function completedGoalAtMs(
  goal: ThreadGoal | null | undefined,
): number | null {
  if (goal?.status !== "complete"
      || typeof goal.updatedAt !== "number"
      || !Number.isFinite(goal.updatedAt)) return null;
  // The Goal API uses epoch seconds; conversation Turn timestamps use epoch
  // milliseconds throughout the reducer and IndexedDB projection.
  return goal.updatedAt * 1000;
}

function turnHasUserContent(turn: Turn): boolean {
  return turn.prompt.trim().length > 0
    || (turn.images?.length ?? 0) > 0
    || (turn.imageRefs?.length ?? 0) > 0
    || (turn.files?.length ?? 0) > 0;
}

/** A completed Goal retires from passive presentation on the next user turn. */
export function completedGoalHasNewerUserTurn(
  goal: ThreadGoal | null | undefined,
  turns: readonly Turn[],
): boolean {
  const completedAt = completedGoalAtMs(goal);
  if (completedAt == null) return false;
  return turns.some((turn) => turnHasUserContent(turn)
    && typeof turn.ts === "number" && Number.isFinite(turn.ts)
    && turn.ts > completedAt);
}

/** Only a Plan from after the completion boundary may outlive the old Goal. */
export function planFollowsCompletedGoal(
  goal: ThreadGoal | null | undefined,
  progress: TurnPlanProgress,
): boolean {
  const completedAt = completedGoalAtMs(goal);
  return completedAt != null
    && typeof progress.turnStartedAt === "number"
    && Number.isFinite(progress.turnStartedAt)
    && progress.turnStartedAt > completedAt
    && progress.ownerMatchesTurn !== false;
}

function completedPlanHasNewerTurn(
  progress: TurnPlanProgress,
  turns: readonly Turn[],
  newerTurns: readonly Turn[] = [],
): boolean {
  if (!planProgressPresentation(progress.block).complete) return false;
  const ownerIndex = turns.findIndex((turn) =>
    turnOwnsProgress(turn, progress.turnId));
  if (ownerIndex >= 0
      && turns.slice(ownerIndex + 1).some(turnHasUserContent)) return true;
  if (newerTurns.length === 0) return false;
  const newerOwnerIndex = newerTurns.findIndex((turn) =>
    turnOwnsProgress(turn, progress.turnId));
  if (newerOwnerIndex >= 0) {
    return newerTurns.slice(newerOwnerIndex + 1).some(turnHasUserContent);
  }
  const ownerStartedAt = progress.turnStartedAt
    ?? (ownerIndex >= 0 ? turns[ownerIndex].ts : undefined);
  return newerTurns.some((turn) => !turnOwnsProgress(turn, progress.turnId)
    && turnHasUserContent(turn)
    && (ownerStartedAt == null || turn.ts == null || turn.ts > ownerStartedAt));
}

function turnPlanProgress(turn: Turn): TurnPlanProgress | null {
  const preferCompletedDetail = turn.done
    && !turn.detailRestorePending && !turn.detailRestoreIncomplete;
  const plansOnly = (blocks: readonly Block[]) =>
    blocks.filter((block): block is ProcessBlock =>
      block.kind === "process" && block.processKind === "plan");
  const withArchive = mergeDetailWithLiveTail(
    plansOnly(turn.detailProjection?.blocks ?? []),
    plansOnly(turn.liveSpillBlocks ?? []),
    preferCompletedDetail,
  );
  const blocks = mergeDetailWithLiveTail(
    withArchive,
    plansOnly(turn.blocks),
    preferCompletedDetail,
  );
  // Match ProcessTimeline: prefer the newest structured update, while still
  // supporting older app-server records which only contain free-form detail.
  const block = [...blocks].reverse().find((candidate) =>
    candidate.kind === "process" && candidate.plan != null) ?? blocks.at(-1);
  if (!block) return null;
  // plansOnly() makes every merged block a ProcessBlock. Keep the narrowing
  // explicit at this module boundary so future merge helpers can stay generic.
  if (block.kind !== "process") return null;
  return {
    turnId: turn.id,
    block,
    detailLoading: turn.detailLoading === true,
    needsDetail: !turn.detailLoaded && (turn.detailEventCount ?? 0) > 0,
    turnStartedAt: turn.ts,
    ownerMatchesTurn: !block.turn_id
      || turnOwnsProgress(turn, block.turn_id),
  };
}

export interface PlanProgressPresentation {
  completed: number;
  total: number;
  currentStep: string | null;
  failed: boolean;
  complete: boolean;
  progress: number;
  progressLabel: string;
  stateLabel: string;
  description: string | null;
  fallbackDetail: string | null;
}

export function planProgressPresentation(
  block: ProcessBlock,
  detailLoading = false,
): PlanProgressPresentation {
  const steps = block.plan ?? [];
  const completed = steps.filter((entry) =>
    entry.status === "completed").length;
  const current = steps.find((entry) => entry.status === "inProgress");
  const failed = ["failed", "declined", "cancelled", "interrupted"]
    .includes(block.status);
  // A successful terminal turn does not imply that unfinished structured plan
  // steps ran; step state remains authoritative whenever it exists.
  const complete = !failed && steps.length > 0 && completed === steps.length;
  const progress = steps.length > 0
    ? Math.min(100, completed / steps.length * 100)
    : 0;
  return {
    completed,
    total: steps.length,
    currentStep: current?.step ?? null,
    failed,
    complete,
    progress,
    progressLabel: steps.length > 0
      ? `${completed} / ${steps.length}`
      : block.done ? "已记录" : "执行中",
    stateLabel: detailLoading ? "正在同步" : failed ? "执行异常"
      : complete ? "全部完成" : block.done ? "执行已结束"
        : current ? "正在执行" : "等待执行",
    description: block.explanation || block.summary || null,
    fallbackDetail: steps.length === 0
      ? block.detail || block.output || block.progress || null
      : null,
  };
}

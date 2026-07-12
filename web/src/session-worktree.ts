import type { SessionInfo, State } from "./protocol";

export const WORKTREE_FORK_NAME_MAX = 80;

export interface SessionMenuCapabilities {
  rename: boolean;
  archive: boolean;
  forkWorktree: boolean;
}

export interface PendingWorktreeFork {
  requestId: string;
  parentSessionId: string;
}

export function sessionMenuCapabilities(session: SessionInfo): SessionMenuCapabilities {
  return {
    rename: true,
    archive: true,
    forkWorktree: session.engine === "codex" && session.tag !== "archived",
  };
}

export function isWorktreeForkBlockedByState(state?: State | null): boolean {
  return state === "running" || state === "interrupting";
}

export function normalizeWorktreeForkName(value: string): string {
  return value.trim();
}

export function isWorktreeForkNameValid(value: string): boolean {
  const normalized = normalizeWorktreeForkName(value);
  return normalized.length > 0 && normalized.length <= WORKTREE_FORK_NAME_MAX;
}

export function matchesWorktreeForkRequest(
  pending: PendingWorktreeFork | null,
  requestId: string | null | undefined,
  parentSessionId?: string | null,
): boolean {
  return pending !== null
    && requestId === pending.requestId
    && (parentSessionId == null || parentSessionId === pending.parentSessionId);
}

/** Relay-side offline errors are provisional: the reliable command remains in
 * the outbox and will be replayed after the wrapper reconnects. */
export function isTerminalWorktreeForkError(code: string): boolean {
  return code !== "wrapper_offline";
}

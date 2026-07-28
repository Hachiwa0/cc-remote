import type { SessionInfo } from "./protocol";

export const HISTORY_REQUEST_TIMEOUT_MS = 15_000;
export const HISTORY_DETAIL_REQUEST_TIMEOUT_MS = 30_000;

export interface HistoryRequestKey {
  sid: string;
  before?: string | null;
  limit: number;
  generation?: string | null;
  revision?: string | null;
  browse?: HistoryBrowseRequestContext;
}

/** Request-time authority for one display-only deep-history waiter.
 *
 * This metadata is deliberately local: protocol v21 already identifies the
 * immutable server page with sid/revision/generation/before.  Freezing the
 * browser view here prevents a delayed page from being relabelled with the
 * machine/surface/session that happens to be active when it arrives.
 */
export interface HistoryBrowseRequestContext {
  scopeKey: string;
  viewId: string;
  windowEpoch: number;
  pendingBefore: string;
  sourcePageKey: string | null;
  anchorTurnId?: string | null;
}

/** Resolve an optional acceleration hint from ownership-accepted session lists.
 *
 * The wrapper remains authoritative. If two accepted engine/space scopes ever
 * claim the same id with different directories, omit the hint and let the SDK
 * perform its global session lookup.
 */
export function resolveHistoryCwdHint(
  lists: Readonly<Record<string, readonly SessionInfo[]>>,
  sid: string,
): string | undefined {
  const hints = new Set<string>();
  for (const sessions of Object.values(lists)) {
    const cwd = sessions.find((session) => session.session_id === sid)?.cwd?.trim();
    if (cwd) hints.add(cwd);
  }
  return hints.size === 1 ? hints.values().next().value : undefined;
}

interface PendingHistoryRequest extends HistoryRequestKey {
  connectionEpoch: number;
  startedAt: number;
  browseWaiters: HistoryBrowseRequestContext[];
}

/** One authority for every focus/reconnect/rebuild history trigger.
 *
 * App historically had four independent call sites.  Each generated a new
 * reliable command id, so wrapper-side command dedupe could not recognize that
 * they all requested the same page.  This coordinator merges those triggers
 * within a connection while still allowing a newer rollback revision, wrapper
 * generation, or pagination cursor to issue its own read.
 */
export class HistoryRequestCoordinator {
  private connectionEpoch = 0;
  private readonly pending = new Map<string, PendingHistoryRequest>();
  private readonly now: () => number;
  private readonly timeoutMs: number;

  constructor(
    now: () => number = () => Date.now(),
    timeoutMs = HISTORY_REQUEST_TIMEOUT_MS,
  ) {
    this.now = now;
    this.timeoutMs = timeoutMs;
  }

  beginConnection(): void {
    this.connectionEpoch += 1;
    this.pending.clear();
  }

  clear(): void {
    this.pending.clear();
  }

  private static key(request: HistoryRequestKey): string {
    return `${request.sid}\u0000${request.before ?? ""}\u0000${request.limit}`;
  }

  request(request: HistoryRequestKey, send: () => boolean): boolean {
    const key = HistoryRequestCoordinator.key(request);
    const existing = this.pending.get(key);
    const now = this.now();
    if (existing && existing.connectionEpoch === this.connectionEpoch
        && now - existing.startedAt < this.timeoutMs) {
      // A newly revealed destructive revision must issue a replacement even
      // when an ordinary focus read is already in flight.  The reverse is safe:
      // a later generic reconnect trigger can share the revision-bound read.
      const sameRevision = existing.revision
        ? !request.revision || existing.revision === request.revision
        : !request.revision;
      // A generic focus read already on the wire cannot satisfy a replay which
      // has since revealed its exact wrapper generation. Send a replacement
      // instead of relabelling the old request: its response may belong to the
      // previous generation and will be rejected by the reducer. The reverse
      // remains safe because a generic trigger can share a generation-bound
      // request already in flight.
      const sameGeneration = existing.generation
        ? !request.generation || existing.generation === request.generation
        : !request.generation;
      if (sameRevision && sameGeneration) {
        if (request.browse) {
          const duplicate = existing.browseWaiters.some((waiter) =>
            waiter.scopeKey === request.browse!.scopeKey
            && waiter.viewId === request.browse!.viewId
            && waiter.windowEpoch === request.browse!.windowEpoch
            && waiter.pendingBefore === request.browse!.pendingBefore
            && waiter.sourcePageKey === request.browse!.sourcePageKey
            && (waiter.anchorTurnId ?? null)
              === (request.browse!.anchorTurnId ?? null));
          if (!duplicate) existing.browseWaiters.push({ ...request.browse });
          // The local anchor has a real waiter even though the immutable wire
          // page was already in flight.
          return !duplicate;
        }
        return false;
      }
    }
    const pending: PendingHistoryRequest = {
      ...request,
      connectionEpoch: this.connectionEpoch,
      startedAt: now,
      browseWaiters: request.browse ? [{ ...request.browse }] : [],
    };
    // Outbox saturation/disconnection is a real rejection. Do not leave a
    // phantom pending entry which suppresses the user's next pagination
    // attempt while no command exists on the wire.
    if (!send()) return false;
    this.pending.set(key, pending);
    return true;
  }

  complete(response: {
    session_id: string;
    before?: string | null;
    generation?: string | null;
    revision?: string | null;
  }): HistoryBrowseRequestContext[] {
    const browseWaiters: HistoryBrowseRequestContext[] = [];
    for (const [key, pending] of this.pending) {
      if (pending.sid !== response.session_id
          || (pending.before ?? "") !== (response.before ?? "")) continue;
      if (pending.generation
          && pending.generation !== response.generation) continue;
      if (pending.revision
          && pending.revision !== response.revision) continue;
      browseWaiters.push(...pending.browseWaiters.map((waiter) => ({ ...waiter })));
      this.pending.delete(key);
    }
    return browseWaiters;
  }

  size(): number {
    return this.pending.size;
  }
}

export type HistoryDetailRequestContext =
  | {
      target: "runtime";
      scopeKey: string;
      sid: string;
      revision: string;
      turnId: string;
      before?: string | null;
      /** Initial user expansion pages to EOF; refresh repair reads one page. */
      autoLoad?: boolean;
    }
  | {
      target: "browse";
      scopeKey: string;
      sid: string;
      revision: string;
      turnId: string;
      before?: string | null;
      viewId: string;
      /** Diagnostic request-time page epoch. Detail remains valid after safe
       * pagination inside the same scope/view/revision while the row exists. */
      windowEpoch: number;
    };

/** Correlate protocol-v21 TurnDetail responses with the projection that asked.
 *
 * TurnDetail has no request id. The wire key is nevertheless exact because one
 * revision contains at most one canonical detail row per sid/turn. Keeping the
 * target frozen locally prevents a response requested in browse mode from
 * mutating the live runtime after navigation.
 */
export class HistoryDetailRequestCoordinator {
  private readonly pending = new Map<string, {
    context: HistoryDetailRequestContext;
    timer: unknown;
  }>();
  private readonly onTimeout: (context: HistoryDetailRequestContext) => void;
  private readonly schedule: (
    callback: () => void,
    delayMs: number,
  ) => unknown;
  private readonly cancelTimer: (timer: unknown) => void;
  private readonly timeoutMs: number;

  constructor(
    onTimeout: (context: HistoryDetailRequestContext) => void =
      () => undefined,
    schedule: (callback: () => void, delayMs: number) => unknown =
      (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
    cancelTimer: (timer: unknown) => void =
      (timer) => globalThis.clearTimeout(
        timer as ReturnType<typeof globalThis.setTimeout>),
    timeoutMs = HISTORY_DETAIL_REQUEST_TIMEOUT_MS,
  ) {
    this.onTimeout = onTimeout;
    this.schedule = schedule;
    this.cancelTimer = cancelTimer;
    this.timeoutMs = timeoutMs;
  }

  private static key(input: {
    session_id?: string;
    sid?: string;
    revision: string;
    turn_id?: string;
    turnId?: string;
    before?: string | null;
  }): string {
    return `${input.session_id ?? input.sid ?? ""}\u0000${input.revision}`
      + `\u0000${input.turn_id ?? input.turnId ?? ""}`
      + `\u0000${input.before ?? ""}`;
  }

  begin(context: HistoryDetailRequestContext): boolean {
    const key = HistoryDetailRequestCoordinator.key(context);
    if (this.pending.has(key)) return false;
    const pending = {
      context: { ...context },
      timer: null as unknown,
    };
    this.pending.set(key, pending);
    try {
      pending.timer = this.schedule(() => {
        if (this.pending.get(key) !== pending) return;
        this.pending.delete(key);
        this.onTimeout({ ...pending.context });
      }, this.timeoutMs);
    } catch {
      this.pending.delete(key);
      return false;
    }
    return true;
  }

  complete(response: {
    session_id: string;
    revision: string;
    turn_id: string;
    before?: string | null;
  }): HistoryDetailRequestContext | null {
    const key = HistoryDetailRequestCoordinator.key(response);
    const pending = this.pending.get(key);
    if (!pending) return null;
    this.pending.delete(key);
    this.cancelTimer(pending.timer);
    return { ...pending.context };
  }

  cancel(context: HistoryDetailRequestContext): void {
    const key = HistoryDetailRequestCoordinator.key(context);
    const pending = this.pending.get(key);
    if (!pending) return;
    if (pending.context.target !== context.target
        || pending.context.scopeKey !== context.scopeKey) return;
    this.pending.delete(key);
    this.cancelTimer(pending.timer);
  }

  clear(): HistoryDetailRequestContext[] {
    const contexts = [...this.pending.values()].map((pending) => {
      this.cancelTimer(pending.timer);
      return { ...pending.context };
    });
    this.pending.clear();
    return contexts;
  }
}

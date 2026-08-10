export interface CompletionReceipt {
  main: boolean;
  mainCompletionId: string | null;
  mainTurnEndSeq: number | null;
  mainTurnEndGeneration: string | null;
  btwSids: string[];
}

export type CompletionReceipts = Record<string, CompletionReceipt>;
export type CompletionBadgeKind = "main" | "btw" | "both";
export interface CompletionProjection {
  id: string | null;
  unread: boolean;
  revision: number;
}

export function catalogCompletionProjection(
  session: {
    completion_id?: string | null;
    completion_unread?: boolean | null;
    completion_revision?: number | null;
  } | null | undefined,
): CompletionProjection | null {
  if (session?.completion_unread == null
      || session.completion_revision == null) return null;
  return {
    id: session.completion_id ?? null,
    unread: session.completion_unread,
    revision: session.completion_revision,
  };
}

export function newestCompletionProjection(
  runtime: CompletionProjection | null | undefined,
  catalog: CompletionProjection | null | undefined,
): CompletionProjection | null {
  if (!runtime) return catalog ?? null;
  if (!catalog || runtime.revision >= catalog.revision) return runtime;
  return catalog;
}

export function markCompletionUnread(
  current: CompletionReceipts,
  parentSid: string,
  sourceSid: string,
  source: "main" | "btw",
  completionId: string | null = null,
  turnEndSeq: number | null = null,
  turnEndGeneration: string | null = null,
): CompletionReceipts {
  const prior = current[parentSid] ?? {
    main: false, mainCompletionId: null, mainTurnEndSeq: null,
    mainTurnEndGeneration: null, btwSids: [],
  };
  if (source === "main") {
    if (prior.main && prior.mainCompletionId === completionId
        && (completionId != null || (
          prior.mainTurnEndSeq === turnEndSeq
          && prior.mainTurnEndGeneration === turnEndGeneration
        ))) {
      return current;
    }
    return {
      ...current,
      [parentSid]: {
        ...prior,
        main: true,
        mainCompletionId: completionId,
        mainTurnEndSeq: turnEndSeq,
        mainTurnEndGeneration: turnEndGeneration,
      },
    };
  }
  if (prior.btwSids.includes(sourceSid)) return current;
  return {
    ...current,
    [parentSid]: {
      ...prior,
      btwSids: [...prior.btwSids, sourceSid],
    },
  };
}

export function acknowledgeCompletion(
  current: CompletionReceipts,
  parentSid: string,
  options: { main?: boolean; btwSid?: string },
): CompletionReceipts {
  const prior = current[parentSid];
  if (!prior) return current;
  const main = options.main ? false : prior.main;
  const mainCompletionId = options.main ? null : prior.mainCompletionId;
  const mainTurnEndSeq = options.main ? null : prior.mainTurnEndSeq;
  const mainTurnEndGeneration = options.main
    ? null : prior.mainTurnEndGeneration;
  const btwSids = options.btwSid
    ? prior.btwSids.filter((sid) => sid !== options.btwSid)
    : prior.btwSids;
  if (main === prior.main
      && mainCompletionId === prior.mainCompletionId
      && mainTurnEndSeq === prior.mainTurnEndSeq
      && mainTurnEndGeneration === prior.mainTurnEndGeneration
      && btwSids.length === prior.btwSids.length) {
    return current;
  }
  const next = { ...current };
  if (!main && btwSids.length === 0) delete next[parentSid];
  else next[parentSid] = {
    main, mainCompletionId, mainTurnEndSeq, mainTurnEndGeneration, btwSids,
  };
  return next;
}

/** Clear a local main fallback when the authoritative read receipt names the
 * same completion, or when an ordered clear follows its TurnEnd. A delayed
 * catalog/read frame for an older completion must not hide a newer turn_end
 * fallback which has not received its durable completion_state yet.
 */
export function acknowledgeMatchingCompletion(
  current: CompletionReceipts,
  parentSid: string,
  authoritative: CompletionProjection | null | undefined,
  options: {
    authoritativeSeq?: number | null;
    authoritativeGeneration?: string | null;
  } = {},
): CompletionReceipts {
  const local = current[parentSid];
  if (!local?.main || authoritative?.unread !== false) return current;
  const identityMatches = !!authoritative.id
    && local.mainCompletionId === authoritative.id;
  // A downstream CompletionState is ordered in the same per-session sequence
  // as TurnEnd. That causal boundary lets an authoritative generated id clear
  // a legacy/null-id fallback without letting an unordered catalog row do so.
  const causallyClearsFallback = (
    authoritative.id == null || local.mainCompletionId == null
  )
    && local.mainTurnEndSeq != null
    && local.mainTurnEndGeneration != null
    && options.authoritativeSeq != null
    && options.authoritativeGeneration === local.mainTurnEndGeneration
    && options.authoritativeSeq > local.mainTurnEndSeq;
  if (!identityMatches && !causallyClearsFallback) return current;
  return acknowledgeCompletion(current, parentSid, { main: true });
}

export function completionAcknowledgementId(
  receipt: CompletionReceipt | undefined,
  authoritative: { id: string | null; unread: boolean } | null | undefined,
): string | null {
  if (authoritative?.unread && authoritative.id) return authoritative.id;
  return receipt?.main ? receipt.mainCompletionId : null;
}

export function completionBadgeKind(
  receipt: CompletionReceipt | undefined,
  authoritativeUnread?: boolean | null,
): CompletionBadgeKind | undefined {
  const main = receipt?.main === true || authoritativeUnread === true;
  const btwSids = receipt?.btwSids ?? [];
  if (main && btwSids.length > 0) return "both";
  if (main) return "main";
  if (btwSids.length > 0) return "btw";
  return undefined;
}

export function rekeyCompletionReceipts(
  current: CompletionReceipts,
  oldSid: string,
  newSid: string,
): CompletionReceipts {
  if (oldSid === newSid || !current[oldSid]) return current;
  const source = current[oldSid];
  const target = current[newSid];
  const next = { ...current };
  delete next[oldSid];
  next[newSid] = target ? {
    main: target.main || source.main,
    mainCompletionId: source.main
      ? source.mainCompletionId : target.mainCompletionId,
    mainTurnEndSeq: source.main
      ? source.mainTurnEndSeq : target.mainTurnEndSeq,
    mainTurnEndGeneration: source.main
      ? source.mainTurnEndGeneration : target.mainTurnEndGeneration,
    btwSids: Array.from(new Set([...target.btwSids, ...source.btwSids])),
  } : source;
  return next;
}

export function discardBtwCompletionReceipts(
  current: CompletionReceipts,
): CompletionReceipts {
  let changed = false;
  const next: CompletionReceipts = {};
  for (const [parentSid, receipt] of Object.entries(current)) {
    if (receipt.btwSids.length > 0) changed = true;
    if (receipt.main) next[parentSid] = {
      main: true,
      mainCompletionId: receipt.mainCompletionId,
      mainTurnEndSeq: receipt.mainTurnEndSeq,
      mainTurnEndGeneration: receipt.mainTurnEndGeneration,
      btwSids: [],
    };
  }
  return changed ? next : current;
}

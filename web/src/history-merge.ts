import type {
  Block,
  ProcessBlock,
  TextBlock,
  ToolBlock,
  Turn,
  TurnDetailProjection,
} from "./domain/conversation";

function combineText(first: string, second: string): string {
  if (!first) return second;
  if (!second || first.includes(second)) return first;
  if (second.includes(first)) return second;
  const max = Math.min(first.length, second.length);
  for (let overlap = max; overlap > 0; overlap--) {
    if (first.slice(-overlap) === second.slice(0, overlap)) {
      return first + second.slice(overlap);
    }
  }
  return first + second;
}

function textChannel(block: TextBlock): string {
  return block.channel ?? "final";
}

function canCompatibilityMatchText(block: TextBlock): boolean {
  return block.text.length > 0 || !block.done;
}

function textAffinity(first: string, second: string): number {
  if (first === second) return Number.MAX_SAFE_INTEGER;
  if (first.includes(second) || second.includes(first)) {
    return Math.min(first.length, second.length);
  }
  const max = Math.min(first.length, second.length);
  for (let overlap = max; overlap > 0; overlap -= 1) {
    if (first.slice(-overlap) === second.slice(0, overlap)
        || second.slice(-overlap) === first.slice(0, overlap)) return overlap;
  }
  return 0;
}

function isFinalTextBlock(
  block: Block,
): block is TextBlock & { channel: "final" } {
  return block.kind === "text" && block.channel === "final";
}

function processBlockMatches(
  history: Block[],
  live: Block[],
): Map<number, number> {
  const matches = new Map<number, number>();
  const usedHistory = new Set<number>();
  const historyProcessIndexes = history.flatMap((block, index) =>
    block.kind === "process" ? [index] : []);
  const liveProcessIndexes = live.flatMap((block, index) =>
    block.kind === "process" ? [index] : []);

  // Native item ids are authoritative regardless of process kind.
  for (const liveIndex of liveProcessIndexes) {
    const liveBlock = live[liveIndex] as ProcessBlock;
    const historyIndex = historyProcessIndexes.find((index) =>
      !usedHistory.has(index)
      && (history[index] as ProcessBlock).item_id === liveBlock.item_id);
    if (historyIndex == null) continue;
    matches.set(liveIndex, historyIndex);
    usedHistory.add(historyIndex);
  }

  // Rollout and live app-server projections may assign different item ids to
  // the same contextCompaction occurrence. Pair only that authoritative process
  // kind, and pair by occurrence instead of collapsing an entire native task:
  // one long task can compact more than once.
  const historyCompactions = new Map<string, number[]>();
  const liveCompactions = new Map<string, number[]>();
  for (const historyIndex of historyProcessIndexes) {
    if (usedHistory.has(historyIndex)) continue;
    const block = history[historyIndex] as ProcessBlock;
    if (block.processKind !== "compaction" || !block.turn_id) continue;
    const indexes = historyCompactions.get(block.turn_id) ?? [];
    indexes.push(historyIndex);
    historyCompactions.set(block.turn_id, indexes);
  }
  for (const liveIndex of liveProcessIndexes) {
    if (matches.has(liveIndex)) continue;
    const block = live[liveIndex] as ProcessBlock;
    if (block.processKind !== "compaction" || !block.turn_id) continue;
    const indexes = liveCompactions.get(block.turn_id) ?? [];
    indexes.push(liveIndex);
    liveCompactions.set(block.turn_id, indexes);
  }
  for (const [turnId, liveIndexes] of liveCompactions) {
    const historyIndexes = historyCompactions.get(turnId);
    if (!historyIndexes?.length) continue;
    const pairCount = Math.min(historyIndexes.length, liveIndexes.length);
    // Live is normally the current tail of a longer transcript projection.
    // When History has more occurrences, align that live tail to History's
    // tail. If live has more, its unmatched suffix is genuinely newer.
    const historyStart = historyIndexes.length - pairCount;
    for (let offset = 0; offset < pairCount; offset += 1) {
      const liveIndex = liveIndexes[offset];
      const historyIndex = historyIndexes[historyStart + offset];
      matches.set(liveIndex, historyIndex);
      usedHistory.add(historyIndex);
    }
  }
  return matches;
}

function mergeBlocks(
  history: Block[],
  live: Block[],
  preserveLiveOpen: boolean,
  preferCompletedHistoryPayload = false,
  allowCompatibilityTextMatch = true,
): Block[] {
  const out = history.map((block) => ({ ...block }));
  const processMatches = processBlockMatches(history, live);
  // Native message identity is the only safe text overlap proof. Two separate
  // commentary items may intentionally contain identical text; content-based
  // matching silently deletes the later item and corrupts cache/detail/spill.
  const historyTextIndexes = out.flatMap((block, index) =>
    block.kind === "text" ? [index] : []);
  const matchedTextIndexes = new Set<number>();
  for (let liveIndex = 0; liveIndex < live.length; liveIndex += 1) {
    const block = live[liveIndex];
    if (block.kind === "process") {
      const historyIndex = processMatches.get(liveIndex);
      const existing = historyIndex == null
        ? undefined : out[historyIndex] as ProcessBlock;
      if (existing) {
        const historyLifecycle = {
          done: existing.done, phase: existing.phase, status: existing.status,
          title: existing.title, progress: existing.progress,
        };
        const keepHistoryPayload = preferCompletedHistoryPayload
          && existing.done && block.done;
        const plan = keepHistoryPayload
          ? existing.plan : block.plan ?? existing.plan;
        if (!keepHistoryPayload) Object.assign(existing, block);
        if (plan) existing.plan = plan.map((entry) => ({ ...entry }));
        // A completed transcript is authoritative over stale cache/live state.
        // Only the explicit in-flight-tail merge may reopen a synthetic history
        // boundary while the same live turn is genuinely still running.
        if (!preserveLiveOpen && historyLifecycle.done && !block.done) {
          Object.assign(existing, historyLifecycle);
        }
      } else {
        out.push({ ...block, plan: block.plan?.map((entry) => ({ ...entry })) });
      }
      continue;
    }
    if (block.kind === "tool") {
      const existing = out.find((candidate) => candidate.kind === "tool"
        && candidate.tool_use_id === block.tool_use_id) as ToolBlock | undefined;
      if (existing) {
        const historyDone = existing.done;
        const historyResult = existing.result;
        const historyTitle = existing.title;
        const historyProgress = existing.progress;
        const keepHistoryPayload = preferCompletedHistoryPayload
          && existing.done && block.done;
        if (!keepHistoryPayload) Object.assign(existing, block);
        if (!preserveLiveOpen && historyDone && !block.done) {
          existing.done = true;
          if (historyResult) existing.result = historyResult;
          existing.title = historyTitle;
          existing.progress = historyProgress;
        }
      }
      else out.push({ ...block });
      continue;
    }
    let existingIndex = historyTextIndexes.find((index) => {
      const candidate = out[index] as TextBlock;
      return !matchedTextIndexes.has(index) && candidate.message_id === block.message_id;
    });
    // Only complete history/live turn reconciliation owns this compatibility
    // fallback: those two projections already have an authoritative turn alias.
    // Detail/spill/cache windows do not carry overlap provenance, so equal text
    // there is never proof that two native items are the same occurrence.
    if (allowCompatibilityTextMatch && existingIndex == null
        && canCompatibilityMatchText(block)) {
      const candidates = historyTextIndexes.filter((index) => {
        const candidate = out[index] as TextBlock;
        return !matchedTextIndexes.has(index)
          && textChannel(candidate) === textChannel(block)
          && canCompatibilityMatchText(candidate);
      });
      let bestScore = 0;
      for (const index of candidates) {
        const score = textAffinity((out[index] as TextBlock).text, block.text);
        if (score > bestScore) {
          bestScore = score;
          existingIndex = index;
        }
      }
      if (existingIndex == null && block.text.length === 0 && !block.done
          && candidates.length === 1) existingIndex = candidates[0];
    }
    const existing = existingIndex == null
      ? undefined : out[existingIndex] as TextBlock;
    if (existing) {
      matchedTextIndexes.add(existingIndex!);
      existing.text = combineText(existing.text, block.text);
      existing.done = existing.done || block.done;
      if (block.channel !== "unknown") existing.channel = block.channel;
      if (block.liveOrder != null) {
        existing.liveOrder = existing.liveOrder == null
          ? block.liveOrder : Math.min(existing.liveOrder, block.liveOrder);
      }
      // History parsers can regenerate an assistant item id. While this turn
      // is still open, future deltas continue targeting the live app-server id.
      // Keeping the history id here makes the next delta create a second block,
      // which then survives every focus-triggered History reconciliation.
      if (preserveLiveOpen) existing.message_id = block.message_id;
    } else {
      out.push({ ...block });
    }
  }
  return out;
}

/** Combine a source-backed detail window with the bounded live tail.
 *
 * Detail pages can be fetched while a turn is still running. New stream frames
 * continue updating Turn.blocks, so rendering the projection alone would make
 * those later frames disappear until another history request. Merge by native
 * block identity and discard only the reducer's old presentation-only marker. */
export function mergeDetailWithLiveTail(
  detail: readonly Block[],
  live: readonly Block[],
  preferCompletedDetailPayload = false,
): Block[] {
  const withoutOldOmissionMarker = (block: Block) => !(
    block.kind === "process"
    && (block.item_id === "__cc_remote_earlier_process_omitted__"
      || block.item_id === "__cc_remote_detail_projection_capped__")
  );
  const filteredDetail = detail.filter(withoutOldOmissionMarker);
  const filteredLive = live.filter(withoutOldOmissionMarker);
  const merged = mergeBlocks(
    filteredDetail,
    filteredLive,
    true,
    preferCompletedDetailPayload,
    false,
  );

  const identity = (block: Block): string => block.kind === "text"
    ? `text:${block.message_id}`
    : block.kind === "tool"
      ? `tool:${block.tool_use_id}`
      : `process:${block.item_id}`;
  const liveIdentities = filteredLive.map(identity);
  const liveIds = new Set(liveIdentities);
  const detailIdentities = filteredDetail.map(identity);
  const liveOrders = filteredLive.map((block) => block.liveOrder);
  const hasAuthoritativeLiveOrder = liveOrders.every(
    (order): order is number => Number.isFinite(order),
  )
    && new Set(liveOrders).size === liveOrders.length
    && liveIds.size === liveIdentities.length
    && new Set(detailIdentities).size === detailIdentities.length;
  // Official Codex full/summary views can omit command/tool items while the
  // browser has already observed the complete interleaved live sequence. When
  // the source page is a subset of that live sequence, its array order is not a
  // chronology authority: using it first moves every missing tool behind the
  // commentary. Keep the source payload merge above, but paint in the complete
  // live order. A genuine source superset (normal paged detail + live tail)
  // retains its source order and simply appends the new tail as before.
  if (filteredLive.length > 0
      && hasAuthoritativeLiveOrder
      && detailIdentities.every((key) => liveIds.has(key))) {
    const byId = new Map(merged.map((block) => [identity(block), block]));
    const ordered = [...filteredLive]
      .sort((left, right) => left.liveOrder! - right.liveOrder!)
      .flatMap((block) => {
      const key = identity(block);
      const resolved = byId.get(key);
      if (!resolved) return [];
      byId.delete(key);
      return [resolved];
      });
    return [...ordered, ...byId.values()];
  }
  return merged;
}

type TurnIdentity = Pick<
  Turn,
  "id" | "clientMsgId" | "historyTurnId" | "forkPointId"
>;

function exactTurnAliases(turn: TurnIdentity): Set<string> {
  return new Set([
    turn.id,
    turn.clientMsgId,
    turn.historyTurnId,
  ].filter((value): value is string => !!value));
}

function sharesExactTurnAlias(first: TurnIdentity, second: TurnIdentity): boolean {
  const firstAliases = exactTurnAliases(first);
  return [...exactTurnAliases(second)].some((alias) => firstAliases.has(alias));
}

function nativeTaskIdentity(turn: Turn): string | undefined {
  return turn.liveTaskId ?? turn.forkPointId ?? turn.codexTurnId;
}

function compactionTurnAliases(turn: Turn): Set<string> {
  return new Set(turn.blocks.flatMap((block) =>
    block.kind === "process"
      && block.processKind === "compaction"
      && block.turn_id
      ? [block.turn_id]
      : []));
}

function sharesCompactionTurnAlias(history: Turn, live: Turn): boolean {
  // Official Codex history keeps the visible user-message id as the row id and
  // exposes the enclosing native task as forkPointId. A live compaction marker
  // carries that native task id before the user-message binding can arrive.
  // This native alias is safe only for compaction: ordinary process rows from
  // multiple steered segments intentionally share one enclosing task id.
  // The prompt guard is equally important: compaction and two steered prompts
  // may all share that task id, but they are still distinct visible rows.
  if (history.prompt !== live.prompt) return false;
  const historyAliases = new Set([
    ...exactTurnAliases(history),
    ...compactionTurnAliases(history),
    history.forkPointId,
  ].filter((value): value is string => !!value));
  const liveAliases = new Set([
    ...exactTurnAliases(live),
    ...compactionTurnAliases(live),
    live.forkPointId,
  ].filter((value): value is string => !!value));
  return [...compactionTurnAliases(live)].some(
    (alias) => historyAliases.has(alias),
  ) || [...compactionTurnAliases(history)].some(
    (alias) => liveAliases.has(alias),
  );
}

function sameTurnIdentity(history: Turn, live: Turn): boolean {
  if (sharesExactTurnAlias(history, live)) return true;
  // Codex may emit contextCompaction before the clean user/message binding.
  // The process event then carries the only native identity available to the
  // optimistic row. Treat that one authoritative marker as a turn alias, but
  // never generalize ordinary process turn ids: multiple steered narrative
  // segments legitimately share one native task id.
  if (sharesCompactionTurnAlias(history, live)) return true;
  // Automatic/goal continuations have no user message. Live uses the app-server
  // turn id as its empty anchor, while rollout history may use the first
  // assistant item id; TurnEnd still supplies the same authoritative branch id.
  if (history.forkPointId && live.forkPointId
      && history.forkPointId === live.forkPointId) return true;
  return history.forkPointId === live.id || live.forkPointId === history.id;
}

function sameTurn(history: Turn, live: Turn): boolean {
  return sameTurnIdentity(history, live);
}

function sameLegacyCachedTurn(summary: Turn, cached: Turn): boolean {
  if (sameTurnIdentity(summary, cached)) return true;
  if (!summary.prompt || summary.prompt !== cached.prompt) return false;
  // CACHE_VER migrations may predate clientMsgId/historyTurnId. Keep this
  // compatibility only while restoring a one-to-one completed cache row;
  // ordinary history/live reconciliation must never infer identity from text.
  if (summary.ts == null || cached.ts == null) return false;
  return Math.abs(summary.ts - cached.ts) <= 3000;
}

export function historyContainsTurn(history: Turn[], live: Turn): boolean {
  return history.some((turn) => sameTurn(turn, live));
}

function cloneDetailBlock(block: Block): Block {
  if (block.kind === "process") {
    return {
      ...block,
      input: block.input ? { ...block.input } : block.input,
      plan: block.plan?.map((entry) => ({ ...entry })),
    };
  }
  if (block.kind === "tool") {
    return {
      ...block,
      input: { ...block.input },
      result: block.result ? { ...block.result } : block.result,
    };
  }
  return { ...block };
}

/** Paint only heavyweight blocks from a same-revision/generation browser
 * cache over an authoritative summary row.
 *
 * The summary remains authoritative for prompt/final/lifecycle. Cached process
 * is deliberately marked provisional and owns no source segments; the next
 * accepted TurnDetail page replaces it rather than merging stale events into
 * the transcript projection.
 */
function installCachedDetailRestore(
  summary: Turn,
  cached: Turn,
): Turn {
  if (!summary.done || !cached.done || summary.detailLoaded
      || summary.detailProjection
      || (summary.detailEventCount ?? 0) <= 0) return summary;
  const source = cached.detailProjection?.blocks ?? cached.blocks;
  const blocks = source.filter((block) => !isFinalTextBlock(block))
    .map(cloneDetailBlock);
  if (blocks.length === 0) return summary;
  return {
    ...summary,
    detailLoaded: false,
    detailLoading: false,
    detailError: undefined,
    detailRetryBefore: undefined,
    detailRetryDirection: undefined,
    detailProjection: {
      // Empty segments distinguish instant-paint cache from authoritative
      // cursor pages. installTurnDetailProjectionPage then replaces this
      // visible block list with the first accepted server page.
      segments: [],
      blocks,
      capped: cached.detailProjection?.capped ?? false,
      hasMore: false,
      oldestCursor: null,
      hasNewer: false,
      newerCursor: null,
    },
    detailHasMore: false,
    detailOldestCursor: null,
    detailHasNewer: false,
    detailNewerCursor: null,
    detailAutoLoad: false,
    // Cache paint is already useful and remains explicitly provisional. Do not
    // turn entering a completed session into a background detail fetch or an
    // automatic disclosure; the user's first click requests the canonical page.
    detailRestorePending: false,
    detailRestoreIncomplete: false,
    detailRestoreOpen: false,
  };
}

/** Keep heavyweight process which this browser painted from the live stream
 * visible across the first authoritative summary after completion/interrupt.
 *
 * This path is intentionally stricter than cache migration: only exact native
 * identity aliases match, and the summary remains authoritative for prompt,
 * final text, lifecycle, and ordering. The caller scopes observedTurns to one
 * accepted history revision/generation, so rollback cannot resurrect a row.
 */
export function restoreObservedLiveTurnDetails(
  summaries: Turn[],
  observedTurns: readonly Turn[],
): Turn[] {
  const observedMatches = new Array<number>(summaries.length).fill(-1);
  const usedObserved = new Set<number>();
  const reserveMatches = (
    predicate: (summary: Turn, observed: Turn) => boolean,
  ) => {
    // A steered Codex task owns several visible user segments which all share
    // one forkPointId. Reserve exact per-segment identities across the complete
    // suffix before that native-task compatibility alias can claim any row.
    // Match newest-to-newest so a legacy projection without exact user ids
    // still preserves the narrative segment order.
    for (let summaryIndex = summaries.length - 1; summaryIndex >= 0;
      summaryIndex -= 1) {
      if (observedMatches[summaryIndex] >= 0) continue;
      for (let observedIndex = observedTurns.length - 1;
        observedIndex >= 0; observedIndex -= 1) {
        if (usedObserved.has(observedIndex)
            || !predicate(summaries[summaryIndex], observedTurns[observedIndex])) {
          continue;
        }
        observedMatches[summaryIndex] = observedIndex;
        usedObserved.add(observedIndex);
        break;
      }
    }
  };
  reserveMatches(sharesExactTurnAlias);
  reserveMatches(sameTurnIdentity);

  const restored = [...summaries];
  for (let summaryIndex = restored.length - 1; summaryIndex >= 0;
    summaryIndex -= 1) {
    const summary = restored[summaryIndex];
    const observedIndex = observedMatches[summaryIndex];
    if (observedIndex < 0) continue;
    const observed = observedTurns[observedIndex];
    if (!summary.done || !observed.done || summary.detailLoaded
        || summary.detailProjection) continue;
    const sourceWithArchive = mergeDetailWithLiveTail(
      observed.detailProjection?.blocks ?? [],
      observed.liveSpillBlocks ?? [],
      true,
    );
    const source = mergeDetailWithLiveTail(
      sourceWithArchive,
      observed.blocks,
      true,
    );
    const blocks = source.filter((block) => !isFinalTextBlock(block)
        && (block.kind !== "text" || block.text.length > 0))
      .map(cloneDetailBlock);
    if (blocks.length === 0) continue;
    restored[summaryIndex] = {
      ...summary,
      detailLoaded: false,
      detailLoading: false,
      detailError: undefined,
      detailProjection: {
        segments: [],
        blocks,
        capped: observed.detailProjection?.capped ?? false,
        hasMore: false,
        oldestCursor: null,
        hasNewer: false,
        newerCursor: null,
      },
      detailHasMore: false,
      detailOldestCursor: null,
      detailHasNewer: false,
      detailNewerCursor: null,
      detailAutoLoad: false,
      detailRestorePending: false,
      detailRestoreIncomplete: false,
      detailRestoreOpen: false,
    };
  }
  return restored;
}

/** Reconcile cached process with canonical summary identities without
 * resurrecting a completed row which authoritative History removed. */
export function restoreCachedTurnDetails(
  summaries: Turn[],
  cachedTurns: readonly Turn[],
): Turn[] {
  const matches = new Array<number>(summaries.length).fill(-1);
  const usedCached = new Set<number>();

  const reserveMatches = (
    predicate: (summary: Turn, cached: Turn) => boolean,
  ) => {
    for (let summaryIndex = summaries.length - 1; summaryIndex >= 0;
      summaryIndex -= 1) {
      if (matches[summaryIndex] >= 0) continue;
      for (let cachedIndex = cachedTurns.length - 1;
        cachedIndex >= 0; cachedIndex -= 1) {
        if (usedCached.has(cachedIndex)
            || !predicate(summaries[summaryIndex], cachedTurns[cachedIndex])) {
          continue;
        }
        matches[summaryIndex] = cachedIndex;
        usedCached.add(cachedIndex);
        break;
      }
    }
  };
  // A steered native task gives every visible segment the same forkPointId.
  // Reserve exact user/client identities for the full suffix before using that
  // broad compatibility alias, otherwise one segment can consume another's
  // cached process and make the original tools appear to vanish.
  reserveMatches(sharesExactTurnAlias);
  reserveMatches(sameTurnIdentity);

  // Older optimistic caches may legitimately have a different id. Keep that
  // compatibility one-to-one and choose the closest timestamp so repeated
  // prompts cannot all reuse the first eligible cache row.
  for (let summaryIndex = 0; summaryIndex < summaries.length; summaryIndex += 1) {
    if (matches[summaryIndex] >= 0) continue;
    const summary = summaries[summaryIndex];
    let bestIndex = -1;
    let bestDistance = Number.POSITIVE_INFINITY;
    for (let cachedIndex = 0; cachedIndex < cachedTurns.length;
      cachedIndex += 1) {
      if (usedCached.has(cachedIndex)) continue;
      const candidate = cachedTurns[cachedIndex];
      if (!sameLegacyCachedTurn(summary, candidate)) continue;
      const distance = Math.abs((summary.ts ?? 0) - (candidate.ts ?? 0));
      if (distance >= bestDistance) continue;
      bestIndex = cachedIndex;
      bestDistance = distance;
    }
    if (bestIndex < 0) continue;
    matches[summaryIndex] = bestIndex;
    usedCached.add(bestIndex);
  }

  const restored = [...summaries];
  for (let summaryIndex = restored.length - 1; summaryIndex >= 0;
    summaryIndex -= 1) {
    const cachedIndex = matches[summaryIndex];
    if (cachedIndex < 0) continue;
    const summary = restored[summaryIndex];
    const installed = installCachedDetailRestore(
      summary, cachedTurns[cachedIndex]);
    restored[summaryIndex] = installed;
  }
  return restored;
}

function mergeTurn(history: Turn, live: Turn, preserveLiveOpen = false): Turn {
  const historyImageRefs = history.imageRefs?.length
    ? history.imageRefs : undefined;
  // A matched transcript row keeps its native lookup identity even when the
  // visible row adopts an optimistic browser id. Detail and image reads both
  // target that history id; tying it only to imageRefs makes an attachment-free
  // alias lose GetTurnDetail authority after the merge.
  const historyTurnId = history.historyTurnId
    ?? (history.id !== live.id ? history.id : live.historyTurnId);
  const detailProjection = live.detailProjection ?? history.detailProjection;
  return {
    ...history,
    id: live.id,
    clientMsgId: history.clientMsgId ?? live.clientMsgId,
    historyTurnId,
    forkPointId: history.forkPointId ?? live.forkPointId,
    checkpointId: history.checkpointId ?? live.checkpointId,
    prompt: history.prompt || live.prompt,
    blocks: mergeBlocks(history.blocks, live.blocks, preserveLiveOpen),
    // A transcript has no ResultMessage, so its EOF is represented by a
    // synthetic TurnEnd.  While this same live tail is still running, that
    // marker is only a snapshot boundary and must not close the turn early.
    done: preserveLiveOpen ? live.done : history.done || live.done,
    interrupted: history.interrupted || live.interrupted,
    error: live.error ?? history.error,
    progress: preserveLiveOpen ? live.progress : undefined,
    // A summary page replaces optimistic inline image bodies with canonical,
    // payload-free references. Retaining both makes ChatView lay out the same
    // attachment twice and leaves a large placeholder below the visible image.
    images: historyImageRefs ? undefined : live.images ?? history.images,
    imageRefs: historyImageRefs ?? live.imageRefs ?? history.imageRefs,
    files: live.files ?? history.files,
    ts: Math.min(history.ts ?? Number.MAX_SAFE_INTEGER,
      live.ts ?? Number.MAX_SAFE_INTEGER) === Number.MAX_SAFE_INTEGER
      ? undefined
      : Math.min(history.ts ?? Number.MAX_SAFE_INTEGER,
          live.ts ?? Number.MAX_SAFE_INTEGER),
    doneTs: preserveLiveOpen
      ? live.doneTs
      : Math.max(history.doneTs ?? 0, live.doneTs ?? 0) || undefined,
    durationMs: history.durationMs === 0 && (live.durationMs ?? 0) > 0
      ? live.durationMs
      : history.durationMs ?? live.durationMs,
    // Detail is a monotonic, revision-bound local projection. A later summary
    // may legitimately contain no heavyweight blocks; it must not erase pages
    // which the user already expanded in this same revision.
    detailProjection,
    detailLoaded: !!detailProjection
      || !!live.detailLoaded || !!history.detailLoaded,
    detailLoading: live.detailLoading ?? history.detailLoading,
    detailError: live.detailError ?? history.detailError,
    detailHasMore: detailProjection
      ? detailProjection.hasMore
      : live.detailHasMore ?? history.detailHasMore,
    detailOldestCursor: detailProjection
      ? detailProjection.oldestCursor
      : live.detailOldestCursor ?? history.detailOldestCursor,
    detailHasNewer: detailProjection
      ? detailProjection.hasNewer
      : live.detailHasNewer ?? history.detailHasNewer,
    detailNewerCursor: detailProjection
      ? detailProjection.newerCursor
      : live.detailNewerCursor ?? history.detailNewerCursor,
    detailAutoLoad: live.detailAutoLoad ?? history.detailAutoLoad,
    liveBlocksSpilled:
      live.liveBlocksSpilled ?? history.liveBlocksSpilled,
    liveSpilledBlockCount: Math.max(
      live.liveSpilledBlockCount ?? 0,
      history.liveSpilledBlockCount ?? 0,
    ) || undefined,
    liveSpillBlocks: live.liveSpillBlocks ?? history.liveSpillBlocks,
    liveSpillRefreshCount: Math.max(
      live.liveSpillRefreshCount ?? 0,
      history.liveSpillRefreshCount ?? 0,
    ) || undefined,
  };
}

function restoreAuthoritativeLifecycle(merged: Turn, history: Turn): Turn {
  return {
    ...merged,
    done: history.done,
    interrupted: history.interrupted,
    error: history.error,
    progress: history.progress,
    doneTs: history.doneTs,
    durationMs: history.durationMs,
  };
}

/** Merge previously-loaded heavyweight detail into a newer summary without
 * allowing stale detail lifecycle fields to reopen a steered/completed turn. */
export function mergeAuthoritativeTurnDetail(
  summary: Turn,
  detail: Turn,
): Turn {
  const merged = mergeTurn(summary, detail, false);
  return {
    ...merged,
    id: summary.id,
    done: summary.done,
    doneTs: summary.doneTs,
    durationMs: summary.durationMs,
    interrupted: summary.interrupted,
    error: summary.error,
    progress: summary.progress,
    detailEventCount: summary.detailEventCount,
    detailLoaded: detail.detailLoaded ?? true,
    detailLoading: false,
    detailError: undefined,
    detailRetryBefore: undefined,
    detailRetryDirection: undefined,
    detailProjection: detail.detailProjection ?? summary.detailProjection,
    detailHasMore: detail.detailProjection
      ? detail.detailProjection.hasMore
      : detail.detailHasMore ?? summary.detailHasMore,
    detailOldestCursor: detail.detailProjection
      ? detail.detailProjection.oldestCursor
      : detail.detailOldestCursor ?? summary.detailOldestCursor,
    detailHasNewer: detail.detailProjection
      ? detail.detailProjection.hasNewer
      : detail.detailHasNewer ?? summary.detailHasNewer,
    detailNewerCursor: detail.detailProjection
      ? detail.detailProjection.newerCursor
      : detail.detailNewerCursor ?? summary.detailNewerCursor,
    detailAutoLoad: detail.detailAutoLoad ?? summary.detailAutoLoad,
    detailRestorePending: false,
    detailRestoreOpen:
      detail.detailRestoreOpen ?? summary.detailRestoreOpen,
    detailRestoreIncomplete:
      detail.detailRestoreIncomplete ?? summary.detailRestoreIncomplete,
  };
}

/** Install one bounded intra-turn detail page.
 *
 * Pages are source-disjoint and may be visited in either direction, so the
 * visible process window is replaced instead of accumulated. Keep the summary's
 * final answer outside that window when an older page does not contain it. */
export function installAuthoritativeTurnDetailPage(
  summary: Turn,
  detail: Turn,
  page: {
    hasMore: boolean;
    oldestCursor?: string | null;
    hasNewer: boolean;
    newerCursor?: string | null;
  },
  projection?: TurnDetailProjection,
): Turn {
  const summaryFinals = summary.blocks.filter(isFinalTextBlock);
  const pageFinals = detail.blocks.filter(isFinalTextBlock);

  const alignFinalSegment = (
    summarySegment: TextBlock[],
    pageSegment: TextBlock[],
  ): TextBlock[] => {
    if (pageSegment.length === 0) return summarySegment;
    // Without an exact id inside this source-ordered segment, older caches may
    // have regenerated assistant ids. Align from the end: a one-to-one alias
    // replaces instead of duplicating, while an unvisited summary prefix stays
    // visible and page-only leading finals remain in source order.
    if (pageSegment.length >= summarySegment.length) return pageSegment;
    return [
      ...summarySegment.slice(0, summarySegment.length - pageSegment.length),
      ...pageSegment,
    ];
  };

  const summaryIndexById = new Map(
    summaryFinals.map((block, index) => [block.message_id, index] as const),
  );
  const anchors: Array<{ summaryIndex: number; pageIndex: number }> = [];
  let nextSummaryIndex = 0;
  pageFinals.forEach((block, pageIndex) => {
    const summaryIndex = summaryIndexById.get(block.message_id);
    if (summaryIndex == null || summaryIndex < nextSummaryIndex) return;
    anchors.push({ summaryIndex, pageIndex });
    nextSummaryIndex = summaryIndex + 1;
  });

  const canonicalFinals: TextBlock[] = [];
  let summaryStart = 0;
  let pageStart = 0;
  for (const anchor of anchors) {
    canonicalFinals.push(...alignFinalSegment(
      summaryFinals.slice(summaryStart, anchor.summaryIndex),
      pageFinals.slice(pageStart, anchor.pageIndex),
    ));
    // The detail page is authoritative for an exact native message id.
    canonicalFinals.push(pageFinals[anchor.pageIndex]);
    summaryStart = anchor.summaryIndex + 1;
    pageStart = anchor.pageIndex + 1;
  }
  canonicalFinals.push(...alignFinalSegment(
    summaryFinals.slice(summaryStart),
    pageFinals.slice(pageStart),
  ));
  const detailWithoutFinals = detail.blocks.filter(
    (block) => !isFinalTextBlock(block));
  const canonicalImageRefs = detail.imageRefs ?? summary.imageRefs;
  const detailProjection = projection ?? summary.detailProjection;
  const hasMore = detailProjection
    ? detailProjection.hasMore : page.hasMore;
  const oldestCursor = detailProjection
    ? detailProjection.oldestCursor : page.oldestCursor ?? null;
  const hasNewer = detailProjection
    ? detailProjection.hasNewer : page.hasNewer;
  const newerCursor = detailProjection
    ? detailProjection.newerCursor : page.newerCursor ?? null;
  const restoreIncomplete =
    summary.detailRestoreIncomplete === true && hasMore;
  return {
    ...summary,
    prompt: detail.prompt || summary.prompt,
    images: canonicalImageRefs?.length
      ? undefined : detail.images ?? summary.images,
    imageRefs: canonicalImageRefs,
    files: detail.files ?? summary.files,
    // Heavy process/tool/commentary pages live in detailProjection so the
    // ordinary live-turn 256 item / 16 MiB cap cannot evict them. Legacy
    // callers without a projection retain the pre-v21 behavior.
    blocks: detailProjection
      ? canonicalFinals : [...detailWithoutFinals, ...canonicalFinals],
    done: summary.done,
    doneTs: summary.doneTs,
    durationMs: summary.durationMs,
    interrupted: summary.interrupted,
    error: summary.error,
    progress: summary.progress,
    detailEventCount: summary.detailEventCount,
    detailLoaded: !restoreIncomplete,
    detailLoading: false,
    detailError: undefined,
    detailRetryBefore: undefined,
    detailRetryDirection: undefined,
    detailProjection,
    detailHasMore: hasMore,
    detailOldestCursor: oldestCursor,
    detailHasNewer: hasNewer,
    detailNewerCursor: newerCursor,
    detailAutoLoad:
      !!summary.detailAutoLoad && hasMore && !detailProjection?.capped,
    detailRestorePending: false,
    detailRestoreIncomplete: restoreIncomplete,
    liveSpillBlocks: summary.done && !hasMore && !hasNewer
      ? undefined : summary.liveSpillBlocks,
  };
}

function chronologicalTurnTime(turn: Turn): number | undefined {
  if (turn.prompt || turn.doneTs == null) return turn.ts;
  const terminalStart = Math.max(0, turn.doneTs - (turn.durationMs ?? 0));
  // Older caches and mixed-version wrappers may contain replay-generated
  // assistant-only starts stamped after their authoritative terminal. Use the
  // terminal-derived time for ordering without mutating the rendered payload.
  if (turn.ts == null || turn.ts > turn.doneTs) return terminalStart;
  return turn.ts;
}

/** Merge transcript history with cache/live state without deleting a just-finished
 * turn that hasn't flushed yet or duplicating the same prompt under engine ids. */
export function mergeInitialHistory(
  history: Turn[],
  live: Turn[],
  options: {
    preserveLiveTailOpen?: boolean;
    /** Exact visible row named as the newest authoritative History row.
     * This binds a uniquely proven live block overlap while a steered row's
     * client/native user alias is still materializing. It also lets a terminal
     * page collapse the stale empty active shell left by an earlier snapshot. */
    newestHistoryId?: string | null;
    /** A newest authoritative page may absorb a replay-created assistant row
     * only when native block identities prove that row belongs to one
     * canonical turn. Disabled for cache, pagination, re-key and detail merges. */
    reconcileReplayOrphans?: boolean;
  } = {},
): Turn[] {
  const merged = history.map((turn) => ({ ...turn, blocks: turn.blocks.map((b) => ({ ...b })) }));
  const matches = new Array<number>(live.length).fill(-1);
  const used = new Set<number>();
  const unmatched: Turn[] = [];

  const reserveMatches = (
    predicate: (historyTurn: Turn, liveTurn: Turn) => boolean,
  ) => {
    // Live is generally the newest suffix of History. Match from the tail so
    // repeated steer prompts within one native task bind their newest segment.
    for (let liveIndex = live.length - 1; liveIndex >= 0; liveIndex -= 1) {
      if (matches[liveIndex] >= 0) continue;
      for (let historyIndex = merged.length - 1; historyIndex >= 0;
        historyIndex -= 1) {
        if (used.has(historyIndex)
            || !predicate(merged[historyIndex], live[liveIndex])) continue;
        matches[liveIndex] = historyIndex;
        used.add(historyIndex);
        break;
      }
    }
  };

  // Reserve exact identities for the whole live suffix before the guarded
  // compaction fallback can consume a row belonging to another live segment.
  reserveMatches((historyTurn, liveTurn) => historyTurn.id === liveTurn.id);
  reserveMatches(sharesExactTurnAlias);
  reserveMatches(sameTurn);

  const replayOrphanMatches = new Set<number>();
  const activeReplayMatches = new Set<number>();
  const authoritativeHeadDuplicateMatches = new Set<number>();
  if (options.reconcileReplayOrphans) {
    const owners = new Map<string, Set<number>>();
    const addOwner = (key: string, historyIndex: number) => {
      const indexes = owners.get(key) ?? new Set<number>();
      indexes.add(historyIndex);
      owners.set(key, indexes);
    };
    const blockKeys = (block: Block): string[] => {
      if (block.kind === "text") return [`message:${block.message_id}`];
      if (block.kind === "tool") {
        return [
          `message:${block.message_id}`,
          `tool:${block.tool_use_id}`,
        ];
      }
      return [`process:${block.item_id}`];
    };
    merged.forEach((turn, historyIndex) => {
      for (const block of turn.blocks) {
        for (const key of blockKeys(block)) addOwner(key, historyIndex);
      }
    });
    const authoritativeHistoryIndexes = options.newestHistoryId
      ? merged.flatMap((turn, index) =>
          exactTurnAliases(turn).has(options.newestHistoryId!)
            ? [index] : [])
      : [];
    const authoritativeHistoryIndex = authoritativeHistoryIndexes.length === 1
      ? authoritativeHistoryIndexes[0] : -1;
    const primaryBlockKey = (block: Block): string => block.kind === "text"
      ? `message:${block.message_id}`
      : block.kind === "tool"
        ? `tool:${block.tool_use_id}`
        : `process:${block.item_id}`;

    const claims = new Map<number, number[]>();
    for (let liveIndex = 0; liveIndex < live.length; liveIndex += 1) {
      if (matches[liveIndex] >= 0) continue;
      const candidate = live[liveIndex];
      const activeCandidate = authoritativeHistoryIndex >= 0
        && liveIndex === live.length - 1
        && !candidate.done;
      if (!activeCandidate && (candidate.prompt
          || candidate.images?.length || candidate.imageRefs?.length
          || candidate.files?.length
          || candidate.clientMsgId || candidate.historyTurnId
          || candidate.forkPointId || candidate.checkpointId
          || candidate.codexTurnId || candidate.liveTaskId
          || candidate.detailProjection
          || candidate.liveSpillBlocks?.length
          || candidate.blocks.length === 0)) continue;
      let historyIndex: number;
      if (activeCandidate) {
        historyIndex = authoritativeHistoryIndex;
        const historyTurn = merged[historyIndex];
        const historyNativeId = nativeTaskIdentity(historyTurn);
        const candidateNativeId = nativeTaskIdentity(candidate);
        if (candidateNativeId && historyNativeId
            && candidateNativeId !== historyNativeId) continue;
        const keys = [...new Set(candidate.blocks.map(primaryBlockKey))];
        const shared = keys.some((key) => owners.get(key)?.has(historyIndex));
        const conflicts = keys.some((key) => {
          const keyOwners = owners.get(key);
          return keyOwners != null && (
            keyOwners.size !== 1 || !keyOwners.has(historyIndex)
          );
        });
        if (!shared || conflicts) continue;
      } else {
        const messageOwners = owners.get(`message:${candidate.id}`);
        if (!messageOwners || messageOwners.size !== 1) continue;
        historyIndex = [...messageOwners][0];
      }
      if (used.has(historyIndex)) continue;
      if (!activeCandidate) {
        const keys = candidate.blocks.flatMap(blockKeys);
        if (keys.length === 0 || new Set(keys).size !== keys.length
            || !keys.every((key) => {
          const keyOwners = owners.get(key);
          return keyOwners?.size === 1 && keyOwners.has(historyIndex);
        })) continue;
      }
      const candidates = claims.get(historyIndex) ?? [];
      candidates.push(liveIndex);
      claims.set(historyIndex, candidates);
      if (activeCandidate) activeReplayMatches.add(liveIndex);
    }
    for (const [historyIndex, liveIndexes] of claims) {
      // Two provisional rows claiming one native assistant item are ambiguous;
      // do not absorb either into the canonical history row.
      if (liveIndexes.length !== 1 || used.has(historyIndex)) continue;
      const liveIndex = liveIndexes[0];
      matches[liveIndex] = historyIndex;
      used.add(historyIndex);
      replayOrphanMatches.add(liveIndex);
    }

    // An item-free active snapshot cannot safely bind a local steer: several
    // visible steer segments may share one native task id. It therefore leaves
    // an exact authoritative shell plus the accepted local row until native
    // blocks arrive. At the terminal snapshot those exact block ids finally
    // prove that the two local rows describe one newest history row. Allow only
    // that unique tail candidate to join the already-reserved head; a shared
    // task id, prompt, timestamp or display text never qualifies on its own.
    if (authoritativeHistoryIndex >= 0
        && matches.some((index) => index === authoritativeHistoryIndex)) {
      const historyTurn = merged[authoritativeHistoryIndex];
      const duplicateCandidates: number[] = [];
      for (let liveIndex = 0; liveIndex < live.length; liveIndex += 1) {
        if (matches[liveIndex] >= 0 || liveIndex !== live.length - 1) continue;
        const candidate = live[liveIndex];
        if (candidate.blocks.length === 0) continue;
        const historyNativeId = nativeTaskIdentity(historyTurn);
        const candidateNativeId = nativeTaskIdentity(candidate);
        if (historyNativeId && candidateNativeId
            && historyNativeId !== candidateNativeId) continue;
        const keys = [...new Set(candidate.blocks.map(primaryBlockKey))];
        const shared = keys.some((key) =>
          owners.get(key)?.has(authoritativeHistoryIndex));
        const conflicts = keys.some((key) => {
          const keyOwners = owners.get(key);
          return keyOwners != null && (
            keyOwners.size !== 1
            || !keyOwners.has(authoritativeHistoryIndex)
          );
        });
        if (shared && !conflicts) duplicateCandidates.push(liveIndex);
      }
      if (duplicateCandidates.length === 1) {
        const liveIndex = duplicateCandidates[0];
        matches[liveIndex] = authoritativeHistoryIndex;
        authoritativeHeadDuplicateMatches.add(liveIndex);
      }
    }
  }

  // Apply the precomputed mapping in original live order. Matching direction
  // must not reorder unmatched optimistic rows.
  for (let liveIndex = 0; liveIndex < live.length; liveIndex += 1) {
    const liveTurn = live[liveIndex];
    const index = matches[liveIndex];
    if (index >= 0) {
      if (authoritativeHeadDuplicateMatches.has(liveIndex)) continue;
      if (activeReplayMatches.has(liveIndex)) {
        const preserveLiveOpen = !!options.preserveLiveTailOpen
          && liveTurn === live[live.length - 1]
          && !liveTurn.done;
        const bound = mergeTurn(
          merged[index], liveTurn, preserveLiveOpen,
        );
        bound.blocks = mergeBlocks(
          merged[index].blocks,
          liveTurn.blocks,
          preserveLiveOpen,
          false,
          false,
        );
        merged[index] = preserveLiveOpen
          ? bound
          : restoreAuthoritativeLifecycle(bound, merged[index]);
        continue;
      }
      if (replayOrphanMatches.has(liveIndex)) {
        // History owns row identity, lifecycle and detail authority. Preserve
        // only newer bytes for the exact native blocks proved above; a stale
        // orphan detail failure must never replace canonical GetTurnDetail.
        merged[index] = {
          ...merged[index],
          blocks: mergeBlocks(
            merged[index].blocks,
            liveTurn.blocks,
            false,
            false,
            false,
          ),
        };
        continue;
      }
      const isOpenLiveTail = !!options.preserveLiveTailOpen
        && liveTurn === live[live.length - 1]
        // A newer authoritative history turn proves this local placeholder is
        // no longer the active tail (for example, same-task steering). Only the
        // matching newest history row may inherit an unfinished live state.
        && index === merged.length - 1
        && !liveTurn.done;
      merged[index] = mergeTurn(merged[index], liveTurn, isOpenLiveTail);
    } else {
      unmatched.push({ ...liveTurn, blocks: liveTurn.blocks.map((b) => ({ ...b })) });
    }
  }

  // Apply a proven duplicate only after the ordinary exact match has refreshed
  // the authoritative shell. This makes the accepted live id the stable React
  // row id while preserving the native history id for detail/image requests.
  for (const liveIndex of authoritativeHeadDuplicateMatches) {
    const index = matches[liveIndex];
    if (index < 0) continue;
    const liveTurn = live[liveIndex];
    const preserveLiveOpen = !!options.preserveLiveTailOpen && !liveTurn.done;
    const bound = mergeTurn(merged[index], liveTurn, preserveLiveOpen);
    bound.blocks = mergeBlocks(
      merged[index].blocks,
      liveTurn.blocks,
      preserveLiveOpen,
      false,
      false,
    );
    merged[index] = preserveLiveOpen
      ? bound
      : restoreAuthoritativeLifecycle(bound, merged[index]);
  }

  const rows = [...merged, ...unmatched].map((turn, order) => ({ turn, order }));
  rows.sort((a, b) => {
    const aTime = chronologicalTurnTime(a.turn);
    const bTime = chronologicalTurnTime(b.turn);
    if (aTime != null && bTime != null && aTime !== bTime) {
      return aTime - bTime;
    }
    return a.order - b.order;
  });
  const seenAliases = new Set<string>();
  return rows.map((row) => row.turn).filter((turn) => {
    const aliases = exactTurnAliases(turn);
    if ([...aliases].some((alias) => seenAliases.has(alias))) return false;
    aliases.forEach((alias) => seenAliases.add(alias));
    return true;
  });
}

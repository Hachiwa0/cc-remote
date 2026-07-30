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

function isFinalTextBlock(block: Block): block is TextBlock {
  return block.kind === "text" && block.channel === "final";
}

function canFuzzyMatchText(block: TextBlock): boolean {
  // A completed assistant envelope without a delta is tool scaffolding, not a
  // semantic text position. Open empty blocks remain eligible so a
  // focus-triggered History merge can retain the id targeted by future deltas.
  return block.text.length > 0 || !block.done;
}

function textAffinity(first: string, second: string): number {
  if (first === second) return Number.MAX_SAFE_INTEGER;
  if (first.includes(second) || second.includes(first)) {
    return Math.min(first.length, second.length);
  }
  const max = Math.min(first.length, second.length);
  for (let overlap = max; overlap > 0; overlap--) {
    if (first.slice(-overlap) === second.slice(0, overlap)
        || second.slice(-overlap) === first.slice(0, overlap)) return overlap;
  }
  return 0;
}

function mergeBlocks(history: Block[], live: Block[], preserveLiveOpen: boolean): Block[] {
  const out = history.map((block) => ({ ...block }));
  // Engine history often regenerates assistant ids. Pair each same-channel text
  // block at most once: prefer matching content, then preserve channel order.
  // Reverse-finding the last block collapses A -> tool -> B into A -> tool -> BA.
  const historyTextIndexes = out.flatMap((block, index) =>
    block.kind === "text" ? [index] : []);
  const matchedTextIndexes = new Set<number>();
  for (const block of live) {
    if (block.kind === "process") {
      const existing = out.find((candidate) => candidate.kind === "process"
        && candidate.item_id === block.item_id) as ProcessBlock | undefined;
      if (existing) {
        const historyLifecycle = {
          done: existing.done, phase: existing.phase, status: existing.status,
          title: existing.title, progress: existing.progress,
        };
        const plan = block.plan ?? existing.plan;
        Object.assign(existing, block);
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
        Object.assign(existing, block);
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
    if (existingIndex == null && canFuzzyMatchText(block)) {
      const candidates = historyTextIndexes.filter((index) => {
        const candidate = out[index] as TextBlock;
        return !matchedTextIndexes.has(index)
          && textChannel(candidate) === textChannel(block)
          && canFuzzyMatchText(candidate);
      });
      let bestScore = 0;
      for (const index of candidates) {
        const score = textAffinity((out[index] as TextBlock).text, block.text);
        if (score > bestScore) {
          bestScore = score;
          existingIndex = index;
        }
      }
      // An open, empty live envelope has no content to score yet. It may inherit
      // the sole canonical position so future deltas keep their live message id.
      // Non-empty zero-affinity text is a distinct narrative block and must not
      // be concatenated merely because it shares a channel.
      if (existingIndex == null && block.text.length === 0 && !block.done
          && candidates.length === 1) {
        existingIndex = candidates[0];
      }
    }
    const existing = existingIndex == null
      ? undefined : out[existingIndex] as TextBlock;
    if (existing) {
      matchedTextIndexes.add(existingIndex!);
      existing.text = combineText(existing.text, block.text);
      existing.done = existing.done || block.done;
      if (block.channel !== "unknown") existing.channel = block.channel;
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

type TurnIdentity = Pick<Turn, "id" | "clientMsgId" | "historyTurnId">;

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

function sameTurnIdentity(history: Turn, live: Turn): boolean {
  if (sharesExactTurnAlias(history, live)) return true;
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
  reveal: boolean,
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
    detailRestorePending: reveal,
    detailRestoreIncomplete: false,
    // Automatically recreating thousands of DOM rows would defeat the instant
    // cache paint. Small latest-turn process is reopened to preserve the live
    // 1..N reading experience; large projections remain one tap away.
    detailRestoreOpen: reveal && blocks.length <= 256,
  };
}

/** Reconcile cached process with canonical summary identities without
 * resurrecting a completed row which authoritative History removed. */
export function restoreCachedTurnDetails(
  summaries: Turn[],
  cachedTurns: readonly Turn[],
): Turn[] {
  const matches = new Array<number>(summaries.length).fill(-1);
  const usedCached = new Set<number>();

  // Reserve every authoritative identity before considering the timestamp
  // compatibility fallback. Otherwise a nearby repeated prompt can consume a
  // cache row which belongs exactly to another summary.
  for (let summaryIndex = 0; summaryIndex < summaries.length; summaryIndex += 1) {
    const cachedIndex = cachedTurns.findIndex((candidate, index) =>
      !usedCached.has(index)
      && sameTurnIdentity(summaries[summaryIndex], candidate));
    if (cachedIndex < 0) continue;
    matches[summaryIndex] = cachedIndex;
    usedCached.add(cachedIndex);
  }

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

  let revealNewest = true;
  const restored = [...summaries];
  for (let summaryIndex = restored.length - 1; summaryIndex >= 0;
    summaryIndex -= 1) {
    const cachedIndex = matches[summaryIndex];
    if (cachedIndex < 0) continue;
    const summary = restored[summaryIndex];
    const installed = installCachedDetailRestore(
      summary, cachedTurns[cachedIndex], revealNewest);
    if (installed !== summary) revealNewest = false;
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
    detailProjection,
    detailHasMore: hasMore,
    detailOldestCursor: oldestCursor,
    detailHasNewer: hasNewer,
    detailNewerCursor: newerCursor,
    detailAutoLoad: !!summary.detailAutoLoad && hasMore,
    detailRestorePending: false,
    detailRestoreIncomplete: restoreIncomplete,
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
  } = {},
): Turn[] {
  const merged = history.map((turn) => ({ ...turn, blocks: turn.blocks.map((b) => ({ ...b })) }));
  const used = new Set<number>();
  const unmatched: Turn[] = [];

  for (const liveTurn of live) {
    let index = merged.findIndex((turn, i) => !used.has(i) && turn.id === liveTurn.id);
    if (index < 0) {
      index = merged.findIndex((turn, i) => !used.has(i) && sameTurn(turn, liveTurn));
    }
    if (index >= 0) {
      const isOpenLiveTail = !!options.preserveLiveTailOpen
        && liveTurn === live[live.length - 1]
        // A newer authoritative history turn proves this local placeholder is
        // no longer the active tail (for example, same-task steering). Only the
        // matching newest history row may inherit an unfinished live state.
        && index === merged.length - 1
        && !liveTurn.done;
      merged[index] = mergeTurn(merged[index], liveTurn, isOpenLiveTail);
      used.add(index);
    } else {
      unmatched.push({ ...liveTurn, blocks: liveTurn.blocks.map((b) => ({ ...b })) });
    }
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

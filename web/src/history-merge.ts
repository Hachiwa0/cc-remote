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
      if (existingIndex == null) existingIndex = candidates[0];
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

function sameTurn(history: Turn, live: Turn): boolean {
  if (history.id === live.id) return true;
  if (history.clientMsgId && (
    history.clientMsgId === live.id
    || history.clientMsgId === live.clientMsgId
  )) return true;
  if (live.clientMsgId && live.clientMsgId === history.id) return true;
  // Automatic/goal continuations have no user message. Live uses the app-server
  // turn id as its empty anchor, while rollout history may use the first
  // assistant item id; TurnEnd still supplies the same authoritative branch id.
  if (history.forkPointId && live.forkPointId
      && history.forkPointId === live.forkPointId) return true;
  if (history.forkPointId === live.id || live.forkPointId === history.id) return true;
  if (!history.prompt || !live.prompt || history.prompt !== live.prompt) return false;
  // Different ids are an optimistic-client id vs transcript id only when their
  // authoritative UserMsg times are nearly identical. Prompt text alone is not
  // an identity: repeated inputs such as "继续" are common.
  if (history.ts == null || live.ts == null) return false;
  return Math.abs(history.ts - live.ts) <= 3000;
}

export function historyContainsTurn(history: Turn[], live: Turn): boolean {
  return history.some((turn) => sameTurn(turn, live));
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
    detailLoaded: true,
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
    detailLoaded: true,
    detailLoading: false,
    detailProjection,
    detailHasMore: hasMore,
    detailOldestCursor: oldestCursor,
    detailHasNewer: hasNewer,
    detailNewerCursor: newerCursor,
    detailAutoLoad: !!summary.detailAutoLoad && hasMore,
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
  const seen = new Set<string>();
  return rows.map((row) => row.turn).filter((turn) => {
    if (seen.has(turn.id)) return false;
    seen.add(turn.id);
    return true;
  });
}

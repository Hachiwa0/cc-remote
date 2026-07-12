import type { Block, TextBlock, ToolBlock, Turn } from "./reducer";

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

function mergeBlocks(history: Block[], live: Block[]): Block[] {
  const out = history.map((block) => ({ ...block }));
  for (const block of live) {
    if (block.kind === "tool") {
      const existing = out.find((candidate) => candidate.kind === "tool"
        && candidate.tool_use_id === block.tool_use_id) as ToolBlock | undefined;
      if (existing) Object.assign(existing, block);
      else out.push({ ...block });
      continue;
    }
    let existing = out.find((candidate) => candidate.kind === "text"
      && candidate.message_id === block.message_id) as TextBlock | undefined;
    if (!existing) {
      existing = [...out].reverse().find((candidate) => candidate.kind === "text") as TextBlock | undefined;
    }
    if (existing) {
      existing.text = combineText(existing.text, block.text);
      existing.done = existing.done || block.done;
    } else {
      out.push({ ...block });
    }
  }
  return out;
}

function sameTurn(history: Turn, live: Turn): boolean {
  if (history.id === live.id) return true;
  if (!history.prompt || !live.prompt || history.prompt !== live.prompt) return false;
  // Different ids are an optimistic-client id vs transcript id only when their
  // authoritative UserMsg times are nearly identical. Prompt text alone is not
  // an identity: repeated inputs such as "继续" are common.
  if (history.ts == null || live.ts == null) return false;
  return Math.abs(history.ts - live.ts) <= 3000;
}

function mergeTurn(history: Turn, live: Turn, preserveLiveOpen = false): Turn {
  return {
    ...history,
    id: live.id,
    forkPointId: history.forkPointId ?? live.forkPointId,
    prompt: history.prompt || live.prompt,
    blocks: mergeBlocks(history.blocks, live.blocks),
    // A transcript has no ResultMessage, so its EOF is represented by a
    // synthetic TurnEnd.  While this same live tail is still running, that
    // marker is only a snapshot boundary and must not close the turn early.
    done: preserveLiveOpen ? live.done : history.done || live.done,
    interrupted: history.interrupted || live.interrupted,
    error: live.error ?? history.error,
    progress: preserveLiveOpen ? live.progress : undefined,
    images: live.images ?? history.images,
    files: live.files ?? history.files,
    ts: Math.min(history.ts ?? Number.MAX_SAFE_INTEGER,
      live.ts ?? Number.MAX_SAFE_INTEGER) === Number.MAX_SAFE_INTEGER
      ? undefined
      : Math.min(history.ts ?? Number.MAX_SAFE_INTEGER,
          live.ts ?? Number.MAX_SAFE_INTEGER),
    doneTs: preserveLiveOpen
      ? live.doneTs
      : Math.max(history.doneTs ?? 0, live.doneTs ?? 0) || undefined,
  };
}

/** Merge transcript history with cache/live state without deleting a just-finished
 * turn that hasn't flushed yet or duplicating the same prompt under engine ids. */
export function mergeInitialHistory(
  history: Turn[],
  live: Turn[],
  options: { preserveLiveTailOpen?: boolean } = {},
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
        && !liveTurn.done;
      merged[index] = mergeTurn(merged[index], liveTurn, isOpenLiveTail);
      used.add(index);
    } else {
      unmatched.push({ ...liveTurn, blocks: liveTurn.blocks.map((b) => ({ ...b })) });
    }
  }

  const rows = [...merged, ...unmatched].map((turn, order) => ({ turn, order }));
  rows.sort((a, b) => {
    if (a.turn.ts != null && b.turn.ts != null && a.turn.ts !== b.turn.ts) {
      return a.turn.ts - b.turn.ts;
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

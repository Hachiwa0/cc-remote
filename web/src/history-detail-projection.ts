import type { ServerEvent } from "./protocol";
import type {
  Block,
  ProcessBlock,
  Turn,
  TurnDetailProjection,
  TurnDetailSegment,
} from "./domain/conversation";
export type {
  TurnDetailProjection,
  TurnDetailSegment,
} from "./domain/conversation";

export const MAX_DETAIL_PROJECTION_ITEMS = 4_000;
export const MAX_DETAIL_PROJECTION_CHARS = 32 * 1024 * 1024;
export const DETAIL_PROJECTION_CAP_ITEM_ID =
  "__cc_remote_detail_projection_capped__";

const LATEST_DETAIL_PAGE_KEY = "__cc_remote_detail_latest__";

export interface TurnDetailProjectionPage {
  before?: string | null;
  events: ServerEvent[];
  hasMore?: boolean;
  oldestCursor?: string | null;
  hasNewer?: boolean;
  newerCursor?: string | null;
}

export interface InstalledTurnDetailProjection {
  projection: TurnDetailProjection;
  detail: Turn | undefined;
}

export function nextAutoLoadDetailTurn(
  turns: readonly Turn[],
): { turnId: string; before: string } | null {
  const turn = turns.find((candidate) =>
    candidate.detailAutoLoad === true
    && candidate.detailLoading !== true
    && candidate.detailHasMore === true
    && !!candidate.detailOldestCursor);
  return turn?.detailOldestCursor
    ? { turnId: turn.id, before: turn.detailOldestCursor }
    : null;
}

function pageKey(before: string | null | undefined): string {
  return before ?? LATEST_DETAIL_PAGE_KEY;
}

function encodedChars(value: unknown): number {
  try {
    return JSON.stringify(value)?.length ?? 0;
  } catch {
    return Number.MAX_SAFE_INTEGER;
  }
}

function capMarker(): ProcessBlock {
  return {
    kind: "process",
    item_id: DETAIL_PROJECTION_CAP_ITEM_ID,
    processKind: "compaction",
    phase: "snapshot",
    status: "succeeded",
    title: "较早过程已省略",
    summary: "完整过程超过浏览器 4000 项或 32 MiB 的安全上限，已保留较新的部分。",
    done: true,
  };
}

function isFinal(block: Block): boolean {
  return block.kind === "text" && block.channel === "final";
}

function visibleBlocks(detail: Turn | undefined): Block[] {
  return detail?.blocks.filter((block) => !isFinal(block)) ?? [];
}

function insertSegment(
  current: readonly TurnDetailSegment[],
  incoming: TurnDetailSegment,
): TurnDetailSegment[] {
  const withoutSamePage = current.filter(
    (segment) => segment.pageKey !== incoming.pageKey);
  if (incoming.before === null) return [...withoutSamePage, incoming];

  // An older request uses the oldest cursor of the page already on screen.
  // Insert directly before that anchor; cursor values are opaque and must
  // never be sorted lexically or numerically by the browser.
  const olderAnchor = withoutSamePage.findIndex(
    (segment) => segment.oldestCursor === incoming.before);
  if (olderAnchor >= 0) {
    return [
      ...withoutSamePage.slice(0, olderAnchor),
      incoming,
      ...withoutSamePage.slice(olderAnchor),
    ];
  }

  // This path supports a locally-triggered newer read without making it the
  // primary product interaction. It is also useful when a retry fills a hole.
  const newerAnchor = withoutSamePage.findIndex(
    (segment) => segment.newerCursor === incoming.before);
  if (newerAnchor >= 0) {
    return [
      ...withoutSamePage.slice(0, newerAnchor + 1),
      incoming,
      ...withoutSamePage.slice(newerAnchor + 1),
    ];
  }

  // A response whose linkage cannot be proven should not reorder already
  // accepted pages. `hasNewer` is authoritative evidence that it belongs on
  // the older side; otherwise keep it at the newest edge.
  return incoming.hasNewer
    ? [incoming, ...withoutSamePage]
    : [...withoutSamePage, incoming];
}

function flattenEvents(segments: readonly TurnDetailSegment[]): ServerEvent[] {
  return segments.flatMap((segment) => segment.events);
}

function segmentChars(segments: readonly TurnDetailSegment[]): number {
  return segments.reduce((sum, segment) => sum + segment.encodedChars, 0);
}

/** Add or replace one cursor-keyed detail page and materialize all retained
 * source events together.
 *
 * The decode callback deliberately belongs to reducer.ts, which already owns
 * the protocol-event-to-Block state machine. Keeping raw ordered segments here
 * avoids a second, subtly incompatible event translator.
 */
export function installTurnDetailProjectionPage(
  current: TurnDetailProjection | undefined,
  page: TurnDetailProjectionPage,
  decode: (events: ServerEvent[]) => Turn | undefined,
  limits: {
    maxItems?: number;
    maxChars?: number;
  } = {},
): InstalledTurnDetailProjection {
  const maxItems = Math.max(
    1, Math.floor(limits.maxItems ?? MAX_DETAIL_PROJECTION_ITEMS));
  const maxChars = Math.max(
    1, Math.floor(limits.maxChars ?? MAX_DETAIL_PROJECTION_CHARS));
  const before = page.before ?? null;
  const incoming: TurnDetailSegment = {
    pageKey: pageKey(before),
    before,
    events: page.events.map((event) => ({ ...event })),
    hasMore: !!page.hasMore,
    oldestCursor: page.oldestCursor ?? null,
    hasNewer: !!page.hasNewer,
    newerCursor: page.newerCursor ?? null,
    encodedChars: encodedChars(page.events),
  };

  // Once the explicit cap is visible, a late in-flight older response cannot
  // restart the automatic pagination loop or evict newer readable content.
  if (current?.capped && before !== null
      && !current.segments.some((segment) =>
        segment.pageKey === incoming.pageKey)) {
    return {
      projection: current,
      detail: decode(flattenEvents(current.segments)),
    };
  }

  let segments = insertSegment(current?.segments ?? [], incoming);
  let detail = decode(flattenEvents(segments));
  let blocks = visibleBlocks(detail);
  let capped = current?.capped ?? false;

  // Pages are bounded to 256 events/8 MiB by the wrapper. Drop only complete
  // oldest segments so lifecycle ordering inside each retained page remains
  // intact. The canonical final answer lives outside this projection.
  while (segments.length > 1 && (
    blocks.length > maxItems
    || encodedChars(blocks) > maxChars
    || segmentChars(segments) > maxChars
  )) {
    segments = segments.slice(1);
    capped = true;
    detail = decode(flattenEvents(segments));
    blocks = visibleBlocks(detail);
  }

  if (blocks.length > maxItems || encodedChars(blocks) > maxChars) {
    capped = true;
    let retained = blocks.slice(-maxItems);
    while (retained.length > 1 && encodedChars(retained) > maxChars) {
      retained = retained.slice(Math.max(1, Math.ceil(retained.length / 16)));
    }
    blocks = retained;
  }
  if (capped) blocks = [capMarker(), ...blocks];

  const oldest = segments[0];
  const newest = segments.at(-1);
  return {
    projection: {
      segments,
      blocks,
      capped,
      hasMore: capped ? false : !!oldest?.hasMore,
      oldestCursor: capped ? null : oldest?.oldestCursor ?? null,
      hasNewer: !!newest?.hasNewer,
      newerCursor: newest?.newerCursor ?? null,
    },
    detail,
  };
}

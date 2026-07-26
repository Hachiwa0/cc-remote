import assert from "node:assert/strict";

import {
  appendNewerPage,
  canonicalTurnId,
  createHistoryBrowse,
  markBrowseDetail,
  markBrowseDetailLoading,
  markBrowseLatestDirty,
  markBrowseNewerUnavailable,
  settleBrowsePageRequest,
  prependOlderPage,
  type HistoryBrowseLimits,
  type HistoryBrowsePage,
} from "../src/history-browse.ts";
import type { Turn } from "../src/reducer.ts";

function turn(
  id: string,
  options: Partial<Turn> = {},
): Turn {
  return {
    id,
    prompt: `prompt-${id}`,
    blocks: [],
    done: true,
    ts: Number(id.replace(/\D/g, "")) || undefined,
    ...options,
  };
}

function limits(maxTurns: number, lowWaterTurns = maxTurns): HistoryBrowseLimits {
  return {
    maxTurns,
    lowWaterTurns,
    maxBytes: Number.MAX_SAFE_INTEGER,
    lowWaterBytes: Number.MAX_SAFE_INTEGER,
  };
}

const scopeKey = "machine-a\u0000code\u0000codex";

assert.equal(canonicalTurnId(turn("optimistic", {
  historyTurnId: "native-user-message",
})), "native-user-message");
assert.equal(canonicalTurnId(turn("plain")), "plain");

const olderPage: HistoryBrowsePage = {
  pageKey: "page-older",
  turns: [turn("1"), turn("2")],
  hasOlder: true,
  olderCursor: "1",
  newerPageKey: "page-latest",
};
const initial = createHistoryBrowse({
  scopeKey,
  sid: "session-a",
  revision: "revision-a",
  generation: "generation-a",
  viewId: "view-a",
  baseTurns: [turn("3"), turn("4")],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "3",
  olderPage,
  limits: limits(10),
});
assert.deepEqual(initial.projection.turns.map((item) => item.id), [
  "1", "2", "3", "4",
]);
assert.deepEqual(initial.projection.loadedPageKeys, [
  "page-older", "page-latest",
]);
assert.equal(initial.projection.scopeKey, scopeKey);
assert.equal(initial.projection.viewId, "view-a");
assert.equal(initial.projection.windowEpoch, 1);
assert.equal(initial.projection.hasOlder, true);
assert.equal(initial.projection.olderCursor, "1");
assert.equal(initial.projection.hasNewer, false);
assert.deepEqual(initial.evictedPages, []);

// The newer runtime identity wins an overlap with a native history page, while
// the canonical id still deduplicates both rows.
const overlap = createHistoryBrowse({
  scopeKey,
  sid: "session-overlap",
  revision: "revision-overlap",
  viewId: "view-overlap",
  baseTurns: [turn("optimistic-2", {
    historyTurnId: "native-2",
    prompt: "same",
  }), turn("3")],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "native-2",
  olderPage: {
    pageKey: "page-older",
    turns: [turn("1"), turn("native-2", { prompt: "same" })],
    hasOlder: false,
    olderCursor: "1",
    newerPageKey: "page-latest",
  },
  limits: limits(10),
}).projection;
assert.deepEqual(overlap.turns.map((item) => item.id), [
  "1", "optimistic-2", "3",
]);

// Loading older rows evicts completed rows from the newer edge down to the low
// water mark and returns the complete pre-eviction segment for IndexedDB.
const tailEviction = createHistoryBrowse({
  scopeKey,
  sid: "session-tail",
  revision: "revision-tail",
  viewId: "view-tail",
  baseTurns: [turn("4"), turn("5")],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "4",
  olderPage: {
    pageKey: "page-older",
    turns: [turn("1"), turn("2"), turn("3")],
    hasOlder: false,
    olderCursor: "1",
    newerPageKey: "page-latest",
  },
  limits: limits(4, 3),
});
assert.deepEqual(tailEviction.projection.turns.map((item) => item.id), [
  "1", "2", "3",
]);
assert.equal(tailEviction.projection.hasNewer, true);
assert.equal(tailEviction.projection.newerPageKey, "page-latest");
assert.equal(tailEviction.evictedPages.length, 1);
assert.equal(tailEviction.evictedPages[0].pageKey, "page-latest");
assert.deepEqual(tailEviction.evictedPages[0].turns.map((item) => item.id), [
  "4", "5",
]);

// A local newer page reverses the window. Its head eviction exposes a valid
// server-before cursor at the oldest retained row.
const headEviction = appendNewerPage(tailEviction.projection, {
  pageKey: "page-latest",
  turns: [turn("4"), turn("5")],
  isLatest: true,
  hasNewer: false,
  newerPageKey: null,
}, {
  expectedScopeKey: scopeKey,
  limits: limits(4, 3),
});
assert.deepEqual(headEviction.projection.turns.map((item) => item.id), [
  "3", "4", "5",
]);
assert.equal(headEviction.projection.hasOlder, true);
assert.equal(headEviction.projection.olderCursor, "3");
assert.equal(headEviction.projection.hasNewer, false);
assert.equal(headEviction.projection.newerPageKey, null);
assert.equal(headEviction.evictedPages[0].pageKey, "page-older");
assert.deepEqual(headEviction.evictedPages[0].turns.map((item) => item.id), [
  "1", "2", "3",
]);

// A viewport anchor is a hard eviction boundary. Going past it would create a
// hole around the row whose pixel offset ChatView is preserving.
const anchoredBase = createHistoryBrowse({
  scopeKey,
  sid: "session-anchor",
  revision: "revision-anchor",
  viewId: "view-anchor",
  baseTurns: [turn("3"), turn("4")],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "3",
  limits: limits(3, 2),
}).projection;
const anchored = prependOlderPage(anchoredBase, {
  pageKey: "page-older",
  turns: [turn("1"), turn("2")],
  hasOlder: false,
  olderCursor: "1",
  newerPageKey: "page-latest",
}, {
  expectedScopeKey: scopeKey,
  protectedTurnIds: ["4"],
  limits: limits(3, 2),
});
assert.deepEqual(anchored.projection.turns.map((item) => item.id), [
  "1", "2", "3", "4",
]);
assert.deepEqual(anchored.evictedPages, []);

// The browse projection is display-only: an active runtime tail must not act
// as an implicit anchor and let each older page grow the window forever.
const active = turn("30", {
  done: false,
  blocks: [{
    kind: "process",
    item_id: "process-30",
    processKind: "command",
    phase: "start",
    status: "running",
    title: "running",
    done: false,
  }],
});
const activeFirstPage = createHistoryBrowse({
  scopeKey,
  sid: "session-active",
  revision: "revision-active",
  viewId: "view-active",
  baseTurns: [
    ...Array.from({ length: 9 }, (_, index) => turn(String(index + 21))),
    active,
  ],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "21",
  olderPage: {
    pageKey: "page-11-20",
    turns: Array.from({ length: 10 }, (_, index) => turn(String(index + 11))),
    hasOlder: true,
    olderCursor: "11",
    newerPageKey: "page-latest",
  },
  limits: limits(20, 16),
});
assert.equal(activeFirstPage.projection.turns.length, 20);
assert.equal(activeFirstPage.projection.turns.at(-1)?.id, "30");
const activeBounded = prependOlderPage(activeFirstPage.projection, {
  pageKey: "page-1-10",
  turns: Array.from({ length: 10 }, (_, index) => turn(String(index + 1))),
  hasOlder: false,
  olderCursor: "1",
  newerPageKey: "page-11-20",
}, {
  expectedScopeKey: scopeKey,
  expectedOlderCursor: "11",
  limits: limits(20, 16),
});
assert.deepEqual(activeBounded.projection.turns.map((item) => item.id),
  Array.from({ length: 16 }, (_, index) => String(index + 1)));
assert.ok(activeBounded.projection.turns.length <= 20);
assert.equal(activeBounded.projection.turns.some((item) => item.id === "30"),
  false, "the authoritative runtime, not browse, owns the active tail");
assert.equal(activeBounded.projection.hasNewer, true);
assert.equal(activeBounded.projection.newerPageKey, "page-11-20");

// The same rule applies to the byte bound: open process output may be dropped
// from browse while it remains authoritative in SessionRuntime.
const byteBounded = createHistoryBrowse({
  scopeKey,
  sid: "session-active-bytes",
  revision: "revision-active-bytes",
  viewId: "view-active-bytes",
  baseTurns: [turn("3"), turn("4", {
    done: false,
    prompt: "x".repeat(4096),
    blocks: [{
      kind: "process",
      item_id: "process-4",
      processKind: "command",
      phase: "update",
      status: "running",
      title: "running",
      output: "y".repeat(4096),
      done: false,
    }],
  })],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "3",
  olderPage: {
    pageKey: "page-older",
    turns: [turn("1"), turn("2")],
    hasOlder: false,
    olderCursor: "1",
    newerPageKey: "page-latest",
  },
  limits: {
    maxTurns: 100,
    lowWaterTurns: 100,
    maxBytes: 2048,
    lowWaterBytes: 1024,
  },
});
assert.ok(new TextEncoder().encode(
  JSON.stringify(byteBounded.projection.turns),
).byteLength <= 2048);
assert.equal(byteBounded.projection.turns.some((item) => item.id === "4"), false);

// detailLoading is also not an implicit page anchor. A frozen viewport anchor
// remains the sole reason browse may intentionally overflow its bound.
const detailLoadingBase = {
  scopeKey,
  sid: "session-detail-boundary",
  revision: "revision-detail-boundary",
  viewId: "view-detail-boundary",
  baseTurns: [turn("3"), turn("4", { detailLoading: true })],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "3",
  olderPage: {
    pageKey: "page-older",
    turns: [turn("1"), turn("2")],
    hasOlder: false,
    olderCursor: "1",
    newerPageKey: "page-latest",
  },
  limits: limits(3, 2),
};
const unanchoredDetail = createHistoryBrowse(detailLoadingBase);
assert.deepEqual(unanchoredDetail.projection.turns.map((item) => item.id),
  ["1", "2"]);
const anchoredDetail = createHistoryBrowse({
  ...detailLoadingBase,
  protectedTurnIds: ["4"],
});
assert.deepEqual(anchoredDetail.projection.turns.map((item) => item.id),
  ["1", "2", "3", "4"]);
assert.deepEqual(anchoredDetail.evictedPages, []);

// Frozen request scope is an authorization/display boundary. A late page from
// another machine/surface leaves this projection byte-for-byte unchanged.
const staleScope = prependOlderPage(initial.projection, {
  pageKey: "wrong-scope-page",
  turns: [turn("0")],
  hasOlder: false,
  olderCursor: "0",
}, {
  expectedScopeKey: "machine-b\u0000work\u0000claude",
  limits: limits(10),
});
assert.equal(staleScope.projection, initial.projection);
assert.deepEqual(staleScope.evictedPages, []);
assert.equal(prependOlderPage(initial.projection, {
  pageKey: "stale-epoch-page",
  turns: [turn("0")],
  hasOlder: false,
  olderCursor: "0",
}, {
  expectedScopeKey: scopeKey,
  expectedViewId: initial.projection.viewId,
  expectedWindowEpoch: initial.projection.windowEpoch - 1,
  expectedOlderCursor: initial.projection.olderCursor,
  limits: limits(10),
}).projection, initial.projection);

const detailed = markBrowseDetail(overlap, "native-2", turn("native-2", {
  prompt: "same",
  blocks: [{
    kind: "text",
    message_id: "answer-2",
    channel: "final",
    text: "full answer",
    done: true,
  }],
  detailLoaded: true,
}), {
  expectedScopeKey: scopeKey,
});
assert.equal(detailed.windowEpoch, overlap.windowEpoch,
  "detail hydration changes row height, not the page-window epoch");
assert.equal(detailed.turns[1].id, "optimistic-2");
assert.equal(detailed.turns[1].detailLoaded, true);
assert.equal(detailed.turns[1].blocks[0].kind, "text");
assert.equal(
  detailed.turns[1].blocks[0].kind === "text"
    ? detailed.turns[1].blocks[0].text
    : "",
  "full answer",
);

const loading = markBrowseDetailLoading(
  overlap, "native-2", true, {
    expectedScopeKey: scopeKey,
    expectedViewId: overlap.viewId,
    expectedWindowEpoch: overlap.windowEpoch,
  });
assert.equal(loading.turns[1].detailLoading, true);
assert.equal(loading.windowEpoch, overlap.windowEpoch);
assert.equal(markBrowseDetailLoading(
  loading, "native-2", false, {
    expectedScopeKey: "another-scope",
  }), loading, "a stale detail target cannot mutate the visible browse row");

const cacheMiss = markBrowseNewerUnavailable(tailEviction.projection, {
  expectedScopeKey: scopeKey,
  expectedViewId: tailEviction.projection.viewId,
  expectedWindowEpoch: tailEviction.projection.windowEpoch,
});
assert.equal(cacheMiss.hasNewer, false);
assert.equal(cacheMiss.newerPageKey, null);
assert.deepEqual(cacheMiss.turns, tailEviction.projection.turns,
  "a local newer-page cache miss keeps the readable older window intact");
const failedOlder = settleBrowsePageRequest(initial.projection, {
  expectedScopeKey: scopeKey,
  expectedViewId: initial.projection.viewId,
  expectedWindowEpoch: initial.projection.windowEpoch,
});
assert.equal(failedOlder.windowEpoch, initial.projection.windowEpoch + 1);
assert.deepEqual(failedOlder.turns, initial.projection.turns);
assert.equal(settleBrowsePageRequest(failedOlder, {
  expectedScopeKey: scopeKey,
  expectedViewId: failedOlder.viewId,
  expectedWindowEpoch: initial.projection.windowEpoch,
}), failedOlder, "a second stale failure cannot advance the active window");

const dirty = markBrowseLatestDirty(detailed, {
  expectedScopeKey: scopeKey,
});
assert.equal(dirty.latestDirty, true);
assert.equal(markBrowseLatestDirty(dirty, {
  expectedScopeKey: scopeKey,
}), dirty);

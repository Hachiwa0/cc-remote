import assert from "node:assert/strict";
import {
  HISTORY_DETAIL_REQUEST_TIMEOUT_MS,
  HistoryDetailRequestCoordinator,
  HistoryRequestCoordinator,
  resolveHistoryCwdHint,
  type HistoryBrowseRequestContext,
  type HistoryDetailRequestContext,
} from "../src/history-requests.ts";
import { RecoverableReadCoordinator } from "../src/recoverable-read.ts";
import {
  HistoryImageAssetCache,
  historyImageAssetKey,
} from "../src/history-image-assets.ts";

let now = 1_000;
const coordinator = new HistoryRequestCoordinator(() => now, 500);
let sends = 0;
const send = () => { sends += 1; return true; };

coordinator.beginConnection();
assert.equal(coordinator.request({
  sid: "session-1", limit: 4,
}, send), true);
// ReplayStart must replace a generic focus read already on the wire. Relabelling
// that request would let its older-generation response strand recovery forever.
assert.equal(coordinator.request({
  sid: "session-1", limit: 4, generation: "generation-1",
}, send), true);
assert.equal(sends, 2);
coordinator.complete({
  session_id: "session-1", generation: "generation-0",
});
assert.equal(coordinator.size(), 1,
  "the response to the generic request cannot complete its generation-bound replacement");
coordinator.complete({
  session_id: "session-1",
});
assert.equal(coordinator.size(), 1,
  "an untagged response cannot complete a generation-bound replacement");
assert.equal(coordinator.request({
  sid: "session-1", limit: 4, generation: "generation-1",
}, send), false);
assert.equal(sends, 2);

// Pagination and another session are independent.
assert.equal(coordinator.request({
  sid: "session-1", before: "turn-5", limit: 12,
}, send), true);
assert.equal(coordinator.request({
  sid: "session-2", limit: 4,
}, send), true);
assert.equal(sends, 4);

const acceptedHistoryLists = {
  "code:claude": [{
    session_id: "session-1",
    engine: "claude" as const,
    space: "code" as const,
    cwd: "/project/one",
  }],
  "work:claude": [{
    session_id: "session-2",
    engine: "claude" as const,
    space: "work" as const,
    cwd: "/project/two",
  }],
};
assert.equal(
  resolveHistoryCwdHint(acceptedHistoryLists, "session-1"),
  "/project/one",
);
assert.equal(resolveHistoryCwdHint(acceptedHistoryLists, "missing"), undefined);
assert.equal(resolveHistoryCwdHint({
  ...acceptedHistoryLists,
  "code:codex": [{
    session_id: "session-1",
    engine: "codex" as const,
    space: "code" as const,
    cwd: "/different/project",
  }],
}, "session-1"), undefined,
"conflicting accepted scopes must omit the optional hint");

// An older response must not clear a rollback-bound replacement request.
assert.equal(coordinator.request({
  sid: "session-1", limit: 4,
  generation: "generation-1", revision: "revision-2",
}, send), true);
assert.equal(sends, 5);
coordinator.complete({
  session_id: "session-1", generation: "generation-1",
  revision: "revision-1",
});
assert.equal(coordinator.size(), 3);
coordinator.complete({
  session_id: "session-1", generation: "generation-1",
  revision: "revision-2",
});
assert.equal(coordinator.size(), 2);

// A new socket and a timed-out request may retry exactly once.
coordinator.beginConnection();
assert.equal(coordinator.size(), 0);
assert.equal(coordinator.request({ sid: "session-1", limit: 4 }, send), true);
now += 600;
assert.equal(coordinator.request({ sid: "session-1", limit: 4 }, send), true);
assert.equal(sends, 7);

const rejectedSendCoordinator = new HistoryRequestCoordinator(() => now, 500);
rejectedSendCoordinator.beginConnection();
assert.equal(rejectedSendCoordinator.request(
  { sid: "rejected-send", before: "cursor", limit: 12 },
  () => false,
), false);
assert.equal(rejectedSendCoordinator.size(), 0,
  "an outbox rejection must not leave a phantom in-flight history request");
assert.equal(rejectedSendCoordinator.request(
  { sid: "rejected-send", before: "cursor", limit: 12 },
  () => true,
), true, "the next pagination gesture may retry after a rejected enqueue");

// A wire pagination read may be shared by two local browse epochs, but the
// immutable request-time contexts must remain distinct. The response handler
// decides which waiter still owns the visible projection; it must never stamp
// the response with whichever session/surface happens to be current on arrival.
const browseCoordinator = new HistoryRequestCoordinator(() => now, 500);
const browseSends: string[] = [];
const browseContext = (
  viewId: string,
  windowEpoch: number,
  scopeKey = "machine-a:code:codex",
): HistoryBrowseRequestContext => ({
  scopeKey,
  viewId,
  windowEpoch,
  pendingBefore: "turn-5",
  sourcePageKey: "head-page",
});
browseCoordinator.beginConnection();
assert.equal(browseCoordinator.request({
  sid: "session-browse", before: "turn-5", limit: 12,
  generation: "generation-a", revision: "revision-a",
  browse: browseContext("view-old", 1),
}, () => { browseSends.push("wire"); return true; }), true);
assert.equal(browseCoordinator.request({
  sid: "session-browse", before: "turn-5", limit: 12,
  generation: "generation-a", revision: "revision-a",
  browse: browseContext("view-new", 2),
}, () => { browseSends.push("wire"); return true; }), true,
"a newer local browse epoch may wait on the already in-flight wire page");
assert.deepEqual(browseSends, ["wire"],
"local browse waiters must not duplicate the server scan");
assert.deepEqual(
  browseCoordinator.complete({
    session_id: "session-browse", before: "turn-5",
    generation: "generation-a", revision: "revision-a",
  }).map((context) => [context.scopeKey, context.viewId, context.windowEpoch]),
  [
    ["machine-a:code:codex", "view-old", 1],
    ["machine-a:code:codex", "view-new", 2],
  ],
);
assert.equal(browseCoordinator.size(), 0);

assert.equal(browseCoordinator.request({
  sid: "session-browse", before: "turn-5", limit: 12,
  generation: "generation-b", revision: "revision-b",
  browse: browseContext("view-revision-b", 3),
}, () => { browseSends.push("revision-b"); return true; }), true);
assert.deepEqual(browseCoordinator.complete({
  session_id: "session-browse", before: "turn-5",
  generation: "generation-b", revision: "revision-a",
}), [], "an old revision cannot consume a newer browse waiter");
assert.equal(browseCoordinator.size(), 1);
browseCoordinator.beginConnection();
assert.deepEqual(browseCoordinator.complete({
  session_id: "session-browse", before: "turn-5",
  generation: "generation-b", revision: "revision-b",
}), [], "a response from the previous socket has no local browse authority");

const detailCoordinator = new HistoryDetailRequestCoordinator();
assert.equal(detailCoordinator.begin({
  target: "browse",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "older-turn",
  viewId: "view-new",
  windowEpoch: 2,
}), true);
assert.equal(detailCoordinator.begin({
  target: "browse",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "older-turn",
  viewId: "view-new",
  windowEpoch: 2,
}), false, "one wire detail read must serve one frozen browse target");
assert.equal(detailCoordinator.complete({
  session_id: "session-browse",
  revision: "revision-old",
  turn_id: "older-turn",
}), null, "a stale detail response cannot consume the current browse target");
assert.deepEqual(detailCoordinator.complete({
  session_id: "session-browse",
  revision: "revision-a",
  turn_id: "older-turn",
}), {
  target: "browse",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "older-turn",
  viewId: "view-new",
  windowEpoch: 2,
});
const newestDetailContext = {
  target: "runtime" as const,
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "paged-turn",
};
const olderDetailContext = {
  ...newestDetailContext,
  before: "detail-cursor-older",
};
assert.equal(detailCoordinator.begin(newestDetailContext), true);
assert.equal(detailCoordinator.begin(olderDetailContext), true,
  "different intra-turn cursors need independent frozen request authority");
assert.equal(detailCoordinator.begin(olderDetailContext), false,
  "the same intra-turn cursor still deduplicates one wire read");
assert.deepEqual(detailCoordinator.complete({
  session_id: newestDetailContext.sid,
  revision: newestDetailContext.revision,
  turn_id: newestDetailContext.turnId,
}), newestDetailContext,
  "the newest detail response consumes only the cursorless request");
assert.equal(detailCoordinator.complete({
  session_id: olderDetailContext.sid,
  revision: olderDetailContext.revision,
  turn_id: olderDetailContext.turnId,
  before: "another-detail-cursor",
}), null, "a response for another detail cursor cannot consume the pending page");
assert.deepEqual(detailCoordinator.complete({
  session_id: olderDetailContext.sid,
  revision: olderDetailContext.revision,
  turn_id: olderDetailContext.turnId,
  before: olderDetailContext.before,
}), olderDetailContext,
  "the response before cursor is part of the exact coordinator key");
detailCoordinator.begin({
  target: "runtime",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "latest-turn",
});
assert.deepEqual(detailCoordinator.clear(), [{
  target: "runtime",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "latest-turn",
}], "clearing frozen targets returns the exact loading rows App must release");
assert.equal(detailCoordinator.complete({
  session_id: "session-browse",
  revision: "revision-a",
  turn_id: "latest-turn",
}), null, "navigation/reconnect must revoke frozen detail authority");

let nextDetailTimer = 1;
const detailTimers = new Map<number, {
  callback: () => void;
  delayMs: number;
}>();
const cancelledDetailTimers: number[] = [];
const timedOutDetailContexts: HistoryDetailRequestContext[] = [];
const expiringDetailCoordinator = new HistoryDetailRequestCoordinator(
  (context) => timedOutDetailContexts.push(context),
  (callback, delayMs) => {
    const timer = nextDetailTimer++;
    detailTimers.set(timer, { callback, delayMs });
    return timer;
  },
  (timer) => {
    const id = timer as number;
    cancelledDetailTimers.push(id);
    detailTimers.delete(id);
  },
);
const expiringDetailContext = {
  target: "runtime" as const,
  scopeKey: "machine-a:code:codex",
  sid: "session-timeout",
  revision: "revision-timeout",
  turnId: "turn-timeout",
};
assert.equal(expiringDetailCoordinator.begin(expiringDetailContext), true);
assert.equal(detailTimers.get(1)?.delayMs, HISTORY_DETAIL_REQUEST_TIMEOUT_MS);
detailTimers.get(1)?.callback();
assert.deepEqual(timedOutDetailContexts, [expiringDetailContext],
  "a lost response must release the exact frozen detail row");
assert.equal(expiringDetailCoordinator.begin(expiringDetailContext), true,
  "the user can retry after a silent detail timeout");
const completedTimerCallback = detailTimers.get(2)!.callback;
assert.deepEqual(expiringDetailCoordinator.complete({
  session_id: expiringDetailContext.sid,
  revision: expiringDetailContext.revision,
  turn_id: expiringDetailContext.turnId,
}), expiringDetailContext);
assert.ok(cancelledDetailTimers.includes(2),
  "a completed response must cancel its liveness timer");
assert.equal(expiringDetailCoordinator.begin(expiringDetailContext), true);
completedTimerCallback();
assert.deepEqual(timedOutDetailContexts, [expiringDetailContext],
  "a completed timer callback cannot later release its same-key retry");
assert.deepEqual(expiringDetailCoordinator.complete({
  session_id: expiringDetailContext.sid,
  revision: expiringDetailContext.revision,
  turn_id: expiringDetailContext.turnId,
}), expiringDetailContext,
  "the same-key retry remains pending after the old timer callback");

const clearRuntimeContext = {
  ...expiringDetailContext,
  turnId: "turn-clear-runtime",
};
const clearBrowseContext = {
  target: "browse" as const,
  scopeKey: expiringDetailContext.scopeKey,
  sid: expiringDetailContext.sid,
  revision: expiringDetailContext.revision,
  turnId: "turn-clear-browse",
  viewId: "view-clear",
  windowEpoch: 7,
};
assert.equal(expiringDetailCoordinator.begin(clearRuntimeContext), true);
assert.equal(expiringDetailCoordinator.begin(clearBrowseContext), true);
const clearedRuntimeCallback = detailTimers.get(4)!.callback;
const clearedBrowseCallback = detailTimers.get(5)!.callback;
assert.deepEqual(expiringDetailCoordinator.clear(), [
  clearRuntimeContext, clearBrowseContext,
], "clear returns every runtime and browse loading row");
assert.ok(cancelledDetailTimers.includes(4));
assert.ok(cancelledDetailTimers.includes(5));
assert.equal(expiringDetailCoordinator.begin(clearRuntimeContext), true);
clearedRuntimeCallback();
clearedBrowseCallback();
assert.deepEqual(timedOutDetailContexts, [expiringDetailContext],
  "callbacks cancelled by navigation cannot touch a later request");
assert.deepEqual(expiringDetailCoordinator.complete({
  session_id: clearRuntimeContext.sid,
  revision: clearRuntimeContext.revision,
  turn_id: clearRuntimeContext.turnId,
}), clearRuntimeContext);

let nextTimer = 1;
const scheduled = new Map<number, () => void>();
const scheduledDelays = new Map<number, number>();
const repair = new RecoverableReadCoordinator(
  (callback, delayMs) => {
    const timer = nextTimer++;
    scheduled.set(timer, callback);
    scheduledDelays.set(timer, delayMs);
    return timer;
  },
  (timer) => {
    scheduled.delete(timer);
    scheduledDelays.delete(timer);
  },
  250,
);
let repairs = 0;
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), true);
assert.equal(scheduledDelays.get(1), 250,
  "ordinary failures retain the short default retry");
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), false,
  "duplicate failures cannot schedule parallel repair reads");
assert.equal(scheduled.size, 1);
const firstRepair = scheduled.get(1);
scheduled.delete(1);
scheduledDelays.delete(1);
firstRepair?.();
assert.equal(repairs, 1);
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), false,
  "the failed repair response must stop instead of looping");
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), true,
  "a later explicit request starts a fresh one-shot repair cycle");
repair.complete("detail:turn-1");
assert.equal(scheduled.size, 0,
  "an authoritative response cancels a repair that has not fired");

assert.equal(repair.retry("history:page-1", () => { repairs += 1; }), true);
repair.clear();
assert.equal(scheduled.size, 0,
  "disconnect cleanup cancels every pending repair");

const movingPreviewKey = "history:moving-preview";
assert.equal(repair.retry(
  movingPreviewKey, () => { repairs += 1; }, 60_000), true);
const cancelledWatchdog = [...scheduled.keys()][0];
assert.equal(scheduledDelays.get(cancelledWatchdog), 60_000,
  "a moving newest-page preview may override the retry with a slow watchdog");
repair.complete(movingPreviewKey);
assert.equal(scheduled.size, 0,
  "an authoritative background refresh cancels the slow watchdog");

assert.equal(repair.retry(
  movingPreviewKey, () => { repairs += 1; }, 60_000), true);
const firedWatchdog = [...scheduled.keys()][0];
const watchdogRead = scheduled.get(firedWatchdog);
scheduled.delete(firedWatchdog);
scheduledDelays.delete(firedWatchdog);
watchdogRead?.();
const repairsAfterWatchdog = repairs;
assert.equal(repair.retry(
  movingPreviewKey, () => { repairs += 1; }, 60_000), false,
  "one fired watchdog is the upper bound for the current failure cycle");
assert.equal(repairs, repairsAfterWatchdog);

const images = new HistoryImageAssetCache(2);
assert.equal(images.begin({
  sid: "session-1", turnId: "turn-1", imageId: "image-1",
  variant: "thumbnail", requestId: "image-request-1", revision: "revision-1",
}), true);
assert.equal(images.accept({
  v: 20, type: "history_image", ts: 1,
  session_id: "session-2", turn_id: "turn-1", image_id: "image-1",
  variant: "thumbnail", request_id: "image-request-1", revision: "revision-1",
  media_type: "image/webp", data: "abc",
}), false, "a delayed response from another session cannot fill this cache");
assert.equal(images.accept({
  v: 20, type: "history_image", ts: 1,
  session_id: "session-1", turn_id: "turn-1", image_id: "image-1",
  variant: "thumbnail", request_id: "image-request-1", revision: "revision-1",
  media_type: "image/webp", width: 10, height: 5, data: "abc",
}), true);
assert.equal(images.forSession("session-1")[
  historyImageAssetKey("turn-1", "image-1", "thumbnail")
].status, "ready");
assert.deepEqual(images.forSession("session-2"), {});

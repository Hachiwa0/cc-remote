import assert from "node:assert/strict";
import { HistoryRequestCoordinator } from "../src/history-requests.ts";

let now = 1_000;
const coordinator = new HistoryRequestCoordinator(() => now, 500);
let sends = 0;
const send = () => { sends += 1; };

coordinator.beginConnection();
assert.equal(coordinator.request({
  sid: "session-1", limit: 4,
}, send), true);
// connected, wrapper_reconnected and replay_start collapse even when the first
// focus request did not yet know the wrapper generation.
assert.equal(coordinator.request({
  sid: "session-1", limit: 4, generation: "generation-1",
}, send), false);
assert.equal(coordinator.request({
  sid: "session-1", limit: 4, generation: "generation-1",
}, send), false);
assert.equal(sends, 1);

// Pagination and another session are independent.
assert.equal(coordinator.request({
  sid: "session-1", before: "turn-5", limit: 12,
}, send), true);
assert.equal(coordinator.request({
  sid: "session-2", limit: 4,
}, send), true);
assert.equal(sends, 3);

// An older response must not clear a rollback-bound replacement request.
assert.equal(coordinator.request({
  sid: "session-1", limit: 4,
  generation: "generation-1", revision: "revision-2",
}, send), true);
assert.equal(sends, 4);
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
assert.equal(sends, 6);

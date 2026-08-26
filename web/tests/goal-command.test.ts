import assert from "node:assert/strict";
import { parseGoalCommand } from "../src/goal-command.js";
import { resetGoalDismissMigrationTracking } from
  "../src/scoped-goal-ui.js";

assert.deepEqual(parseGoalCommand("", "codex"), { kind: "show" });
assert.deepEqual(parseGoalCommand("   ", "claude"), { kind: "show" });
assert.deepEqual(parseGoalCommand("clear", "codex"), { kind: "clear" });
assert.deepEqual(parseGoalCommand(" CLEAR ", "claude"), { kind: "clear" });
assert.deepEqual(parseGoalCommand("resume", "codex"), { kind: "resume" });
assert.deepEqual(parseGoalCommand(" RESUME ", "codex"), { kind: "resume" });
assert.deepEqual(parseGoalCommand("resume", "claude"), {
  kind: "set", objective: "resume",
});
assert.deepEqual(parseGoalCommand(" RESUME ", "claude"), {
  kind: "set", objective: "RESUME",
});
assert.deepEqual(parseGoalCommand("resume release", "codex"), {
  kind: "set", objective: "resume release",
});
assert.deepEqual(parseGoalCommand("ship the release", "claude"), {
  kind: "set", objective: "ship the release",
});

const migrationKey = "machine\0session\0goal";
const dismissMigrations = new Set([migrationKey]);
const migrationByRequest = new Map([["replayed-request", migrationKey]]);
resetGoalDismissMigrationTracking(dismissMigrations, migrationByRequest);
assert.equal(dismissMigrations.size, 0);
assert.equal(migrationByRequest.size, 0,
  "reconnect resets both Goal-dismiss maps after reliable replay");
assert.equal(dismissMigrations.has(migrationKey), false,
  "fresh GoalState can retry a migration after the replayed request errors");
dismissMigrations.add(migrationKey);
migrationByRequest.set("fresh-request", migrationKey);
assert.equal(migrationByRequest.get("fresh-request"), migrationKey,
  "the retry establishes a fresh request correlation");

console.log("goal command tests passed");

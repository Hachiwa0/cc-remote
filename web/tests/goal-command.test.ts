import assert from "node:assert/strict";
import { parseGoalCommand } from "../src/goal-command.js";

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

console.log("goal command tests passed");

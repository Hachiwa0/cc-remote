import assert from "node:assert/strict";
import type { SessionInfo } from "../src/protocol.ts";
import { makeForkSessionWorktreeCommand } from "../src/protocol.ts";
import {
  isWorktreeForkNameValid,
  isWorktreeForkBlockedByState,
  isTerminalWorktreeForkError,
  matchesWorktreeForkRequest,
  normalizeWorktreeForkName,
  sessionMenuCapabilities,
  WORKTREE_FORK_NAME_MAX,
} from "../src/session-worktree.ts";

const codex: SessionInfo = { session_id: "codex-parent", engine: "codex" };
const claude: SessionInfo = { session_id: "claude-parent", engine: "claude" };
const archivedCodex: SessionInfo = { session_id: "codex-archived", engine: "codex", tag: "archived" };

assert.deepEqual(sessionMenuCapabilities(codex), {
  rename: true,
  archive: true,
  forkWorktree: true,
});
assert.deepEqual(sessionMenuCapabilities(claude), {
  rename: true,
  archive: true,
  forkWorktree: false,
});
assert.equal(sessionMenuCapabilities(archivedCodex).forkWorktree, false);
assert.equal(isWorktreeForkBlockedByState("running"), true);
assert.equal(isWorktreeForkBlockedByState("interrupting"), true);
assert.equal(isWorktreeForkBlockedByState("idle"), false);

assert.equal(normalizeWorktreeForkName("  fix-login  "), "fix-login");
assert.equal(isWorktreeForkNameValid(""), false);
assert.equal(isWorktreeForkNameValid("   "), false);
assert.equal(isWorktreeForkNameValid("x".repeat(WORKTREE_FORK_NAME_MAX)), true);
assert.equal(isWorktreeForkNameValid("x".repeat(WORKTREE_FORK_NAME_MAX + 1)), false);

assert.deepEqual(
  makeForkSessionWorktreeCommand("codex-parent", "fix-login", "request-1", 123),
  {
    v: 5,
    type: "fork_session_worktree",
    session_id: "codex-parent",
    name: "fix-login",
    request_id: "request-1",
    ts: 123,
  },
);

const pending = { requestId: "request-1", parentSessionId: "codex-parent" };
assert.equal(matchesWorktreeForkRequest(pending, "request-1", "codex-parent"), true);
assert.equal(matchesWorktreeForkRequest(pending, "request-2", "codex-parent"), false);
assert.equal(matchesWorktreeForkRequest(pending, "request-1", "other-parent"), false);
assert.equal(matchesWorktreeForkRequest(pending, "request-1"), true);
assert.equal(matchesWorktreeForkRequest(null, "request-1"), false);
assert.equal(isTerminalWorktreeForkError("wrapper_offline"), false);
assert.equal(isTerminalWorktreeForkError("internal"), true);

console.log("session worktree tests passed");

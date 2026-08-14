import assert from "node:assert/strict";
import { createServer } from "vite";

import type { ServerEvent } from "../src/protocol.ts";
import {
  mergeInitialHistory,
  restoreCachedTurnDetails,
} from "../src/history-merge.ts";
import type { Block, Turn } from "../src/reducer.ts";

const blockIdentity = (block: Block): string => block.kind === "text"
  ? block.message_id
  : block.kind === "tool" ? block.tool_use_id : block.item_id;

const settledCachedProcess = restoreCachedTurnDetails([{
  id: "settled-cache-summary", prompt: "done already", done: true,
  detailEventCount: 1, detailLoaded: false, blocks: [],
}], [{
  id: "settled-cache-summary", prompt: "done already", done: true,
  blocks: [{
    kind: "process", item_id: "stale-open-cache-process",
    processKind: "command", phase: "start", status: "running",
    title: "old command", done: false,
  }],
}])[0];
assert.equal(settledCachedProcess.detailProjection?.blocks[0]?.done, true,
  "a completed summary closes a stale open process restored from IndexedDB");
assert.equal(
  settledCachedProcess.detailProjection?.blocks[0]?.kind === "process"
    ? settledCachedProcess.detailProjection.blocks[0].status : null,
  "succeeded",
  "a stale cached child cannot restart the working spark after refresh",
);

const interleavedLiveBlocks: Block[] = [
  { kind: "text", message_id: "ordered-comment-a", text: "A", done: true,
    channel: "commentary", liveOrder: 0 },
  { kind: "tool", message_id: "ordered-tool-envelope-a",
    tool_use_id: "ordered-tool-a", tool: "Read", input: {}, done: true,
    liveOrder: 1, result: { content: "live A", is_error: false } },
  { kind: "text", message_id: "ordered-comment-b", text: "B", done: true,
    channel: "commentary", liveOrder: 2 },
  { kind: "tool", message_id: "ordered-tool-envelope-b",
    tool_use_id: "ordered-tool-b", tool: "Bash", input: {}, done: false,
    liveOrder: 3 },
];
const interleavedSummaryTurn: Turn = {
  id: "interleaved-turn", prompt: "inspect", done: false,
  blocks: [interleavedLiveBlocks[0], interleavedLiveBlocks[2]].map(
    ({ liveOrder: _liveOrder, ...block }) => block as Block),
};
const interleavedLiveTurn: Turn = {
  ...interleavedSummaryTurn,
  blocks: interleavedLiveBlocks,
};
assert.deepEqual(
  mergeInitialHistory(
    [interleavedSummaryTurn],
    [interleavedLiveTurn],
    { preserveLiveTailOpen: true, reconcileReplayOrphans: true },
  )[0].blocks.map(blockIdentity),
  ["ordered-comment-a", "ordered-tool-a", "ordered-comment-b", "ordered-tool-b"],
  "a running Codex summary subset preserves the complete live chronology",
);

const sourceSuperset = mergeInitialHistory(
  [{
    ...interleavedSummaryTurn,
    blocks: [{ kind: "process", item_id: "source-only-process",
      processKind: "reasoning", phase: "end", status: "succeeded",
      title: "source only", done: true }, ...interleavedSummaryTurn.blocks],
  }],
  [interleavedLiveTurn],
  { preserveLiveTailOpen: true, reconcileReplayOrphans: true },
)[0];
assert.deepEqual(
  sourceSuperset.blocks.map(blockIdentity),
  ["source-only-process", "ordered-comment-a", "ordered-comment-b",
    "ordered-tool-a", "ordered-tool-b"],
  "a History superset remains the chronology authority",
);

const ambiguousLiveOrder = mergeInitialHistory(
  [interleavedSummaryTurn],
  [{ ...interleavedLiveTurn, blocks: interleavedLiveBlocks.map(
    (block) => ({ ...block, liveOrder: 0 })) }],
  { preserveLiveTailOpen: true, reconcileReplayOrphans: true },
)[0];
assert.deepEqual(
  ambiguousLiveOrder.blocks.map(blockIdentity),
  ["ordered-comment-a", "ordered-comment-b", "ordered-tool-a", "ordered-tool-b"],
  "duplicate live order fails closed to the History-first merge",
);

const duplicateHistoryIdentity = mergeInitialHistory(
  [{
    ...interleavedSummaryTurn,
    blocks: [interleavedSummaryTurn.blocks[0],
      { ...interleavedSummaryTurn.blocks[0] },
      interleavedSummaryTurn.blocks[1]],
  }],
  [interleavedLiveTurn],
  { preserveLiveTailOpen: true, reconcileReplayOrphans: true },
)[0];
assert.deepEqual(
  duplicateHistoryIdentity.blocks.map(blockIdentity),
  ["ordered-comment-a", "ordered-comment-a", "ordered-comment-b",
    "ordered-tool-a", "ordered-tool-b"],
  "a duplicate History block identity fails closed instead of disappearing during live-order repair",
);

const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const { createRuntime, initialState, reduce } =
    await reducerHarness.ssrLoadModule("/src/reducer.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 22, ts: 10, ...body,
  } as ServerEvent);
  const sid = "history-live-block-order";
  let state = {
    ...initialState,
    focusedSid: sid,
    sessions: [{
      session_id: sid,
      engine: "codex" as const,
      space: "code" as const,
    }],
    runtimes: { [sid]: createRuntime() },
  };

  for (const liveEvent of [
    event({ type: "state", sid, state: "running", seq: 1 }),
    event({ type: "user_msg", sid,
      msg_id: "ordered-live-turn", prompt: "inspect", seq: 2 }),
    event({ type: "assistant_msg_start", sid,
      message_id: "ordered-live-comment-a", channel: "commentary", seq: 3 }),
    event({ type: "delta", sid,
      message_id: "ordered-live-comment-a", channel: "commentary",
      text: "live A", seq: 4 }),
    event({ type: "assistant_msg_end", sid,
      message_id: "ordered-live-comment-a", channel: "commentary", seq: 5 }),
    event({ type: "tool_use", sid,
      message_id: "ordered-live-tool-envelope-a",
      tool_use_id: "ordered-live-tool-a", tool: "Read",
      input: { file_path: "/tmp/a" }, seq: 6 }),
    event({ type: "tool_result", sid,
      tool_use_id: "ordered-live-tool-a", content: "live result A",
      is_error: false, seq: 7 }),
    event({ type: "assistant_msg_start", sid,
      message_id: "ordered-live-comment-b", channel: "commentary", seq: 8 }),
    event({ type: "delta", sid,
      message_id: "ordered-live-comment-b", channel: "commentary",
      text: "live B", seq: 9 }),
    event({ type: "assistant_msg_end", sid,
      message_id: "ordered-live-comment-b", channel: "commentary", seq: 10 }),
    event({ type: "tool_use", sid,
      message_id: "ordered-live-tool-envelope-b",
      tool_use_id: "ordered-live-tool-b", tool: "Bash",
      input: { command: "pwd" }, seq: 11 }),
  ]) {
    state = reduce(state, { type: "event", event: liveEvent });
  }
  state = reduce(state, {
    type: "event",
    event: event({
      type: "history",
      sid,
      session_id: sid,
      revision: "ordered-live-r1",
      generation: "ordered-live-g1",
      build_seq: 1,
      live_seq: 11,
      detail: "summary",
      has_more: false,
      in_progress: true,
      events: [],
      turns: [{
        id: "ordered-live-turn",
        prompt: "inspect",
        done: false,
        blocks: [
          { kind: "text", message_id: "ordered-live-comment-a",
            channel: "commentary", text: "canonical A", done: true },
          { kind: "text", message_id: "ordered-live-comment-b",
            channel: "commentary", text: "canonical B", done: true },
        ],
      }],
    }),
  });

  const blocks: Block[] = state.runtimes[sid].turns[0].blocks;
  assert.deepEqual(
    blocks.map(blockIdentity),
    ["ordered-live-comment-a", "ordered-live-tool-a",
      "ordered-live-comment-b", "ordered-live-tool-b"],
    "the real History reducer preserves complete live tool chronology",
  );
  assert.equal(
    blocks[0].kind === "text" ? blocks[0].text : null,
    "canonical A",
    "chronology restoration must retain History text payload authority",
  );
  const toolResult = blocks[1].kind === "tool" ? blocks[1].result : null;
  assert.equal(toolResult?.content, "live result A",
    "chronology restoration must retain the live tool result");
  assert.equal(toolResult?.is_error, false,
    "chronology restoration must retain the live tool lifecycle");
} finally {
  await reducerHarness.close();
}

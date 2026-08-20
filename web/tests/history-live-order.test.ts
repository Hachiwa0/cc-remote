import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import type { ServerEvent } from "../src/protocol.ts";
import {
  mergeInitialHistory,
  restoreCachedTurnDetails,
  restoreObservedLiveTurnDetails,
} from "../src/history-merge.ts";
import type { Block, Turn } from "../src/reducer.ts";
import { reconcileProvenCompactionOrphans } from
  "../src/compaction-orphans.ts";

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

const cachedNeutralSteerPlan = restoreCachedTurnDetails([{
  id: "cached-neutral-steer", prompt: "continue", done: true,
  detailEventCount: 1, detailLoaded: false, blocks: [],
}], [{
  id: "cached-neutral-steer", prompt: "continue", done: true,
  blocks: [{
    kind: "process", item_id: "cached-neutral-plan",
    processKind: "plan", phase: "update", status: "running",
    title: "计划", plan: [
      { step: "inspect", status: "completed" },
      { step: "finish", status: "inProgress" },
    ], done: false,
  }],
}])[0];
const restoredNeutralPlan = cachedNeutralSteerPlan.detailProjection
  ?.blocks[0];
assert.equal(restoredNeutralPlan?.done, false,
  "IndexedDB reconciliation preserves a neutral-steer Plan until an exact terminal");
assert.equal(
  restoredNeutralPlan?.kind === "process" ? restoredNeutralPlan.status : null,
  "running",
  "cache paint cannot fabricate a terminal Plan status",
);

const compactionNativeId = "repeated-compaction-native-turn";
const compaction = (item_id: string): Block => ({
  kind: "process", item_id, processKind: "compaction", phase: "end",
  status: "succeeded", turn_id: compactionNativeId,
  title: "压缩上下文", done: true,
});
const compactionOwner: Turn = {
  id: "source-compaction-owner", forkPointId: compactionNativeId,
  prompt: "long task", done: true,
  blocks: [compaction("source-occurrence")],
};
const compactionOrphan = (
  id: string, itemIds: string[],
): Turn => ({
  id, prompt: "", done: true,
  blocks: itemIds.map(compaction),
});
assert.equal(reconcileProvenCompactionOrphans([
  compactionOwner,
  compactionOrphan("different-occurrence-row", ["different-occurrence"]),
]).length, 2,
"the same native task cannot prove two compactions are the same occurrence");
assert.equal(reconcileProvenCompactionOrphans([
  compactionOwner,
  compactionOrphan("exact-occurrence-row", ["source-occurrence"]),
]).length, 1,
"an exact compaction item duplicate remains safely removable");
assert.equal(reconcileProvenCompactionOrphans([
  compactionOwner,
  compactionOrphan("partially-matched-row", [
    "source-occurrence", "bounded-out-occurrence",
  ]),
]).length, 2,
"one item omitted by a bounded summary preserves the complete orphan row");

const staleObservedTurn: Turn = {
  id: "completed-cli-observed", prompt: "inspect", done: true,
  blocks: [{
    kind: "process", item_id: "replayed-cli-command",
    processKind: "command", phase: "start", status: "running",
    title: "retained command", output: "retained output", done: false,
  }],
};
const completedCliSummary: Turn = {
  id: staleObservedTurn.id, prompt: "inspect", done: true,
  blocks: [{ kind: "text", message_id: "completed-cli-final",
    channel: "final", text: "done", done: true }],
};
const preservedBackground = restoreObservedLiveTurnDetails(
  [completedCliSummary], [staleObservedTurn],
)[0];
assert.equal(preservedBackground.detailProjection?.blocks[0]?.done, false,
  "without an idle Codex fence genuine late activity stays live");
assert.equal(staleObservedTurn.blocks[0].done, false,
  "observed-detail restoration never mutates reducer input");

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

// A Wrapper generation can restart while the official Codex task keeps
// running in the shared app-server. The browser then owns the user-bound
// pre-restart row while the new generation briefly paints a prompt-less
// suffix. An early idle History fallback can terminalize that suffix before
// the final authoritative page arrives. Once every stable suffix block is
// uniquely owned by one completed native History row, the fallback row is a
// duplicate projection, not a second failed conversation turn.
const restartedNativeTurn = "wrapper-restart-native-turn";
const restartedSettledHistory: Turn = {
  id: "wrapper-restart-history-user",
  prompt: "部署一下",
  forkPointId: restartedNativeTurn,
  done: true,
  blocks: [{
    kind: "text",
    message_id: "wrapper-restart-before-message",
    channel: "commentary",
    text: "开始部署",
    done: true,
  }, {
    kind: "tool",
    message_id: "wrapper-restart-after-envelope",
    tool_use_id: "wrapper-restart-after-tool",
    tool: "shell",
    input: {},
    done: true,
    result: { content: "ok", is_error: false, status: "succeeded" },
  }, {
    kind: "text",
    message_id: "wrapper-restart-final-message",
    channel: "final",
    text: "部署成功",
    done: true,
  }],
};
const restartedTerminalProjection = mergeInitialHistory(
  [restartedSettledHistory],
  [{
    id: "wrapper-restart-history-user",
    prompt: "部署一下",
    forkPointId: restartedNativeTurn,
    done: false,
    blocks: [{
      kind: "text",
      message_id: "wrapper-restart-before-message",
      channel: "commentary",
      text: "开始部署",
      done: true,
    }],
  }, {
    id: "wrapper-restart-promptless-suffix",
    prompt: "",
    liveTaskId: restartedNativeTurn,
    done: true,
    interrupted: true,
    error: "会话已结束，但未收到完整的终止状态。",
    blocks: [{
      kind: "tool",
      message_id: "wrapper-restart-after-envelope",
      tool_use_id: "wrapper-restart-after-tool",
      tool: "shell",
      input: {},
      done: true,
      result: { content: "ok", is_error: true, status: "interrupted" },
    }],
  }],
  {
    newestHistoryId: "wrapper-restart-history-user",
    reconcileReplayOrphans: true,
  },
  true,
);
assert.deepEqual(
  restartedTerminalProjection.map((turn) => ({
    id: turn.id,
    done: turn.done,
    interrupted: turn.interrupted,
    error: turn.error,
    blockIds: turn.blocks.map(blockIdentity),
  })),
  [{
    id: "wrapper-restart-history-user",
    done: true,
    interrupted: undefined,
    error: undefined,
    blockIds: [
      "wrapper-restart-before-message",
      "wrapper-restart-after-tool",
      "wrapper-restart-final-message",
    ],
  }],
  "settled Codex History must absorb a terminalized restart suffix",
);

const repairedPersistedRestartId = mergeInitialHistory(
  [restartedSettledHistory],
  [{
    ...restartedSettledHistory,
    id: "persisted-wrapper-restart-orphan",
    historyTurnId: restartedSettledHistory.id,
    clientMsgId: undefined,
  }],
  { reconcileReplayOrphans: true },
  true,
)[0];
assert.deepEqual(
  {
    id: repairedPersistedRestartId.id,
    historyTurnId: repairedPersistedRestartId.historyTurnId,
  },
  { id: restartedSettledHistory.id, historyTurnId: undefined },
  "a previously cached unowned restart id self-heals to native History",
);

const preservedBrowserOwnedId = mergeInitialHistory(
  [restartedSettledHistory],
  [{
    ...restartedSettledHistory,
    id: "browser-owned-restart-row",
    clientMsgId: "browser-owned-restart-row",
    historyTurnId: restartedSettledHistory.id,
  }],
  { reconcileReplayOrphans: true },
  true,
)[0];
assert.deepEqual(
  {
    id: preservedBrowserOwnedId.id,
    clientMsgId: preservedBrowserOwnedId.clientMsgId,
    historyTurnId: preservedBrowserOwnedId.historyTurnId,
  },
  {
    id: "browser-owned-restart-row",
    clientMsgId: "browser-owned-restart-row",
    historyTurnId: restartedSettledHistory.id,
  },
  "a real browser-owned alias must keep its stable optimistic identity",
);

const uncertainRestartSuffix = mergeInitialHistory(
  [restartedSettledHistory],
  [{
    id: "uncertain-wrapper-restart-suffix",
    prompt: "",
    liveTaskId: restartedNativeTurn,
    done: true,
    interrupted: true,
    error: "会话已结束，但未收到完整的终止状态。",
    blocks: [{
      kind: "tool",
      message_id: "unmatched-restart-envelope",
      tool_use_id: "unmatched-restart-tool",
      tool: "shell",
      input: {},
      done: true,
    }],
  }],
  {
    newestHistoryId: restartedSettledHistory.id,
    reconcileReplayOrphans: true,
  },
  true,
);
assert.deepEqual(
  uncertainRestartSuffix.map((turn) => turn.id),
  [restartedSettledHistory.id, "uncertain-wrapper-restart-suffix"],
  "a restart suffix without unique native block proof must remain visible",
);

const deferredPlan: Block = {
  kind: "process", item_id: "deferred-session-plan", processKind: "plan",
  phase: "snapshot", status: "succeeded", title: "计划", done: true,
  plan: [{ step: "完成修复", status: "inProgress" }],
};
const deferredPlanSummary: Turn = {
  id: "native-plan-summary-turn", clientMsgId: "browser-plan-turn",
  prompt: "继续执行计划。", done: true,
  detailEventCount: 75, detailLoaded: false,
  blocks: [deferredPlan, {
    kind: "text", message_id: "plan-summary-final", channel: "final",
    text: "阶段工作已完成。", done: true,
  }],
};
const repairedPlanOnlyDeferredDetail = mergeInitialHistory(
  [deferredPlanSummary],
  [{
    ...deferredPlanSummary,
    id: "browser-plan-turn",
    historyTurnId: deferredPlanSummary.id,
    detailLoaded: true,
  }],
)[0];
assert.equal(repairedPlanOnlyDeferredDetail.detailLoaded, false,
  "an externalized Plan cannot suppress deferred process history");

const loadedPlanDetail = mergeInitialHistory(
  [{ ...deferredPlanSummary, id: "loaded-plan-detail-turn",
    clientMsgId: undefined }],
  [{
    ...deferredPlanSummary,
    id: "loaded-plan-detail-turn",
    clientMsgId: undefined,
    detailLoaded: true,
    detailProjection: {
      segments: [{
        pageKey: "loaded-plan-detail-page", before: null, events: [],
        hasMore: false, oldestCursor: null, hasNewer: false,
        newerCursor: null, encodedChars: 0,
      }],
      blocks: [{
        kind: "tool", message_id: "loaded-tool-message",
        tool_use_id: "loaded-tool", tool: "Read", input: {}, done: true,
      }],
      capped: false, hasMore: false, oldestCursor: null,
      hasNewer: false, newerCursor: null,
    },
  }],
)[0];
assert.equal(loadedPlanDetail.detailLoaded, true,
  "a real same-turn detail projection remains loaded across summary refresh");

const runningInlineDetail = mergeInitialHistory(
  [{ ...deferredPlanSummary, id: "running-inline-detail", done: false }],
  [{
    ...deferredPlanSummary,
    id: "running-inline-detail",
    done: false,
    detailLoaded: true,
    blocks: [deferredPlan, {
      kind: "tool", message_id: "running-tool-message",
      tool_use_id: "running-tool", tool: "Read", input: {}, done: false,
    }],
  }],
  { preserveLiveTailOpen: true },
)[0];
assert.equal(runningInlineDetail.detailLoaded, true,
  "a raced running summary keeps genuine inline process detail loaded");

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

  const idleSid = "codex-shared-cli-idle-projection";
  const idleRevision = "codex-shared-cli-idle-r1";
  const idleGeneration = "codex-shared-cli-idle-g1";
  const openObserved = (): Turn => ({
    id: "shared-cli-completed-row", prompt: "done already", done: true,
    detailEventCount: 1,
    blocks: [{ kind: "process", item_id: "shared-cli-replayed-process",
      processKind: "command", phase: "start", status: "running",
      title: "CLI replay", output: "keep this output", done: false }],
  });
  const summaryTurn = {
    id: "shared-cli-completed-row", prompt: "done already", done: true,
    detailEventCount: 1, detailLoaded: false,
    blocks: [{ kind: "text" as const, message_id: "shared-cli-final",
      channel: "final" as const, text: "complete", done: true }],
  };
  const idleHistory = event({
    type: "history", sid: idleSid, session_id: idleSid,
    revision: idleRevision, generation: idleGeneration,
    build_seq: 2, live_seq: 40, authoritative: true,
    detail: "summary", in_progress: false, has_more: false,
    newest_id: summaryTurn.id, events: [], turns: [summaryTurn],
  });
  let idleState = {
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: {
      [idleSid]: {
        ...createRuntime(), state: "idle" as const,
        historyRevision: idleRevision, historyGeneration: idleGeneration,
        lastLiveSeq: 40, liveDetailTurnIds: [summaryTurn.id],
        turns: [openObserved()],
      },
    },
  };
  idleState = reduce(idleState, { type: "event", event: idleHistory });
  const repaired = idleState.runtimes[idleSid].turns[0];
  const repairedProcess = repaired.detailProjection?.blocks.find(
    (block: Block) => block.kind === "process");
  assert.equal(idleState.runtimes[idleSid].state, "idle");
  assert.equal(repairedProcess?.done, true,
    "a passive shared CLI holder cannot reactivate completed Codex detail");
  assert.equal(repairedProcess?.kind === "process"
    ? repairedProcess.output : null, "keep this output",
  "idle History repair retains the collapsed process payload");
  const { ChatView } = await reducerHarness.ssrLoadModule(
    "/src/components/ChatView.tsx");
  const idleMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: idleSid, turns: [repaired], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {}, onLoadDetail: () => {},
  }));
  assert.doesNotMatch(idleMarkup, /class="turn-working"/,
    "a passive CLI holder cannot leave the working spark animated");
  assert.match(idleMarkup, /class="turn-done-mark"/,
    "the completed row returns to its static terminal spark");
  assert.match(idleMarkup, /aria-expanded="false"/,
    "the completed process disclosure stays settled instead of flapping open");

  const repairedPlanOnlyMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "repaired-plan-summary-session",
    turns: [repairedPlanOnlyDeferredDetail],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
    externalPlanProgress: {
      turnId: repairedPlanOnlyDeferredDetail.id,
      itemId: deferredPlan.kind === "process" ? deferredPlan.item_id : "",
    },
  }));
  assert.match(repairedPlanOnlyMarkup, /已处理/,
    "lifting a Plan into the session strip retains the process disclosure");
  assert.match(repairedPlanOnlyMarkup, /75 项/);

  const directAnswerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "direct-answer-session",
    turns: [{
      id: "direct-answer-turn", prompt: "你好", done: true,
      detailEventCount: 0, detailLoaded: false,
      blocks: [{
        kind: "text", message_id: "direct-answer-final", channel: "final",
        text: "你好。", done: true,
      }],
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(directAnswerMarkup, /已处理/,
    "a direct answer with no process remains free of a synthetic disclosure");

  const currentRunningState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "running" as const,
      historyRevision: idleRevision, historyGeneration: idleGeneration,
      lastLiveSeq: 40, liveDetailTurnIds: [summaryTurn.id],
      turns: [openObserved()],
    } },
  }, { type: "event", event: event({
    ...idleHistory,
    in_progress: true,
  }) });
  assert.equal(currentRunningState.runtimes[idleSid].turns[0]
    .detailProjection?.blocks.find(
      (block: Block) => block.kind === "process")?.done, false,
  "a currently running History page preserves genuine Codex process activity");

  const racedLiveState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "running" as const,
      historyRevision: idleRevision, historyGeneration: idleGeneration,
      lastLiveSeq: 41, liveDetailTurnIds: [summaryTurn.id],
      turns: [openObserved()],
    } },
  }, { type: "event", event: idleHistory });
  const racedTurn = racedLiveState.runtimes[idleSid].turns[0];
  const racedProcess = [
    ...(racedTurn.detailProjection?.blocks ?? []),
    ...(racedTurn.liveSpillBlocks ?? []),
    ...racedTurn.blocks,
  ].find((block: Block) => block.kind === "process");
  assert.equal(racedProcess?.done, false,
    "an older idle History page cannot settle a newer live process event");

  const lostTerminalSid = "codex-idle-history-recovery-source";
  const lostTerminalState = reduce({
    ...initialState,
    focusedSid: lostTerminalSid,
    sessions: [{ session_id: lostTerminalSid, engine: "codex" as const }],
    runtimes: { [lostTerminalSid]: {
      ...createRuntime(), state: "running" as const,
      turns: [{
        id: "promptless-wrapper-generation-tail", prompt: "", done: false,
        blocks: [{
          kind: "tool" as const,
          message_id: "promptless-wrapper-generation-envelope",
          tool_use_id: "promptless-wrapper-generation-tool",
          tool: "shell", input: {}, done: true,
          result: { content: "done", is_error: false },
        }],
      }],
    } },
  }, { type: "event", event: event({
    type: "history", sid: lostTerminalSid, session_id: lostTerminalSid,
    revision: "lost-terminal-r1", generation: "lost-terminal-g1",
    build_seq: 1, live_seq: 0, authoritative: true,
    detail: "summary", in_progress: false, has_more: false,
    events: [], turns: [],
  }) });
  assert.deepEqual(
    {
      done: lostTerminalState.runtimes[lostTerminalSid].turns[0].done,
      terminalSource:
        lostTerminalState.runtimes[lostTerminalSid].turns[0].terminalSource,
      error: lostTerminalState.runtimes[lostTerminalSid].turns[0].error,
    },
    {
      done: true,
      terminalSource: "idle_history_recovery",
      error: "会话已结束，但未收到完整的终止状态。",
    },
    "a new idle-History fallback carries a machine-readable recovery source",
  );

  const cachedInput = openObserved();
  const cachedState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: createRuntime() },
  }, {
    type: "hydrate_cache", sid: idleSid, turns: [cachedInput],
    revision: idleRevision, generation: idleGeneration,
  });
  assert.equal(cachedState.runtimes[idleSid].turns[0].blocks[0].done, true,
    "an IndexedDB paint cannot animate stale completed child activity");
  assert.equal(cachedInput.blocks[0].done, false,
    "cache hydration cannot mutate its action payload");

  const idleBoundaryState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "running" as const,
      turns: [openObserved()],
    } },
  }, { type: "event", event: event({
    type: "state", sid: idleSid, state: "idle", seq: 42,
  }) });
  assert.equal(idleBoundaryState.runtimes[idleSid].turns[0].blocks[0].done,
    true, "an exact Codex idle frame settles stale display-only activity");
  assert.equal(idleBoundaryState.runtimes[idleSid].turns[0].blocks[0].kind
    === "process"
    ? idleBoundaryState.runtimes[idleSid].turns[0].blocks[0].output : null,
  "keep this output", "the idle boundary preserves process payloads");

  const resumedBackgroundState = reduce(idleBoundaryState, {
    type: "event", event: event({
      type: "process", sid: idleSid, seq: 43,
      item_id: "shared-cli-replayed-process", kind: "command",
      phase: "update", status: "running", turn_id: summaryTurn.id,
      title: "real background work resumed",
    }),
  });
  assert.equal(resumedBackgroundState.runtimes[idleSid].turns[0]
    .blocks[0].done, false,
  "a later real process event can reopen its exact settled child");

  const runningBoundaryState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "idle" as const,
      turns: [openObserved()],
    } },
  }, { type: "event", event: event({
    type: "state", sid: idleSid, state: "running", seq: 42,
  }) });
  assert.equal(runningBoundaryState.runtimes[idleSid].turns[0].blocks[0].done,
    false, "a running frame cannot settle genuine Codex process activity");

  const claudeSid = "claude-background-after-terminal-history";
  let claudeState = {
    ...initialState,
    focusedSid: claudeSid,
    sessions: [{ session_id: claudeSid, engine: "claude" as const }],
    runtimes: { [claudeSid]: {
      ...createRuntime(), state: "idle" as const,
      historyRevision: "claude-background-r1",
      historyGeneration: "claude-background-g1", lastLiveSeq: 50,
      liveDetailTurnIds: ["claude-background-row"],
      turns: [{ id: "claude-background-row", prompt: "delegate", done: true,
        blocks: [{ kind: "process" as const,
          item_id: "claude-background-agent", processKind: "agent" as const,
          phase: "start" as const, status: "running" as const,
          title: "background", done: false }] }],
    } },
  };
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "history", sid: claudeSid, session_id: claudeSid,
    revision: "claude-background-r1", generation: "claude-background-g1",
    build_seq: 2, live_seq: 50, authoritative: true, detail: "summary",
    in_progress: false, has_more: false, newest_id: "claude-background-row",
    events: [], turns: [{ id: "claude-background-row", prompt: "delegate",
      done: true, detailEventCount: 1, detailLoaded: false, blocks: [] }],
  }) });
  assert.equal(claudeState.runtimes[claudeSid].turns[0]
    .detailProjection?.blocks[0]?.done, false,
  "Codex idle repair cannot close a genuine Claude background process");
} finally {
  await reducerHarness.close();
}

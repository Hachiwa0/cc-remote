import assert from "node:assert/strict";
import { createServer } from "vite";

import type { ProcessBlock, Turn } from "../src/domain/conversation.js";
import {
  latestPlanProgress,
  planProgressPresentation,
} from "../src/plan-progress.js";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.js";

const activePlanBlock: ProcessBlock = {
  kind: "process",
  item_id: "active-plan",
  processKind: "plan",
  phase: "update",
  status: "running",
  title: "计划",
  plan: [
    { step: "检查实现", status: "completed" },
    { step: "完成验证", status: "inProgress" },
  ],
  done: false,
};

const staleTerminalPlanOwner: Turn = {
  id: "stale-terminal-plan-turn",
  prompt: "执行但忘记更新计划",
  done: true,
  blocks: [{
    ...activePlanBlock,
    status: "succeeded",
    done: true,
  }],
};
const staleTerminalProgress = latestPlanProgress([staleTerminalPlanOwner]);
assert.ok(staleTerminalProgress);
assert.deepEqual(
  {
    stale: planProgressPresentation(staleTerminalProgress.block).stale,
    currentStep: planProgressPresentation(
      staleTerminalProgress.block).currentStep,
    stateLabel: planProgressPresentation(
      staleTerminalProgress.block).stateLabel,
  },
  {
    stale: true,
    currentStep: null,
    stateLabel: "本轮已结束，计划未更新",
  },
  "a successful terminal must not present the last stale step as active",
);

assert.equal(latestPlanProgress([staleTerminalPlanOwner, {
  id: "after-stale-terminal-plan",
  prompt: "开始新任务",
  done: false,
  blocks: [],
}]), null,
"the next user message retires a terminal plan whose steps were stale");

const recoveredTerminalPlan = latestPlanProgress([{
  ...staleTerminalPlanOwner,
  blocks: [{ ...activePlanBlock, done: true, status: "succeeded" }],
}]);
assert.equal(recoveredTerminalPlan?.block.done, true,
  "a durable snapshot retains its exact terminal without changing steps");
assert.equal(
  planProgressPresentation(recoveredTerminalPlan!.block).stale,
  true,
);

const steeredPlanOwner: Turn = {
  id: "steered-plan-turn",
  prompt: "执行任务",
  done: true,
  blocks: [{ ...activePlanBlock }],
};
const steeredPlan = latestPlanProgress([steeredPlanOwner, {
  id: "steer-follow-up",
  prompt: "补充要求",
  done: false,
  blocks: [],
}]);
assert.equal(steeredPlan?.turnId, "steered-plan-turn",
  "a neutral steer boundary keeps its open Plan across the clarification");
assert.equal(planProgressPresentation(steeredPlan!.block).currentStep,
  "完成验证");

const event = (body: Record<string, unknown>): ServerEvent => ({
  v: PROTOCOL_VERSION,
  ts: 1,
  ...body,
}) as ServerEvent;
const reducerSid = "steered-plan-reducer";
const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { createRuntime, initialState, reduce } =
    await reducerHarness.ssrLoadModule("/src/reducer.ts");
  let reducerState = reduce({
    ...initialState,
    focusedSid: reducerSid,
    runtimes: { [reducerSid]: createRuntime() },
  }, {
    type: "query_sent",
    sid: reducerSid,
    prompt: "执行任务",
    msg_id: "native-steered-plan",
    ts: 1_000,
  });
  reducerState = reduce(reducerState, { type: "event", event: event({
    type: "turn_plan",
    sid: reducerSid,
    item_id: "plan:native-steered-plan",
    turn_id: "native-steered-plan",
    explanation: null,
    plan: activePlanBlock.plan,
  }) });
  reducerState = reduce(reducerState, { type: "event", event: event({
    type: "turn_end",
    sid: reducerSid,
    turn_id: "native-steered-plan",
    result: { subtype: "steered", duration_ms: 0, is_error: false },
  }) });
  const reducerPlan = reducerState.runtimes[reducerSid].turns[0].blocks.find(
    (block: ProcessBlock) =>
      block.kind === "process" && block.processKind === "plan",
  );
  assert.equal(reducerPlan?.done, false,
    "a live neutral steer boundary must not settle the Plan block");
  reducerState = reduce(reducerState, { type: "event", event: event({
    type: "turn_end",
    sid: reducerSid,
    turn_id: "native-steered-plan",
    result: { subtype: "success", duration_ms: 1000, is_error: false },
  }) });
  const terminalReducerPlan = reducerState.runtimes[reducerSid].turns[0]
    .blocks.find((block: ProcessBlock) =>
      block.kind === "process" && block.processKind === "plan");
  assert.equal(terminalReducerPlan?.done, true,
    "the later exact terminal settles the same Plan block");
} finally {
  await reducerHarness.close();
}

const terminalPlanWithoutSteps: Turn = {
  id: "terminal-empty-plan",
  prompt: "执行无结构计划",
  done: true,
  blocks: [{
    ...activePlanBlock,
    item_id: "terminal-empty-plan-block",
    status: "succeeded",
    plan: [],
    done: true,
  }],
};
assert.equal(latestPlanProgress([terminalPlanWithoutSteps, {
  id: "after-terminal-empty-plan",
  prompt: "下一项任务",
  done: false,
  blocks: [],
}]), null, "a terminal plan without steps retires at the next user turn");

console.log("plan progress tests passed");

// Turn/block state model for the chat UI.
//
// A Turn = one user query + the assistant's response (which may span multiple
// assistant messages with tool calls). Blocks are keyed by message_id (text) or
// tool_use_id (tool). The reducer applies server events in arrival order; the
// wrapper's emit-lock guarantees replay batches arrive contiguously before live
// events, so no client-side reordering is needed.
import type { ConnState } from "./ws";
import type { ServerEvent, SessionInfo, State, ContextReport, QueryImg, QueryFile } from "./protocol";
import type { DiffLine, GitDiffSection } from "./diff";
import { parseGitDiff } from "./diff";
import { matchModelId } from "./data";

export interface TextBlock {
  kind: "text";
  message_id: string;
  text: string;
  done: boolean;
}
export interface ToolBlock {
  kind: "tool";
  message_id: string;
  tool_use_id: string;
  tool: string;
  input: Record<string, unknown>;
  result?: { content: string; is_error: boolean; truncated?: boolean | null };
  done: boolean;
}
export type Block = TextBlock | ToolBlock;

export interface Turn {
  id: string;
  prompt: string; // empty when we joined mid-turn (no user bubble rendered)
  blocks: Block[];
  done: boolean;
  interrupted?: boolean;
  error?: string;
  images?: QueryImg[]; // attached images on the user's message (originating client only)
  files?: QueryFile[]; // attached files (written to /tmp by wrapper, prompt gets @path)
  ts?: number; // send timestamp (ms) for the user-bubble time readout
  doneTs?: number; // turn-end timestamp (ms) for the AI reply time readout
}

export interface Artifact { file: string; kind: "diff" | "md" | "gitdiff"; diff?: DiffLine[]; content?: string; sections?: GitDiffSection[]; }

export interface AppState {
  turns: Turn[];
  state: State;
  connState: ConnState;
  ccSessionId?: string;
  replaying: boolean;
  truncated: boolean; // history may be incomplete
  banner?: string; // status line (machine offline, catching up, ...)
  // composer / client-side turn scheduling
  queue: string[]; // queued messages (drain on turn_end)
  pendingSend: string | null; // interrupt-and-send: send once state returns to idle
  sendMode: "interrupt" | "queue";
  wrapperOnline: boolean; // wrapper_disconnected -> false, wrapper_reconnected -> true
  model: string; // selected model id (UI; wired to backend in Phase 2)
  perm: string; // permission mode id (UI; Phase 2)
  sessions: SessionInfo[]; // sessions sidebar rows
  activeSessionId: string | null;
  contextReport: ContextReport | null; // /context result modal
  artifact: Artifact | null; // right-side diff/markdown panel for a changed file
}

export type Action =
  | { type: "event"; event: ServerEvent }
  | { type: "query_sent"; prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[]; ts: number }
  | { type: "conn"; connState: ConnState; detail?: string }
  | { type: "enqueue"; prompt: string }
  | { type: "dequeue_at"; i: number }
  | { type: "set_send_mode"; mode: "interrupt" | "queue" }
  | { type: "set_pending"; prompt: string }
  | { type: "clear_pending" }
  | { type: "set_model"; model: string }
  | { type: "set_perm"; perm: string }
  | { type: "set_context"; report: ContextReport }
  | { type: "clear_context" }
  | { type: "set_turns"; turns: Turn[] }
  | { type: "set_artifact"; artifact: Artifact }
  | { type: "clear_artifact" };

export const initialState: AppState = {
  turns: [],
  state: "idle",
  connState: "connecting",
  replaying: false,
  truncated: false,
  queue: [],
  pendingSend: null,
  sendMode: "interrupt",
  wrapperOnline: true,
  model: "claude-mythos-5",
  perm: "bypassPermissions",
  sessions: [],
  activeSessionId: null,
  contextReport: null,
  artifact: null,
};

function cloneTurns(turns: Turn[]): Turn[] {
  return turns.map((t) => ({ ...t, blocks: t.blocks.map((b) => ({ ...b })) }));
}

export function reduce(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "conn": {
      let banner = state.banner;
      if (action.connState === "connected") banner = undefined;
      else if (action.connState === "reconnecting") banner = action.detail || "reconnecting…";
      else if (action.connState === "connecting") banner = "connecting…";
      return { ...state, connState: action.connState, banner };
    }
    case "query_sent": {
      const turn: Turn = { id: action.msg_id, prompt: action.prompt, blocks: [], done: false, images: action.images, files: action.files, ts: action.ts };
      return { ...state, turns: [...state.turns, turn] };
    }
    case "enqueue":
      return { ...state, queue: [...state.queue, action.prompt] };
    case "dequeue_at":
      return { ...state, queue: state.queue.filter((_, i) => i !== action.i) };
    case "set_send_mode":
      return { ...state, sendMode: action.mode };
    case "set_pending":
      return { ...state, pendingSend: action.prompt };
    case "clear_pending":
      return { ...state, pendingSend: null };
    case "set_model":
      return { ...state, model: action.model };
    case "set_perm":
      return { ...state, perm: action.perm };
    case "set_turns":
      return { ...state, turns: action.turns };
    case "set_context":
      return { ...state, contextReport: action.report };
    case "clear_context":
      return { ...state, contextReport: null };
    case "set_artifact":
      return { ...state, artifact: action.artifact };
    case "clear_artifact":
      return { ...state, artifact: null };
    case "event": {
      return reduceEvent(state, action.event);
    }
  }
}

function reduceEvent(state: AppState, e: ServerEvent): AppState {
  switch (e.type) {
    case "snapshot":
      // First hello: just learn cc_session_id + state. The app reads its
      // IndexedDB cache and re-hellos with last_seq to fetch the delta.
      return { ...state, state: e.state, ccSessionId: e.cc_session_id ?? undefined };
    case "state":
      return { ...state, state: e.state };
    case "model":
      return { ...state, model: matchModelId(e.model) };
    case "perm":
      return { ...state, perm: e.mode };
    case "context_report":
      return { ...state, contextReport: e };
    case "diff_report":
      return { ...state, artifact: { file: e.file, kind: "gitdiff", sections: parseGitDiff(e.diff) } };
    case "session_list":
      return { ...state, sessions: e.sessions };
    case "session_switched":
      // new active session: clear turns, reset turn scheduling, drop queue
      return {
        ...state,
        turns: [],
        state: "idle",
        replaying: false,
        truncated: false,
        banner: undefined,
        queue: [],
        pendingSend: null,
        activeSessionId: e.session_id || state.activeSessionId,
      };
    case "replay_start":
      // truncated = the buffer evicted events the client's last_seq wanted, so
      // rebuild from the buffer (drop local turns). Otherwise incremental catch-up:
      // merge onto existing (possibly cached-from-IndexedDB) turns.
      return { ...state, replaying: true, truncated: e.truncated, turns: e.truncated ? [] : state.turns };
    case "replay_end":
      return { ...state, replaying: false, truncated: state.truncated || e.truncated };
    case "wrapper_disconnected":
      return { ...state, wrapperOnline: false, banner: "machine offline — waiting for reconnect" };
    case "wrapper_reconnected":
      // wrapper came back; its buffer survived, but we may have missed events
      // during the gap. The App re-hellos on this event to trigger replay.
      return { ...state, wrapperOnline: true, banner: undefined, state: e.state, ccSessionId: e.cc_session_id ?? undefined };
    case "error": {
      const turns = cloneTurns(state.turns);
      const t = turns[turns.length - 1];
      if (t && !t.done) t.error = `${e.code}: ${e.message}`;
      else turns.push({ id: `err-${Date.now()}`, prompt: "", blocks: [], done: true, error: `${e.code}: ${e.message}` });
      return { ...state, turns };
    }
    case "user_msg": {
      // Originating client already created the turn on send (dedup by msg_id);
      // other clients create it here so they see the prompt too.
      const turns = cloneTurns(state.turns);
      if (!turns.some((t) => t.id === e.msg_id)) {
        turns.push({ id: e.msg_id, prompt: e.prompt, blocks: [], done: false });
      }
      return { ...state, turns };
    }
    case "assistant_msg_start": {
      const turns = cloneTurns(state.turns);
      let t = turns[turns.length - 1];
      if (!t || t.done) {
        t = { id: e.message_id, prompt: "", blocks: [], done: false };
        turns.push(t);
      }
      if (!t.blocks.some((b) => b.kind === "text" && b.message_id === e.message_id)) {
        t.blocks.push({ kind: "text", message_id: e.message_id, text: "", done: false });
      }
      return { ...state, turns };
    }
    case "delta": {
      const turns = cloneTurns(state.turns);
      let t = turns[turns.length - 1];
      if (!t || t.done) {
        t = { id: e.message_id, prompt: "", blocks: [], done: false };
        turns.push(t);
      }
      let block = t.blocks.find((b) => b.kind === "text" && b.message_id === e.message_id) as TextBlock | undefined;
      if (!block) {
        block = { kind: "text", message_id: e.message_id, text: "", done: false };
        t.blocks.push(block);
      }
      block.text += e.text;
      return { ...state, turns };
    }
    case "tool_use": {
      const turns = cloneTurns(state.turns);
      let t = turns[turns.length - 1];
      if (!t || t.done) {
        t = { id: e.message_id, prompt: "", blocks: [], done: false };
        turns.push(t);
      }
      if (!t.blocks.some((b) => b.kind === "tool" && b.tool_use_id === e.tool_use_id)) {
        t.blocks.push({
          kind: "tool", message_id: e.message_id, tool_use_id: e.tool_use_id,
          tool: e.tool, input: e.input, done: false,
        });
      }
      return { ...state, turns };
    }
    case "tool_result": {
      const turns = cloneTurns(state.turns);
      for (const t of turns) {
        const b = t.blocks.find((b) => b.kind === "tool" && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
        if (b) {
          b.result = { content: e.content, is_error: e.is_error, truncated: e.truncated ?? undefined };
          b.done = true;
          break;
        }
      }
      return { ...state, turns };
    }
    case "assistant_msg_end": {
      const turns = cloneTurns(state.turns);
      for (const t of turns) {
        const b = t.blocks.find((b) => b.kind === "text" && b.message_id === e.message_id) as TextBlock | undefined;
        if (b) { b.done = true; break; }
      }
      return { ...state, turns };
    }
    case "turn_end": {
      const turns = cloneTurns(state.turns);
      const t = turns[turns.length - 1];
      if (t) {
        t.done = true;
        if (e.result.subtype === "error_during_execution") t.interrupted = true;
        if (t.ts && e.result.duration_ms) t.doneTs = t.ts + e.result.duration_ms;
      }
      return { ...state, turns, state: "idle" };
    }
    case "pong":
    case "hello":
      return state;
  }
}

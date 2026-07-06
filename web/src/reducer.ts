// Turn/block state model for the chat UI.
//
// A Turn = one user query + the assistant's response (which may span multiple
// assistant messages with tool calls). Blocks are keyed by message_id (text) or
// tool_use_id (tool). The reducer applies server events in arrival order; the
// wrapper's emit-lock guarantees replay batches arrive contiguously before live
// events, so no client-side reordering is needed.
import type { ConnState } from "./ws";
import type { ServerEvent, SessionInfo, State, ContextReport, QueryImg, QueryFile, DirEntry } from "./protocol";
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
  pendingQuestion: { ask_id: string; question: string; options: { label: string; ds?: string }[] } | null;
  // Current directory listing for the session-creation picker (null = closed).
  dirPicker: { path: string; parent: string | null; dirs: DirEntry[] } | null;
  // Active session's cwd (from Snapshot/SessionSwitched) — default for a new chat.
  currentCwd: string;
  // New-chat welcome page: non-null while picking a cwd / before the first message.
  newChat: { cwd: string } | null;
  // First message held until the freshly-created session's session_switched arrives;
  // switchTick bumps on every switch to wake the sender effect.
  pendingNewQuery: { prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[] } | null;
  switchTick: number;
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
  | { type: "clear_artifact" }
  | { type: "answer_question" } // dismiss the question card (answer sent)
  | { type: "enter_new_chat"; cwd: string }
  | { type: "set_new_chat_cwd"; cwd: string }
  | { type: "exit_new_chat" }
  | { type: "start_new_query"; prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[] }
  | { type: "clear_pending_new_query" };

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
  pendingQuestion: null,
  dirPicker: null,
  currentCwd: "",
  newChat: null,
  pendingNewQuery: null,
  switchTick: 0,
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
      // Originating-client turn creation. Dedup by msg_id: a late user_msg
      // echo or a re-render can fire this twice; don't create a duplicate turn.
      if (state.turns.some((t) => t.id === action.msg_id)) return state;
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
    case "answer_question":
      return { ...state, pendingQuestion: null };
    case "enter_new_chat":
      return { ...state, newChat: { cwd: action.cwd } };
    case "set_new_chat_cwd":
      return state.newChat ? { ...state, newChat: { cwd: action.cwd } } : state;
    case "exit_new_chat":
      return { ...state, newChat: null };
    case "start_new_query":
      return { ...state, newChat: null, pendingNewQuery: { prompt: action.prompt, msg_id: action.msg_id, images: action.images, files: action.files } };
    case "clear_pending_new_query":
      return { ...state, pendingNewQuery: null };
    case "event": {
      return reduceEvent(state, action.event);
    }
  }
}

function reduceEvent(state: AppState, e: ServerEvent): AppState {
  switch (e.type) {
    case "snapshot":
      // First hello: just learn cc_session_id + state + cwd. The app reads its
      // IndexedDB cache and re-hellos with last_seq to fetch the delta.
      return { ...state, state: e.state, ccSessionId: e.cc_session_id ?? undefined, currentCwd: e.cwd ?? state.currentCwd };
    case "state":
      return { ...state, state: e.state };
    case "model":
      return { ...state, model: matchModelId(e.model) };
    case "perm":
      return { ...state, perm: e.mode };
    case "context_report":
      return { ...state, contextReport: e };
    case "ask_user":
      return { ...state, pendingQuestion: { ask_id: e.ask_id, question: e.question, options: e.options } };
    case "diff_report":
      return { ...state, artifact: { file: e.file, kind: "gitdiff", sections: parseGitDiff(e.diff) } };
    case "session_list":
      return { ...state, sessions: e.sessions };
    case "dir_list":
      return { ...state, dirPicker: { path: e.path, parent: e.parent ?? null, dirs: e.dirs } };
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
        currentCwd: e.cwd ?? state.currentCwd,
        newChat: null,
        switchTick: state.switchTick + 1,
      };
    case "replay_start":
      // truncated = the buffer evicted events the client's last_seq wanted, so
      // rebuild from the buffer (drop local turns). rebuild = the client's
      // last_seq was from a previous wrapper lifetime (seq reset on restart);
      // also drop local turns, but it's NOT data loss so don't set truncated.
      // Otherwise incremental catch-up: merge onto existing turns.
      {
        const clear = e.truncated || !!e.rebuild;
        return { ...state, replaying: true, truncated: e.truncated, turns: clear ? [] : state.turns };
      }
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
      return { ...state, turns, pendingNewQuery: null };
    }
    case "user_msg": {
      // Originating client already created the turn on send (dedup by msg_id);
      // other clients create it here so they see the prompt too. If a race left
      // the turn with an empty prompt (assistant_msg_start beat query_sent),
      // fill it — the wrapper always echoes the prompt here. Carries images so
      // replay/other devices render the attachment.
      const turns = cloneTurns(state.turns);
      const existing = turns.find((t) => t.id === e.msg_id);
      const imgs = (e.images && e.images.length) ? e.images : undefined;
      if (existing) {
        if (!existing.prompt && e.prompt) existing.prompt = e.prompt;
        if (!existing.images && imgs) existing.images = imgs;
      } else {
        turns.push({ id: e.msg_id, prompt: e.prompt, images: imgs, blocks: [], done: false });
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

// Turn/block state model for the chat UI.
//
// Multi-session: AppState holds a `runtimes` map keyed by session id (or a
// wrapper-assigned temp key for a brand-new session until its real id is
// captured). Each SessionRuntime has its own turns/state/model/perm/queue/etc.
// `focusedSid` selects the viewed one. Switching sessions is a pure view change
// (session_focus) — background turns keep streaming into their own runtime.
//
// Inbound frames carry `sid`; narrative events route to runtimes[msg.sid]
// (unknown sid → drop; null sid → focused). Control frames (session_list,
// session_focus, wrapper_reconnected, diff_report, ...) are global.
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
  images?: QueryImg[];
  files?: QueryFile[];
  ts?: number;
  doneTs?: number;
}

export interface Artifact { file: string; kind: "diff" | "md" | "gitdiff"; diff?: DiffLine[]; content?: string; sections?: GitDiffSection[]; loading?: boolean; }

export interface SessionRuntime {
  turns: Turn[];
  state: State;
  model: string;
  effort: string;
  perm: string;
  replaying: boolean;
  truncated: boolean;
  // true while we've switched to a session but its history hasn't arrived yet
  // (no cache hit + waiting on the wrapper's cold spawn/replay) — drives a spinner.
  loading?: boolean;
  ccSessionId?: string;
  pendingQuestion: { ask_id: string; question: string; options: { label: string; ds?: string }[] } | null;
  contextReport: ContextReport | null;
  queue: string[];
  pendingSend: string | null;
}

export interface AppState {
  // connection / global UI
  connState: ConnState;
  wrapperOnline: boolean;
  banner?: string;
  artifact: Artifact | null;
  dirPicker: { path: string; parent: string | null; dirs: DirEntry[] } | null;
  currentCwd: string;
  sendMode: "interrupt" | "queue";
  // new-chat welcome page (global; only one new-chat flow at a time)
  newChat: { cwd: string } | null;
  pendingNewQuery: { prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[] } | null;
  switchTick: number;
  // sessions + multi-session runtimes
  sessions: SessionInfo[];
  focusedSid: string | null;
  runtimes: Record<string, SessionRuntime>;
}

export function createRuntime(): SessionRuntime {
  return {
    turns: [], state: "idle", model: "claude-mythos-5", effort: "max", perm: "bypassPermissions",
    replaying: false, truncated: false, pendingQuestion: null, contextReport: null,
    queue: [], pendingSend: null,
  };
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
  | { type: "set_effort"; effort: string }
  | { type: "set_perm"; perm: string }
  | { type: "set_context"; report: ContextReport }
  | { type: "clear_context" }
  | { type: "set_turns"; sid: string; turns: Turn[] }
  | { type: "set_artifact"; artifact: Artifact }
  | { type: "open_artifact_loading"; file: string }
  | { type: "clear_artifact" }
  | { type: "focus_session"; sid: string }
  | { type: "set_session_tag"; sid: string; tag: string | null }
  | { type: "hydrate_cache"; sid: string; turns: Turn[] }
  | { type: "answer_question" }
  | { type: "enter_new_chat"; cwd: string }
  | { type: "set_new_chat_cwd"; cwd: string }
  | { type: "exit_new_chat" }
  | { type: "start_new_query"; prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[] }
  | { type: "clear_pending_new_query" };

export const initialState: AppState = {
  connState: "connecting",
  wrapperOnline: true,
  artifact: null,
  dirPicker: null,
  currentCwd: "",
  sendMode: "interrupt",
  newChat: null,
  pendingNewQuery: null,
  switchTick: 0,
  sessions: [],
  focusedSid: null,
  runtimes: {},
};

function cloneTurns(turns: Turn[]): Turn[] {
  return turns.map((t) => ({ ...t, blocks: t.blocks.map((b) => ({ ...b })) }));
}

// Patch a runtime by sid (explicit sid wins; null/undefined → focused). `create`
// creates the runtime if missing (used by snapshot for a session we haven't
// seen). Unknown sid with create=false → no-op (drop the frame: it's for a
// non-resident session the client doesn't track yet).
function patch(state: AppState, sid: string | null | undefined,
               fn: (rt: SessionRuntime) => void, create = false): AppState {
  const key = sid ?? state.focusedSid;
  if (!key) return state;
  let rt = state.runtimes[key];
  if (!rt) {
    if (!create) return state;
    rt = createRuntime();
  } else {
    rt = { ...rt };
  }
  fn(rt);
  return { ...state, runtimes: { ...state.runtimes, [key]: rt } };
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
    case "query_sent":
      return patch(state, state.focusedSid, (rt) => {
        if (rt.turns.some((t) => t.id === action.msg_id)) return;
        rt.turns = [...rt.turns, { id: action.msg_id, prompt: action.prompt, blocks: [], done: false, images: action.images, files: action.files, ts: action.ts }];
      });
    case "enqueue":
      return patch(state, state.focusedSid, (rt) => { rt.queue = [...rt.queue, action.prompt]; });
    case "dequeue_at":
      return patch(state, state.focusedSid, (rt) => { rt.queue = rt.queue.filter((_, i) => i !== action.i); });
    case "set_send_mode":
      return { ...state, sendMode: action.mode };
    case "set_pending":
      return patch(state, state.focusedSid, (rt) => { rt.pendingSend = action.prompt; });
    case "clear_pending":
      return patch(state, state.focusedSid, (rt) => { rt.pendingSend = null; });
    case "set_model":
      return patch(state, state.focusedSid, (rt) => { rt.model = action.model; });
    case "set_effort":
      return patch(state, state.focusedSid, (rt) => { rt.effort = action.effort; });
    case "set_perm":
      return patch(state, state.focusedSid, (rt) => { rt.perm = action.perm; });
    case "set_turns":
      return patch(state, action.sid, (rt) => { rt.turns = action.turns; }, true);
    case "set_context":
      return patch(state, state.focusedSid, (rt) => { rt.contextReport = action.report; });
    case "clear_context":
      return patch(state, state.focusedSid, (rt) => { rt.contextReport = null; });
    case "set_artifact":
      return { ...state, artifact: action.artifact };
    case "open_artifact_loading":
      // optimistic: show the diff panel (with a spinner) instantly on click; the
      // diff_report event replaces it with the real sections when it arrives.
      return { ...state, artifact: { file: action.file, kind: "gitdiff", sections: [], loading: true } };
    case "clear_artifact":
      return { ...state, artifact: null };
    case "focus_session": {
      // optimistic view switch: focus the session locally right away (its runtime
      // is usually already in memory) instead of waiting for the round-trip
      // session_focus. The server's session_focus later just re-confirms.
      const sid = action.sid;
      const rt = state.runtimes[sid] ?? createRuntime();
      // if we have no turns yet, mark loading so the UI shows a spinner (not the
      // empty "send a message" prompt) until cache-hydrate or the wrapper replay lands.
      const runtimes = { ...state.runtimes, [sid]: { ...rt, loading: rt.turns.length === 0 } };
      return { ...state, focusedSid: sid, runtimes };
    }
    case "set_session_tag":
      // optimistic archive/unarchive: flip the tag locally right away so the card
      // moves instantly, even if the archive_session round-trip is slow or a
      // connection blip drops it. The server's next SessionList reconciles.
      return { ...state, sessions: state.sessions.map((s) => s.session_id === action.sid ? { ...s, tag: action.tag } : s) };
    case "hydrate_cache":
      // fill a session's turns from the IndexedDB cache for an INSTANT render;
      // only if still empty (never clobber live/streaming or already-replayed turns).
      return patch(state, action.sid, (rt) => {
        if (rt.turns.length === 0 && action.turns.length) rt.turns = action.turns;
        rt.loading = false;
      }, true);
    case "answer_question":
      return patch(state, state.focusedSid, (rt) => { rt.pendingQuestion = null; });
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
    case "event":
      return reduceEvent(state, action.event);
  }
}

function reduceEvent(state: AppState, e: ServerEvent): AppState {
  switch (e.type) {
    case "snapshot": {
      // Per-session: the frame's sid is the runtime key; cc_session_id is the
      // real cc id (may still be null while a brand-new session's id is captured).
      const key = e.sid ?? e.cc_session_id ?? state.focusedSid;
      if (!key) return state;
      // First snapshot on a fresh connect focuses that session so the UI shows
      // something; a later session_focus (or the user picking one) overrides it.
      const focusedSid = state.focusedSid ?? key;
      return { ...patch(state, key, (rt) => {
        rt.state = e.state;
        rt.ccSessionId = e.cc_session_id ?? rt.ccSessionId;
      }, true), focusedSid };
    }
    case "session_focus": {
      // NON-destructive, focus-ONLY view change. Runtime key migration on
      // id-capture is handled by session_rekey — keeping it out of here is what
      // stops a background session's id-capture from stealing the user's view.
      const newF = e.session_id;
      // switch confirmed by the wrapper → stop the loading spinner. Essential for
      // a RESIDENT session with no replay (e.g. one that only ran /theme and has
      // no history) — otherwise it'd spin until the 6s fallback.
      const base = state.runtimes[newF] ?? createRuntime();
      const runtimes = { ...state.runtimes, [newF]: { ...base, loading: false } };
      return { ...state, focusedSid: newF, runtimes, currentCwd: e.cwd ?? state.currentCwd, switchTick: state.switchTick + 1 };
    }
    case "session_rekey": {
      // A temp-keyed new session captured its real cc id. Rename the runtime
      // old_key -> session_id; focus follows ONLY if we were viewing old_key
      // (so a BACKGROUND new session's capture never yanks the current view).
      const { old_key, session_id } = e;
      if (old_key === session_id) return state;
      const runtimes = { ...state.runtimes };
      if (runtimes[old_key]) {
        if (!runtimes[session_id]) runtimes[session_id] = runtimes[old_key];
        delete runtimes[old_key];
      } else if (!runtimes[session_id]) {
        runtimes[session_id] = createRuntime();
      }
      const wasFocused = state.focusedSid === old_key;
      return {
        ...state,
        runtimes,
        focusedSid: wasFocused ? session_id : state.focusedSid,
        currentCwd: wasFocused && e.cwd ? e.cwd : state.currentCwd,
      };
    }
    case "session_list":
      return { ...state, sessions: e.sessions };
    case "dir_list":
      return { ...state, dirPicker: { path: e.path, parent: e.parent ?? null, dirs: e.dirs } };
    case "wrapper_disconnected":
      return { ...state, wrapperOnline: false, banner: "machine offline — waiting for reconnect" };
    case "wrapper_reconnected":
      return { ...state, wrapperOnline: true, banner: undefined };
    case "diff_report":
      return { ...state, artifact: { file: e.file, kind: "gitdiff", sections: parseGitDiff(e.diff) } };
    case "state":
      return patch(state, e.sid, (rt) => { rt.state = e.state; });
    case "model":
      return patch(state, e.sid, (rt) => { rt.model = matchModelId(e.model); });
    case "effort":
      return patch(state, e.sid, (rt) => { rt.effort = e.effort; });
    case "perm":
      return patch(state, e.sid, (rt) => { rt.perm = e.mode; });
    case "context_report":
      return patch(state, e.sid, (rt) => { rt.contextReport = e; });
    case "ask_user":
      return patch(state, e.sid, (rt) => { rt.pendingQuestion = { ask_id: e.ask_id, question: e.question, options: e.options }; });
    case "replay_start":
      return patch(state, e.sid, (rt) => {
        rt.replaying = true;
        rt.truncated = e.truncated;
        // rebuild clears turns then refills — keep loading=true so the gap shows a
        // spinner rather than briefly flashing the empty "send a message" prompt.
        if (e.truncated || !!e.rebuild) { rt.turns = []; rt.loading = true; }
      });
    case "replay_end":
      return patch(state, e.sid, (rt) => { rt.replaying = false; rt.truncated = rt.truncated || e.truncated; rt.loading = false; });
    case "error":
      return { ...patch(state, e.sid, (rt) => {
        rt.loading = false; // never leave a spinner spinning behind an error
        const turns = cloneTurns(rt.turns);
        const t = turns[turns.length - 1];
        if (t && !t.done) t.error = `${e.code}: ${e.message}`;
        else turns.push({ id: `err-${Date.now()}`, prompt: "", blocks: [], done: true, error: `${e.code}: ${e.message}` });
        rt.turns = turns;
      }), pendingNewQuery: null };
    case "user_msg":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const existing = turns.find((t) => t.id === e.msg_id);
        const imgs = (e.images && e.images.length) ? e.images : undefined;
        // server ts is seconds -> ms; keep any optimistic client ts already set.
        const stamp = e.ts ? Math.round(e.ts * 1000) : undefined;
        if (existing) {
          if (!existing.prompt && e.prompt) existing.prompt = e.prompt;
          if (!existing.images && imgs) existing.images = imgs;
          if (!existing.ts && stamp) existing.ts = stamp;
        } else {
          turns.push({ id: e.msg_id, prompt: e.prompt, images: imgs, blocks: [], done: false, ts: stamp });
        }
        rt.turns = turns;
      });
    case "assistant_msg_start":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = turns[turns.length - 1];
        if (!t || t.done) { t = { id: e.message_id, prompt: "", blocks: [], done: false }; turns.push(t); }
        if (!t.blocks.some((b) => b.kind === "text" && b.message_id === e.message_id))
          t.blocks.push({ kind: "text", message_id: e.message_id, text: "", done: false });
        rt.turns = turns;
      });
    case "delta":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = turns[turns.length - 1];
        if (!t || t.done) { t = { id: e.message_id, prompt: "", blocks: [], done: false }; turns.push(t); }
        let block = t.blocks.find((b) => b.kind === "text" && b.message_id === e.message_id) as TextBlock | undefined;
        if (!block) { block = { kind: "text", message_id: e.message_id, text: "", done: false }; t.blocks.push(block); }
        block.text += e.text;
        rt.turns = turns;
      });
    case "tool_use":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = turns[turns.length - 1];
        if (!t || t.done) { t = { id: e.message_id, prompt: "", blocks: [], done: false }; turns.push(t); }
        if (!t.blocks.some((b) => b.kind === "tool" && b.tool_use_id === e.tool_use_id))
          t.blocks.push({ kind: "tool", message_id: e.message_id, tool_use_id: e.tool_use_id, tool: e.tool, input: e.input, done: false });
        rt.turns = turns;
      });
    case "tool_result":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        for (const t of turns) {
          const b = t.blocks.find((b) => b.kind === "tool" && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
          if (b) { b.result = { content: e.content, is_error: e.is_error, truncated: e.truncated ?? undefined }; b.done = true; break; }
        }
        rt.turns = turns;
      });
    case "assistant_msg_end":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        for (const t of turns) {
          const b = t.blocks.find((b) => b.kind === "text" && b.message_id === e.message_id) as TextBlock | undefined;
          if (b) { b.done = true; break; }
        }
        rt.turns = turns;
      });
    case "turn_end":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const t = turns[turns.length - 1];
        if (t) {
          t.done = true;
          if (e.result.subtype === "error_during_execution") t.interrupted = true;
          // Stamp completion time from the event's own server ts (seconds -> ms).
          // Robust for BOTH live turns and replayed history: the old
          // `t.ts + duration_ms` reconstruction dropped the timestamp for any turn
          // without a client-side start time (i.e. everything after a refresh,
          // where turns come from history replay). Fall back to start time, then now.
          t.doneTs = e.ts ? Math.round(e.ts * 1000) : (t.ts || Date.now());
        }
        rt.turns = turns;
        rt.state = "idle";
      });
    case "pong":
    case "hello":
      return state;
  }
}

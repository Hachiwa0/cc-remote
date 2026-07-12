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
import type { ServerEvent, SessionInfo, State, ContextReport, StatusReport, ThreadGoal, QueryImg, QueryFile, DirEntry } from "./protocol";
import type { Catalog } from "./data";
import type { DiffLine, GitDiffSection } from "./diff";
import { parseGitDiff } from "./diff";
import { matchModelId } from "./data";
import { canEnqueueQuery, collectWaitingQueries, reduceTargetedRuntime } from "./runtime-drain";
import { mergeInitialHistory } from "./history-merge";
import { boundRuntimeTurns, pruneRuntimeMap } from "./runtime-bounds";

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
  // Codex app-server turn id. Unlike the client message id, this is the
  // authoritative branch point accepted by thread/fork.
  codexTurnId?: string;
  prompt: string; // empty when we joined mid-turn (no user bubble rendered)
  blocks: Block[];
  done: boolean;
  interrupted?: boolean;
  error?: string;
  progress?: string;
  images?: QueryImg[];
  files?: QueryFile[];
  ts?: number;
  doneTs?: number;
}

export interface PendingQuery {
  prompt: string;
  images?: QueryImg[];
  files?: QueryFile[];
}

export interface Artifact { file: string; kind: "diff" | "md" | "gitdiff"; sid?: string | null; diff?: DiffLine[]; content?: string; sections?: GitDiffSection[]; loading?: boolean; }

export interface SessionRuntime {
  turns: Turn[];
  state: State;
  model: string;
  effort: string;
  perm: string;
  fast: boolean;   // codex Fast-mode (service tier) on/off
  replaying: boolean;
  // True only after this connection has received this sid's Snapshot or
  // ReplayEnd. Prevents stale local "idle" state from draining work early.
  syncReady: boolean;
  truncated: boolean;
  // true while we've switched to a session but its history hasn't arrived yet
  // (no cache hit + waiting on the wrapper's cold spawn/replay) — drives a spinner.
  loading?: boolean;
  // pagination: older turns exist beyond what's loaded, and the oldest loaded
  // turn id — the cursor the "load more" button pages back from.
  hasMore?: boolean;
  oldestId?: string | null;
  // true => an external process (native `claude`/`codex` in the terminal) owns this
  // session and is writing its transcript; we mirror it read-only.
  external?: boolean;
  takeoverPending: boolean;
  takeoverMessage: string | null;
  ccSessionId?: string;
  pendingQuestion: { ask_id: string; header?: string | null; question: string; options: { label: string; ds?: string }[]; allow_text?: boolean; secret?: boolean } | null;
  contextReport: ContextReport | null;
  goal: ThreadGoal | null;
  statusReport: StatusReport | null;
  queue: PendingQuery[];
  pendingSend: PendingQuery | null;
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
  // new-chat welcome page (global; only one new-chat flow at a time). model/effort
  // are the pre-selected values (null = use the wrapper's engine default).
  newChat: { cwd: string; model: string | null; effort: string | null } | null;
  // sessions + multi-session runtimes
  sessions: SessionInfo[];
  focusedSid: string | null;
  runtimes: Record<string, SessionRuntime>;
  // /btw ephemeral side-fork: the fork's routing key (its runtime lives in
  // `runtimes[btwSid]`) + engine, or null when no side panel is open.
  btwSid: string | null;
  btwEngine?: string;
  // Model catalogs the ENGINE reported (codex only). Absent until the `models`
  // frame lands; data.ts falls back to its static table until then.
  catalog: Catalog;
  // engine -> the model a NEW session starts on (codex config.toml's `model`).
  // Never the focused session's model — that one is per-session.
  catalogDefault: Record<string, string>;
}

export function createRuntime(): SessionRuntime {
  return {
    turns: [], state: "idle", model: "claude-mythos-5", effort: "max", perm: "bypassPermissions",
    fast: false,
    takeoverPending: false, takeoverMessage: null,
    replaying: false, syncReady: false, truncated: false,
    pendingQuestion: null, contextReport: null, goal: null, statusReport: null,
    queue: [], pendingSend: null,
  };
}

export type Action =
  | { type: "reset" }
  | { type: "event"; event: ServerEvent }
  | { type: "query_sent"; sid: string; prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[]; ts: number }
  | { type: "conn"; connState: ConnState; detail?: string }
  | { type: "command_error"; detail: string }
  | { type: "enqueue"; query: PendingQuery }
  | { type: "dequeue_at"; sid: string; i: number }
  | { type: "set_send_mode"; mode: "interrupt" | "queue" }
  | { type: "set_pending"; query: PendingQuery }
  | { type: "clear_pending"; sid: string }
  | { type: "set_model"; model: string }
  | { type: "set_effort"; effort: string }
  | { type: "set_perm"; perm: string }
  | { type: "set_context"; report: ContextReport }
  | { type: "clear_context" }
  | { type: "set_turns"; sid: string; turns: Turn[] }
  | { type: "set_artifact"; artifact: Artifact }
  | { type: "open_artifact_loading"; file: string; sid: string | null }
  | { type: "clear_artifact" }
  | { type: "clear_btw" }
  | { type: "focus_session"; sid: string }
  | { type: "hydrate_cache"; sid: string; turns: Turn[] }
  | { type: "prune_runtimes"; protectedSids: string[] }
  | { type: "answer_question" }
  | { type: "enter_new_chat"; cwd: string; model?: string | null; effort?: string | null }
  | { type: "set_new_chat_cwd"; cwd: string }
  | { type: "set_new_chat_model"; model: string | null }
  | { type: "set_new_chat_effort"; effort: string | null }
  | { type: "exit_new_chat" };

export const initialState: AppState = {
  connState: "connecting",
  // Require a wrapper-originated frame before draining queued work. A relay
  // socket can be connected while the machine-side wrapper is still offline.
  wrapperOnline: false,
  artifact: null,
  dirPicker: null,
  currentCwd: "",
  sendMode: "interrupt",
  newChat: null,
  sessions: [],
  focusedSid: null,
  runtimes: {},
  btwSid: null,
  catalog: {},
  catalogDefault: {},
};

function cloneTurns(turns: Turn[]): Turn[] {
  return turns.map((t) => ({ ...t, blocks: t.blocks.map((b) => ({ ...b })) }));
}

function replaceWithBoundedTurns(runtime: SessionRuntime, turns: Turn[]): void {
  const bounded = boundRuntimeTurns(turns);
  if (bounded.length < turns.length) {
    runtime.truncated = true;
    runtime.hasMore = false;
    runtime.oldestId = bounded[0]?.id ?? null;
  }
  runtime.turns = bounded;
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
    case "reset":
      return {
        ...initialState,
        sessions: [], runtimes: {}, artifact: null, dirPicker: null,
        newChat: null, btwSid: null, catalog: {}, catalogDefault: {},
      };
    case "conn": {
      let banner = state.banner;
      if (action.connState === "connected") banner = undefined;
      else if (action.connState === "reconnecting") banner = action.detail || "reconnecting…";
      else if (action.connState === "connecting") banner = "connecting…";
      const runtimes = action.connState === "connected"
        ? state.runtimes
        : Object.fromEntries(Object.entries(state.runtimes).map(
            ([sid, runtime]) => [sid, {
              ...runtime, syncReady: false, replaying: false,
            }]));
      return {
        ...state,
        runtimes,
        connState: action.connState,
        // A reconnect may land on a restarted relay with no wrapper. Wait for
        // replay/snapshot proof before allowing background queue removal.
        wrapperOnline: action.connState === "connected" ? state.wrapperOnline : false,
        banner,
      };
    }
    case "command_error":
      return { ...state, banner: action.detail };
    case "query_sent": {
      const turn: Turn = {
        id: action.msg_id, prompt: action.prompt, blocks: [], done: false,
        images: action.images,
        files: action.files?.map((file) => ({ filename: file.filename, data: "" })),
        ts: action.ts,
      };
      const runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "query_sent", turn });
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "enqueue": {
      const allQueued = collectWaitingQueries(state.runtimes);
      if (!canEnqueueQuery(allQueued, action.query)) return state;
      return patch(state, state.focusedSid, (rt) => {
        rt.queue = [...rt.queue, action.query];
      });
    }
    case "dequeue_at": {
      const runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "dequeue_at", i: action.i });
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "set_send_mode":
      return { ...state, sendMode: action.mode };
    case "set_pending": {
      const waiting = collectWaitingQueries(state.runtimes, state.focusedSid);
      if (!canEnqueueQuery(waiting, action.query)) return state;
      return patch(state, state.focusedSid, (rt) => { rt.pendingSend = action.query; });
    }
    case "clear_pending": {
      const runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "clear_pending" });
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "set_model":
      return patch(state, state.focusedSid, (rt) => { rt.model = action.model; });
    case "set_effort":
      return patch(state, state.focusedSid, (rt) => { rt.effort = action.effort; });
    case "set_perm":
      return patch(state, state.focusedSid, (rt) => { rt.perm = action.perm; });
    case "set_turns":
      return patch(state, action.sid, (rt) => {
        replaceWithBoundedTurns(rt, action.turns);
      }, true);
    case "set_context":
      return patch(state, state.focusedSid, (rt) => { rt.contextReport = action.report; });
    case "clear_context":
      return patch(state, state.focusedSid, (rt) => { rt.contextReport = null; });
    case "set_artifact":
      return { ...state, artifact: action.artifact };
    case "open_artifact_loading":
      // optimistic: show the diff panel (with a spinner) instantly on click; the
      // diff_report event replaces it with the real sections when it arrives.
      return { ...state, artifact: { file: action.file, sid: action.sid, kind: "gitdiff", sections: [], loading: true } };
    case "clear_artifact":
      return { ...state, artifact: null };
    case "clear_btw": {
      if (!state.btwSid) return state;
      const runtimes = { ...state.runtimes };
      delete runtimes[state.btwSid];   // ephemeral: drop the fork's runtime
      return { ...state, btwSid: null, btwEngine: undefined, runtimes };
    }
    case "focus_session": {
      // optimistic view switch: focus the session locally right away (its runtime
      // is usually already in memory) instead of waiting for the round-trip
      // session_focus. The server's session_focus later just re-confirms.
      const sid = action.sid;
      const rt = state.runtimes[sid] ?? createRuntime();
      // if we have no turns yet, mark loading so the UI shows a spinner (not the
      // empty "send a message" prompt) until cache-hydrate or the wrapper replay lands.
      const runtimes = { ...state.runtimes, [sid]: { ...rt, loading: rt.turns.length === 0 } };
      return { ...state, focusedSid: sid, runtimes, artifact: null };
    }
    case "hydrate_cache":
      // fill a session's turns from the IndexedDB cache for an INSTANT render;
      // only if still empty (never clobber live/streaming or already-replayed turns).
      return patch(state, action.sid, (rt) => {
        if (rt.turns.length === 0 && action.turns.length) {
          replaceWithBoundedTurns(rt, action.turns);
        }
        rt.loading = false;
      }, true);
    case "prune_runtimes": {
      const protectedSids = new Set(action.protectedSids);
      if (state.focusedSid) protectedSids.add(state.focusedSid);
      if (state.btwSid) protectedSids.add(state.btwSid);
      if (state.artifact?.sid) protectedSids.add(state.artifact.sid);
      const runtimes = pruneRuntimeMap(state.runtimes, protectedSids);
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "answer_question":
      return patch(state, state.focusedSid, (rt) => { rt.pendingQuestion = null; });
    case "enter_new_chat":
      return { ...state, newChat: { cwd: action.cwd, model: action.model ?? null, effort: action.effort ?? null } };
    case "set_new_chat_cwd":
      return state.newChat ? { ...state, newChat: { ...state.newChat, cwd: action.cwd } } : state;
    case "set_new_chat_model":
      return state.newChat ? { ...state, newChat: { ...state.newChat, model: action.model } } : state;
    case "set_new_chat_effort":
      return state.newChat ? { ...state, newChat: { ...state.newChat, effort: action.effort } } : state;
    case "exit_new_chat":
      return { ...state, newChat: null };
    case "event":
      return reduceEvent(state, action.event);
  }
}

function reduceEvent(
  state: AppState, e: ServerEvent, boundCompletedTurns = true,
): AppState {
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
        rt.syncReady = true;
        rt.ccSessionId = e.cc_session_id ?? rt.ccSessionId;
      }, true), focusedSid, wrapperOnline: true };
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
      const runtimes = {
        ...state.runtimes,
        [newF]: { ...base, loading: false, syncReady: true },
      };
      return {
        ...state, focusedSid: newF, runtimes,
        artifact: state.focusedSid && state.focusedSid !== newF ? null : state.artifact,
        currentCwd: e.cwd ?? state.currentCwd,
      };
    }
    case "session_rekey": {
      // A temp-keyed new session captured its real cc id. Rename the runtime
      // old_key -> session_id; focus follows ONLY if we were viewing old_key
      // (so a BACKGROUND new session's capture never yanks the current view).
      const { old_key, session_id } = e;
      if (old_key === session_id) return state;
      const runtimes = { ...state.runtimes };
      if (runtimes[old_key]) {
        const source = runtimes[old_key];
        const target = runtimes[session_id];
        if (target) {
          const seen = new Set(target.turns.map((turn) => turn.id));
          const mergedTurns = [
            ...target.turns,
            ...source.turns.filter((turn) => !seen.has(turn.id)),
          ];
          const mergedRuntime: SessionRuntime = {
            ...target,
            ...source,
            state: target.state,
            syncReady: target.syncReady || source.syncReady,
            ccSessionId: session_id,
            turns: mergedTurns,
            queue: [...source.queue, ...target.queue],
            pendingSend: source.pendingSend ?? target.pendingSend,
          };
          replaceWithBoundedTurns(mergedRuntime, mergedTurns);
          runtimes[session_id] = mergedRuntime;
        } else {
          runtimes[session_id] = { ...source, ccSessionId: session_id };
        }
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
    case "history": {
      // Bulk on-demand history (one frame, read from the transcript — like a web
      // chat's GET /conversation). Rebuild this session's COMPLETED turns by
      // running the events through a throwaway empty runtime: this reuses the
      // per-event reduce logic verbatim so deltas accumulate EXACTLY ONCE (never
      // double-appending over cache-hydrated or live text). Any not-yet-done turn
      // already in the runtime (an in-flight turn still streaming live, not yet in
      // the transcript) is preserved and appended after the rebuilt history.
      const sid = e.session_id;
      let scratch: AppState = {
        ...state, banner: undefined, runtimes: { [sid]: createRuntime() },
      };
      for (const ev of e.events) {
        scratch = reduceEvent(scratch, ev as ServerEvent, false);
      }
      const built = scratch.runtimes[sid] ?? createRuntime();
      const base = state.runtimes[sid] ?? createRuntime();
      let turns: Turn[];
      if (e.before) {
        // pagination (load older): PREPEND the older turns ahead of what we have,
        // deduped by id — keeps the current view and in-flight turn intact.
        const haveIds = new Set(base.turns.map((t) => t.id));
        turns = [...built.turns.filter((t) => !haveIds.has(t.id)), ...base.turns];
      } else {
        // Initial load has no atomic transcript/live boundary. Merge instead of
        // replacing: preserve just-finished turns not flushed to disk, correlate
        // optimistic client ids by prompt/time, and combine an in-flight tail.
        turns = mergeInitialHistory(built.turns, base.turns, {
          // History's final TurnEnd is synthetic: Claude transcripts do not
          // contain ResultMessage.  A focus switch can read that EOF while the
          // resident turn is still running, so never let it close the live tail.
          preserveLiveTailOpen: !!e.in_progress || base.state !== "idle",
        });
      }
      const boundedTurns = boundRuntimeTurns(turns);
      const historyTrimmed = boundedTurns.length < turns.length;
      turns = boundedTurns;
      const hadModel = e.events.some((ev) => (ev as { type?: string }).type === "model");
      return {
        ...state,
        banner: scratch.banner ?? state.banner,
        runtimes: {
          ...state.runtimes,
          [sid]: {
            ...base, turns, loading: false,
            model: hadModel ? built.model : base.model,
            hasMore: historyTrimmed ? false : e.has_more,
            oldestId: historyTrimmed
              ? (turns[0]?.id ?? null)
              : (e.oldest_id ?? base.oldestId),
            truncated: base.truncated || historyTrimmed,
            // A native `claude`/`codex` in the terminal owns this session and is
            // appending to its transcript; the wrapper mirrors those appends here.
            // Render read-only — a cc session has ONE owner, and typing would fork it.
            external: !!e.external,
            takeoverPending: !!e.takeover_pending,
            takeoverMessage: e.takeover_pending ? base.takeoverMessage : null,
          },
        },
      };
    }
    case "dir_list":
      return { ...state, dirPicker: { path: e.path, parent: e.parent ?? null, dirs: e.dirs } };
    // The engine's real model catalog. Empty => the wrapper couldn't read it; keep
    // what we have (data.ts's static table) rather than blanking the pickers.
    case "models": {
      if (!e.models.length) return state;
      const catalog = { ...state.catalog, [e.engine]: e.models };
      const catalogDefault = e.default_model
        ? { ...state.catalogDefault, [e.engine]: e.default_model }
        : state.catalogDefault;
      return { ...state, catalog, catalogDefault };
    }
    case "wrapper_disconnected":
      return {
        ...state,
        runtimes: Object.fromEntries(Object.entries(state.runtimes).map(
          ([sid, runtime]) => [sid, {
            ...runtime, syncReady: false, replaying: false,
          }])),
        wrapperOnline: false,
        banner: "machine offline — waiting for reconnect",
      };
    case "wrapper_reconnected":
      // The event only proves a process connected to the relay. Wait for this
      // client's Hello replay/snapshot before draining any queued turns.
      return { ...state, wrapperOnline: false, banner: "machine reconnected — syncing…" };
    case "diff_report":
      if (!state.artifact || state.artifact.file !== e.file
          || state.artifact.sid !== (e.sid ?? state.focusedSid)) return state;
      return { ...state, artifact: {
        file: e.file, sid: state.artifact.sid, kind: "gitdiff", sections: parseGitDiff(e.diff),
      } };
    case "state":
      return patch(state, e.sid, (rt) => {
        rt.state = e.state;
        const turns = cloneTurns(rt.turns);
        const turn = e.msg_id
          ? turns.find((candidate) => candidate.id === e.msg_id)
          : turns[turns.length - 1];
        if (e.detail && turn && !turn.done) turn.progress = e.detail;
        else if (turn && (Object.hasOwn(e, "detail") || e.state !== "running")) {
          turn.progress = undefined;
        }
        if (e.state === "idle") {
          rt.pendingQuestion = null;
        }
        rt.turns = turns;
      });
    case "takeover_state":
      return patch(state, e.sid, (rt) => {
        rt.takeoverPending = e.pending;
        rt.takeoverMessage = e.message ?? null;
      });
    case "model":
      return patch(state, e.sid, (rt) => { rt.model = matchModelId(e.model); });
    case "effort":
      return patch(state, e.sid, (rt) => { rt.effort = e.effort; });
    case "fast":
      return patch(state, e.sid, (rt) => { rt.fast = e.on; });
    case "btw_opened": {
      // open the side panel + ensure a runtime for the fork; do NOT change focus
      // (the main view stays put — the fork lives only in the panel).
      const runtimes = { ...state.runtimes, [e.btw_sid]: state.runtimes[e.btw_sid] ?? createRuntime() };
      return { ...state, btwSid: e.btw_sid, btwEngine: e.engine, runtimes };
    }
    case "perm":
      return patch(state, e.sid, (rt) => { rt.perm = e.mode; });
    case "context_report":
      return patch(state, e.sid, (rt) => { rt.contextReport = e; });
    case "ask_user":
      return patch(state, e.sid, (rt) => { rt.pendingQuestion = { ask_id: e.ask_id, header: e.header, question: e.question, options: e.options, allow_text: e.allow_text, secret: e.secret }; });
    case "goal_state":
      return patch(state, e.sid, (rt) => { rt.goal = e.goal ?? null; });
    case "status_report":
      return patch(state, e.sid, (rt) => { rt.statusReport = e; });
    case "replay_start":
      return { ...patch(state, e.sid, (rt) => {
        rt.replaying = true;
        rt.syncReady = false;
        rt.truncated = e.truncated;
        // rebuild clears turns then refills — keep loading=true so the gap shows a
        // spinner rather than briefly flashing the empty "send a message" prompt.
        if (e.truncated || !!e.rebuild) { rt.turns = []; rt.loading = true; }
        if (e.rebuild) rt.pendingQuestion = null;
      }, true) };
    case "replay_end":
      return { ...patch(state, e.sid, (rt) => {
        rt.replaying = false;
        rt.syncReady = true;
        rt.truncated = rt.truncated || e.truncated;
        rt.loading = false;
      }, true), wrapperOnline: true };
    case "error": {
      // The relay has not accepted/rejected the command yet: reliable commands
      // stay in the outbox and will be retried when the wrapper returns. Keep the
      // optimistic turn pending instead of falsely marking it failed.
      if (e.code === "wrapper_offline") {
        return {
          ...state,
          runtimes: Object.fromEntries(Object.entries(state.runtimes).map(
            ([sid, runtime]) => [sid, {
              ...runtime, syncReady: false, replaying: false,
            }])),
          wrapperOnline: false,
          banner: "machine offline — waiting for reconnect",
        };
      }
      if (!e.msg_id) {
        return { ...state, banner: `${e.code}: ${e.message}` };
      }
      return patch(state, e.sid, (rt) => {
        rt.loading = false; // never leave a spinner spinning behind an error
        const turns = cloneTurns(rt.turns);
        const t = turns.find((turn) => turn.id === e.msg_id);
        if (t) {
          t.error = `${e.code}: ${e.message}`;
          t.progress = undefined;
          t.done = true;
          t.doneTs ??= Date.now();
        }
        else turns.push({ id: e.msg_id!, prompt: "", blocks: [], done: true,
          error: `${e.code}: ${e.message}`, doneTs: Date.now() });
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
        rt.pendingQuestion = null;
      }, true);
    }
    case "user_msg":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const existing = turns.find((t) => t.id === e.msg_id);
        const imgs = (e.images && e.images.length) ? e.images : undefined;
        const fileMeta = (e.files && e.files.length)
          ? e.files.map((file) => ({ filename: file.filename, data: "" }))
          : undefined;
        // Server time correlates the optimistic id with transcript history. The
        // client clock may drift, so authoritative echo time replaces it.
        const stamp = e.ts ? Math.round(e.ts * 1000) : undefined;
        if (existing) {
          if (!existing.prompt && e.prompt) existing.prompt = e.prompt;
          if (!existing.images && imgs) existing.images = imgs;
          if (fileMeta) existing.files = fileMeta;
          else if (existing.files) existing.files = existing.files.map(
            (file) => ({ filename: file.filename, data: "" }));
          if (stamp) existing.ts = stamp;
        } else {
          turns.push({ id: e.msg_id, prompt: e.prompt, images: imgs,
            files: fileMeta, blocks: [], done: false, ts: stamp });
        }
        rt.turns = turns;
      });
    case "assistant_msg_start":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = turns[turns.length - 1];
        if (!t || t.done) { t = { id: e.message_id, prompt: "", blocks: [], done: false }; turns.push(t); }
        t.progress = undefined;
        if (!t.blocks.some((b) => b.kind === "text" && b.message_id === e.message_id))
          t.blocks.push({ kind: "text", message_id: e.message_id, text: "", done: false });
        rt.turns = turns;
      });
    case "delta":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = turns[turns.length - 1];
        if (!t || t.done) { t = { id: e.message_id, prompt: "", blocks: [], done: false }; turns.push(t); }
        t.progress = undefined;
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
        t.progress = undefined;
        if (!t.blocks.some((b) => b.kind === "tool" && b.tool_use_id === e.tool_use_id))
          t.blocks.push({ kind: "tool", message_id: e.message_id, tool_use_id: e.tool_use_id, tool: e.tool, input: e.input, done: false });
        rt.turns = turns;
      });
    case "tool_result":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        for (const t of turns) {
          const b = t.blocks.find((b) => b.kind === "tool" && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
          if (b) {
            b.result = { content: e.content, is_error: e.is_error, truncated: e.truncated ?? undefined };
            b.done = true;
            t.progress = undefined;
            break;
          }
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
          if (e.turn_id) t.codexTurnId = e.turn_id;
          t.progress = undefined;
          if (e.result.subtype === "error_during_execution") t.interrupted = true;
          // Stamp completion time from the event's own server ts (seconds -> ms).
          // Robust for BOTH live turns and replayed history: the old
          // `t.ts + duration_ms` reconstruction dropped the timestamp for any turn
          // without a client-side start time (i.e. everything after a refresh,
          // where turns come from history replay). Fall back to start time, then now.
          t.doneTs = e.ts ? Math.round(e.ts * 1000) : (t.ts || Date.now());
        }
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
        rt.state = "idle";
        rt.pendingQuestion = null;
      });
    case "pong":
    case "command_ack":
    case "session_forked":
    case "hello":
      return state;
  }
}

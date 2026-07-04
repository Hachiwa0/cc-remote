// Turn/block state model for the chat UI.
//
// A Turn = one user query + the assistant's response (which may span multiple
// assistant messages with tool calls). Blocks are keyed by message_id (text) or
// tool_use_id (tool). The reducer applies server events in arrival order; the
// wrapper's emit-lock guarantees replay batches arrive contiguously before live
// events, so no client-side reordering is needed.
import type { ConnState } from "./ws";
import type { ServerEvent, State } from "./protocol";

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
}

export interface AppState {
  turns: Turn[];
  state: State;
  connState: ConnState;
  ccSessionId?: string;
  replaying: boolean;
  truncated: boolean; // history may be incomplete
  banner?: string; // status line (machine offline, catching up, ...)
}

export type Action =
  | { type: "event"; event: ServerEvent }
  | { type: "query_sent"; prompt: string; msg_id: string }
  | { type: "conn"; connState: ConnState; detail?: string };

export const initialState: AppState = {
  turns: [],
  state: "idle",
  connState: "connecting",
  replaying: false,
  truncated: false,
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
      const turn: Turn = { id: action.msg_id, prompt: action.prompt, blocks: [], done: false };
      return { ...state, turns: [...state.turns, turn] };
    }
    case "event": {
      return reduceEvent(state, action.event);
    }
  }
}

function reduceEvent(state: AppState, e: ServerEvent): AppState {
  switch (e.type) {
    case "snapshot": {
      // For a fresh connect (no turns yet) with prior context, show the tail as
      // a faded text turn. On reconnect-with-gap, replay follows and rebuilds
      // properly, so ignore the tail there.
      let turns = state.turns;
      if (turns.length === 0 && e.tail_text) {
        turns = [{
          id: "snapshot",
          prompt: "",
          blocks: [{ kind: "text", message_id: "snapshot", text: e.tail_text, done: true }],
          done: true,
        }];
      }
      return { ...state, turns, state: e.state, ccSessionId: e.cc_session_id ?? undefined };
    }
    case "state":
      return { ...state, state: e.state };
    case "replay_start":
      return { ...state, replaying: true, truncated: e.truncated };
    case "replay_end":
      return { ...state, replaying: false, truncated: state.truncated || e.truncated };
    case "wrapper_disconnected":
      return { ...state, banner: "machine offline — waiting for reconnect" };
    case "wrapper_reconnected":
      // wrapper came back; its buffer survived, but we may have missed events
      // during the gap. The App re-hellos on this event to trigger replay.
      return { ...state, banner: undefined, state: e.state, ccSessionId: e.cc_session_id ?? undefined };
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
      }
      return { ...state, turns, state: "idle" };
    }
    case "pong":
    case "hello":
      return state;
  }
}

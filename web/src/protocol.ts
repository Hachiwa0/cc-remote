// Mirror of cc_remote/protocol.py. Kept in sync manually (generate later).

export type State = "idle" | "running" | "interrupting" | "draining";

interface Base {
  v: number;
  type: string;
  ts: number;
  sid?: string | null;
  seq?: number | null;
  to?: string | null;
}

export interface Hello extends Base {
  type: "hello";
  role: "client" | "wrapper";
  client_id?: string | null;
  last_seq?: number | null;
  cc_session_id?: string | null;
  state?: State | null;
  buffer_head_seq?: number | null;
  buffer_tail_seq?: number | null;
}
export interface Query extends Base { type: "query"; prompt: string; msg_id: string }
export interface Interrupt extends Base { type: "interrupt" }
export interface Ping extends Base { type: "ping"; n: number }
export interface Pong extends Base { type: "pong"; n: number }
export interface ReplayStart extends Base { type: "replay_start"; from_seq: number; to_seq: number; truncated: boolean }
export interface ReplayEnd extends Base { type: "replay_end"; to_seq: number; truncated: boolean }
export interface Snapshot extends Base { type: "snapshot"; cc_session_id?: string | null; state: State; tail_text: string }
export interface StateEvent extends Base { type: "state"; state: State }
export interface UserMsg extends Base { type: "user_msg"; msg_id: string; prompt: string }
export interface AssistantMsgStart extends Base { type: "assistant_msg_start"; message_id: string }
export interface Delta extends Base { type: "delta"; message_id: string; text: string }
export interface ToolUse extends Base { type: "tool_use"; message_id: string; tool_use_id: string; tool: string; input: Record<string, unknown> }
export interface ToolResult extends Base { type: "tool_result"; tool_use_id: string; content: string; is_error: boolean; truncated?: boolean | null }
export interface AssistantMsgEnd extends Base { type: "assistant_msg_end"; message_id: string }
export interface TurnResult { subtype: string; duration_ms: number; is_error: boolean; total_cost_usd?: number | null; num_turns?: number | null }
export interface TurnEnd extends Base { type: "turn_end"; result: TurnResult }
export interface ErrorMsg extends Base { type: "error"; code: string; message: string }
export interface WrapperDisconnected extends Base { type: "wrapper_disconnected" }
export interface WrapperReconnected extends Base { type: "wrapper_reconnected"; cc_session_id?: string | null; state: State }

export type ServerEvent =
  | Pong | ReplayStart | ReplayEnd | Snapshot | StateEvent
  | UserMsg | AssistantMsgStart | Delta | ToolUse | ToolResult | AssistantMsgEnd
  | TurnEnd | ErrorMsg | WrapperDisconnected | WrapperReconnected | Hello;

export const PROTOCOL_VERSION = 1;

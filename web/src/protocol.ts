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
  cursors?: Record<string, number> | null;
  cc_session_id?: string | null;
  state?: State | null;
  buffer_head_seq?: number | null;
  buffer_tail_seq?: number | null;
}
export interface QueryImg { media_type: string; data: string }
export interface QueryFile { filename: string; data: string }
export interface Query extends Base { type: "query"; prompt: string; msg_id: string; images?: QueryImg[] | null; files?: QueryFile[] | null }
export interface Interrupt extends Base { type: "interrupt" }
export interface SetModel extends Base { type: "set_model"; model: string }
export interface SetEffort extends Base { type: "set_effort"; effort: string }
export interface SetServiceTier extends Base { type: "set_service_tier"; service_tier: string }
export interface Ping extends Base { type: "ping"; n: number }
export interface Pong extends Base { type: "pong"; n: number }
export interface ReplayStart extends Base { type: "replay_start"; from_seq: number; to_seq: number; truncated: boolean; rebuild?: boolean }
export interface ReplayEnd extends Base { type: "replay_end"; to_seq: number; truncated: boolean }
export interface Snapshot extends Base { type: "snapshot"; cc_session_id?: string | null; state: State; tail_text: string; cwd?: string | null }
export interface StateEvent extends Base { type: "state"; state: State }
export interface Model extends Base { type: "model"; model: string }
export interface Effort extends Base { type: "effort"; effort: string }
export interface Fast extends Base { type: "fast"; on: boolean }
export interface UserMsg extends Base { type: "user_msg"; msg_id: string; prompt: string; images?: QueryImg[] | null }
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

// sessions
export interface SessionInfo {
  session_id: string;
  summary?: string | null;
  last_modified?: string | null;
  first_prompt?: string | null;
  git_branch?: string | null;
  cwd?: string | null;
  tag?: string | null;
  state?: State | null;
  engine?: "claude" | "codex" | null;
}
export interface ListSessions extends Base { type: "list_sessions"; engine?: "claude" | "codex" }
export interface SwitchSession extends Base { type: "switch_session"; session_id: string; engine?: "claude" | "codex" }
export interface NewSession extends Base { type: "new_session"; cwd?: string | null; engine?: "claude" | "codex" }
export interface SessionList extends Base { type: "session_list"; sessions: SessionInfo[] }
export interface SessionFocus extends Base { type: "session_focus"; session_id: string; cwd?: string | null }
// NON-focusing re-key: a temp-keyed new session captured its real cc id. Rename
// the runtime old_key -> session_id + migrate the cursor; focus only follows if
// we were already viewing old_key. Prevents focus-steal by background sessions.
export interface SessionRekey extends Base { type: "session_rekey"; old_key: string; session_id: string; cwd?: string | null }
export interface RenameSession extends Base { type: "rename_session"; session_id: string; title: string }
export interface ArchiveSession extends Base { type: "archive_session"; session_id: string; archived: boolean }
export interface DirEntry { name: string; path: string }
export interface ListDir extends Base { type: "list_dir"; path?: string | null }
export interface DirList extends Base { type: "dir_list"; path: string; parent?: string | null; dirs: DirEntry[] }
export interface SetPerm extends Base { type: "set_perm"; mode: string }
export interface Perm extends Base { type: "perm"; mode: string }
export interface GetContext extends Base { type: "get_context" }
export interface GetDiff extends Base { type: "get_diff"; file: string; theme?: string }
export interface DiffReport extends Base { type: "diff_report"; file: string; diff: string }
// On-demand bulk history: fetched once when a session is opened (like a web
// chat's GET /conversation) instead of replaying the ring buffer on every hello.
export interface GetHistory extends Base { type: "get_history"; session_id: string; client_id?: string | null; cwd?: string | null; before?: string | null; limit?: number | null }
export interface History extends Base { type: "history"; session_id: string; events: ServerEvent[]; has_more: boolean; oldest_id?: string | null; newest_id?: string | null; before?: string | null }
export interface AskOption { label: string; ds?: string }
export interface AskUser extends Base { type: "ask_user"; ask_id: string; question: string; options: AskOption[] }
export interface AnswerQuestion extends Base { type: "answer_question"; ask_id: string; answer: string }
export interface ContextCategory { name: string; tokens: number; color: string; isDeferred?: boolean }
export interface ContextReport extends Base {
  type: "context_report";
  total_tokens: number;
  max_tokens: number;
  percentage: number;
  model?: string | null;
  is_auto_compact_enabled?: boolean | null;
  categories: ContextCategory[];
}

export type ServerEvent =
  | Pong | ReplayStart | ReplayEnd | Snapshot | StateEvent | Model | Effort | Fast | Perm | ContextReport | DiffReport | History
  | AskUser
  | SessionList | SessionFocus | SessionRekey
  | DirList
  | UserMsg | AssistantMsgStart | Delta | ToolUse | ToolResult | AssistantMsgEnd
  | TurnEnd | ErrorMsg | WrapperDisconnected | WrapperReconnected | Hello;

export const PROTOCOL_VERSION = 3;

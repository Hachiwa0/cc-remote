import type {
  AssistantChannel,
  ConversationImageRef,
  PlanEntry,
  ProcessKind,
  ProcessStatus,
  QueryFile,
  QueryImg,
  ServerEvent,
  ToolCategory,
} from "../protocol";

export interface TextBlock {
  kind: "text";
  message_id: string;
  text: string;
  done: boolean;
  channel?: AssistantChannel;
}

export interface ToolBlock {
  kind: "tool";
  message_id: string;
  tool_use_id: string;
  tool: string;
  input: Record<string, unknown>;
  category?: ToolCategory;
  title?: string | null;
  parent_id?: string | null;
  server?: string | null;
  progress?: string;
  output?: string;
  diff?: string;
  result?: {
    content: string;
    is_error: boolean;
    truncated?: boolean | null;
    status?: ProcessStatus | null;
    summary?: string | null;
    diff?: string | null;
    exit_code?: number | null;
    duration_ms?: number | null;
  };
  done: boolean;
}

export interface ProcessBlock {
  kind: "process";
  item_id: string;
  processKind: ProcessKind;
  phase: "start" | "update" | "end" | "snapshot";
  status: ProcessStatus;
  turn_id?: string | null;
  parent_id?: string | null;
  title: string;
  summary?: string | null;
  detail?: string | null;
  input?: Record<string, unknown> | null;
  output?: string | null;
  diff?: string | null;
  progress?: string | null;
  server?: string | null;
  tool?: string | null;
  command?: string | null;
  cwd?: string | null;
  exit_code?: number | null;
  duration_ms?: number | null;
  truncated?: boolean | null;
  explanation?: string | null;
  plan?: PlanEntry[];
  done: boolean;
}

export type Block = TextBlock | ToolBlock | ProcessBlock;

export interface TurnDetailSegment {
  pageKey: string;
  before: string | null;
  events: ServerEvent[];
  hasMore: boolean;
  oldestCursor: string | null;
  hasNewer: boolean;
  newerCursor: string | null;
  encodedChars: number;
}

export interface TurnDetailProjection {
  segments: TurnDetailSegment[];
  blocks: Block[];
  capped: boolean;
  hasMore: boolean;
  oldestCursor: string | null;
  hasNewer: boolean;
  newerCursor: string | null;
}

export interface Turn {
  id: string;
  /** Codex turn/steer's browser id persisted beside a distinct history cursor. */
  clientMsgId?: string;
  /** Native history user id when it differs from the optimistic browser id. */
  historyTurnId?: string;
  /** Engine-specific authoritative branch point. */
  forkPointId?: string;
  /** Claude's authoritative top-level user transcript UUID. */
  checkpointId?: string;
  /** @deprecated Read only while migrating CACHE_VER=5 entries. */
  codexTurnId?: string;
  /** Routing-only native task identity for a steered live segment. */
  liveTaskId?: string;
  prompt: string;
  blocks: Block[];
  done: boolean;
  interrupted?: boolean;
  error?: string;
  progress?: string;
  images?: QueryImg[];
  imageRefs?: ConversationImageRef[];
  files?: QueryFile[];
  ts?: number;
  doneTs?: number;
  durationMs?: number;
  detailEventCount?: number;
  detailLoaded?: boolean;
  detailLoading?: boolean;
  detailHasMore?: boolean;
  detailOldestCursor?: string | null;
  detailHasNewer?: boolean;
  detailNewerCursor?: string | null;
  /** Heavy, cursor-paged process history; never subject to Turn.blocks caps. */
  detailProjection?: TurnDetailProjection;
  /** Initial disclosure click keeps fetching older pages until EOF or cap. */
  detailAutoLoad?: boolean;
}

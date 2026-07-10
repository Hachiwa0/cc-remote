// Slash commands, models, permission modes. Slash commands split into
// client-side ones (CLIENT_SLASHES: model/plan/normal/permissions/clear/context,
// handled in Composer.send) and cc skills (forwarded verbatim to cc). Model/perm
// chips drive set_model / set_permission_mode on the wrapper.

export interface CmdGroup { g: string }
export interface Cmd { slash: string; name: string; ds: string; ic: string }
export type Command = CmdGroup | Cmd;

export const COMMANDS: Command[] = [
  { g: "模式" },
  { slash: "plan", name: "Plan mode", ds: "先给方案，确认后再动手", ic: "plan" },
  { slash: "normal", name: "普通模式", ds: "直接执行，边做边说", ic: "run" },
  { slash: "permissions", name: "权限模式", ds: "选择 cc 的权限模式", ic: "shield" },
  { g: "模型" },
  { slash: "model", name: "切换模型", ds: "/model <id> 切到指定模型(支持隐藏模型),无参数则打开选择器", ic: "cpu" },
  { g: "审查" },
  { slash: "code-review", name: "代码审查", ds: "审当前 diff 的正确性与可简化项", ic: "review" },
  { slash: "security-review", name: "安全审查", ds: "扫描分支改动的安全隐患", ic: "shield" },
  { slash: "verify", name: "验证改动", ds: "真跑一遍确认行为符合预期", ic: "verify" },
  { slash: "simplify", name: "精简", ds: "复用、简化、去重", ic: "simplify" },
  { g: "技能" },
  { slash: "run", name: "运行 App", ds: "启动并驱动本项目查看效果", ic: "run" },
  { slash: "deep-research", name: "深度调研", ds: "多源检索 + 交叉验证 + 成文", ic: "research" },
  { slash: "init", name: "初始化 CLAUDE.md", ds: "生成代码库说明", ic: "init" },
  { g: "会话" },
  { slash: "btw", name: "侧边对话 (btw)", ds: "基于当前会话开一个临时 fork 侧聊,不影响主线", ic: "spark" },
  { slash: "clear", name: "清空会话", ds: "开新会话，清空上下文", ic: "close" },
  { slash: "context", name: "上下文用量", ds: "查看 token 占用", ic: "cpu" },
];

export interface Model { id: string; name: string; ds: string; ic: string }
export const MODELS: Model[] = [
  { id: "claude-mythos-5", name: "Mythos 5", ds: "最强王牌", ic: "crown" },
  { id: "claude-opus-4-8", name: "Opus 4.8", ds: "最强推理", ic: "gem" },
  { id: "claude-sonnet-5", name: "Sonnet 5", ds: "均衡 · 更快", ic: "balance" },
  { id: "claude-haiku-4-5", name: "Haiku 4.5", ds: "轻量 · 极速", ic: "bolt" },
  { id: "claude-fable-5", name: "Fable 5", ds: "大便", ic: "book" },
];

// Reasoning effort (思考强度) — maps to the cc `--effort` flag. Changing it
// respawns the session with resume (one cold context resend), so it's a
// deliberate knob, not a per-message toggle.
export interface Effort { id: string; name: string; ds: string; ic: string }
export const EFFORTS: Effort[] = [
  { id: "low", name: "低", ds: "最省 · 最快响应", ic: "gauge1" },
  { id: "medium", name: "中", ds: "适度思考", ic: "gauge2" },
  { id: "high", name: "高", ds: "深度推理 · 默认", ic: "gauge3" },
  { id: "xhigh", name: "超高", ds: "更深推理 · 部分模型支持", ic: "gauge4" },
  { id: "max", name: "最大", ds: "最强 · 最慢最贵", ic: "gauge5" },
];

// ---- Codex engine option sets (aligned with the codex app-server) ----
// Model ids + display names verified against the codex binary's embedded catalog
// (openai.gpt-5.6-sol -> "Sol", -terra -> "Terra", -luna -> "Luna"). Note codex's
// `model/list` RPC only advertises its built-in OpenAI catalog (5.5/5.4); a custom
// provider (config.toml `model_provider`) passes any model id straight through —
// and ~/.codex/config.toml already defaults to gpt-5.6-sol.
// 5.5/5.4 stay listed so OLDER sessions render their real model instead of falling
// back to the first entry (Composer falls back to MODELS_E[0] on an unknown id).
// The `ds` blurbs are our own labels — codex exposes no per-model description for
// a custom provider.
// supportedReasoningEfforts => low/medium/high/xhigh; "mode" maps to codex approval
// policy (untrusted/on-request/never).
export const CODEX_MODELS: Model[] = [
  { id: "gpt-5.6-sol", name: "GPT-5.6 Sol", ds: "旗舰 · 最强 agentic 编码", ic: "crown" },
  { id: "gpt-5.6-terra", name: "GPT-5.6 Terra", ds: "均衡 · 日常编码", ic: "balance" },
  { id: "gpt-5.6-luna", name: "GPT-5.6 Luna", ds: "轻量 · 更快", ic: "bolt" },
  { id: "gpt-5.5", name: "GPT-5.5", ds: "上一代 · 旧会话兼容", ic: "cpu" },
  { id: "gpt-5.4", name: "GPT-5.4", ds: "更早 · 旧会话兼容", ic: "cpu" },
];
export const CODEX_EFFORTS: Effort[] = [
  { id: "low", name: "低", ds: "更快 · 轻推理", ic: "gauge1" },
  { id: "medium", name: "中", ds: "均衡", ic: "gauge2" },
  { id: "high", name: "高", ds: "深度推理", ic: "gauge3" },
  { id: "xhigh", name: "超高", ds: "最深推理 · 最慢", ic: "gauge5" },
];
export const CODEX_PERMS: Perm[] = [
  { id: "never", name: "自动", short: "自动", ds: "不询问 · 直接执行", ic: "run" },
  { id: "on-request", name: "按需", short: "按需", ds: "需要时才询问", ic: "shield" },
  { id: "untrusted", name: "严格", short: "严格", ds: "每步都先询问", ic: "shield" },
];
export const modelsFor = (engine?: string): Model[] => (engine === "codex" ? CODEX_MODELS : MODELS);
export const effortsFor = (engine?: string): Effort[] => (engine === "codex" ? CODEX_EFFORTS : EFFORTS);
export const permsFor = (engine?: string): Perm[] => (engine === "codex" ? CODEX_PERMS : PERMS);

// Map a cc-reported model id (e.g. "claude-mythos-5[1m]") to a MODELS entry id.
export function matchModelId(m: string, engine?: string): string {
  const base = m.replace(/\[.*\]$/, "");
  const hit = modelsFor(engine).find((x) => base === x.id || base.startsWith(x.id));
  return hit ? hit.id : m;
}

export interface Perm { id: string; name: string; short: string; ds: string; ic: string; danger?: boolean }
export const PERMS: Perm[] = [
  { id: "default", name: "默认", short: "询问", ds: "每次动作前询问", ic: "shield" },
  { id: "acceptEdits", name: "自动接受编辑", short: "编辑", ds: "文件编辑免询问，命令仍询问", ic: "edit" },
  { id: "plan", name: "Plan 模式", short: "Plan", ds: "只读 · 先出方案再执行", ic: "plan" },
  { id: "auto", name: "自动", short: "自动", ds: "自动执行常规操作", ic: "run" },
  { id: "bypassPermissions", name: "危险模式", short: "危险", ds: "危险 · 不询问直接执行 · --dangerously-skip-permissions", ic: "bolt", danger: true },
];

export function isCmd(c: Command): c is Cmd {
  return (c as Cmd).slash !== undefined;
}

const CMD_LIST: Cmd[] = COMMANDS.filter(isCmd) as Cmd[];

// Slashes handled locally by the web client (never forwarded to cc as a prompt).
// Everything else (code-review, verify, run, deep-research, …) is a cc skill and
// is forwarded verbatim so cc's own slash-command layer runs it.
export const CLIENT_SLASHES = new Set(["model", "plan", "normal", "permissions", "clear", "context", "btw"]);

// Codex engine command palette — its REAL slash commands (verified against the
// codex binary's own "get started" hint: /init /status /permissions /model /review).
// Client-handled: model/clear/context/status/permissions (status & context both
// surface token usage; permissions opens the approval-policy picker). Forwarded
// ones (review/init) expand to a prompt codex handles agentically — the app-server
// has no TUI slash layer. NB: /fast is a codex keybinding (not an app-server knob),
// and /hook /plan are Claude-only — so they're intentionally omitted.
export const CODEX_COMMANDS: Command[] = [
  { g: "审查" },
  { slash: "review", name: "代码审查", ds: "审当前 git 改动的 bug 与改进", ic: "review" },
  { g: "项目" },
  { slash: "init", name: "初始化 AGENTS.md", ds: "生成/更新代码库说明", ic: "init" },
  { g: "模式" },
  { slash: "model", name: "切换模型", ds: "选择模型与思考强度", ic: "cpu" },
  { slash: "permissions", name: "权限模式", ds: "选择 Codex 的审批策略(自动/按需/严格)", ic: "shield" },
  { slash: "fast", name: "Fast 模式", ds: "开/关 Fast 服务档位(更快响应),下条消息生效", ic: "bolt" },
  { g: "会话" },
  { slash: "btw", name: "侧边对话 (btw)", ds: "基于当前会话开一个临时 fork 侧聊,不影响主线", ic: "spark" },
  { slash: "status", name: "会话状态", ds: "模型 · 思考强度 · token 占用", ic: "cpu" },
  { slash: "context", name: "上下文用量", ds: "查看 token 占用与容量", ic: "cpu" },
  { slash: "clear", name: "新会话", ds: "开新 codex 会话", ic: "close" },
];
const CODEX_CMD_LIST: Cmd[] = CODEX_COMMANDS.filter(isCmd) as Cmd[];
export const CODEX_CLIENT_SLASHES = new Set(["model", "clear", "context", "status", "permissions", "fast", "btw"]);
export const commandsFor = (engine?: string): Command[] => (engine === "codex" ? CODEX_COMMANDS : COMMANDS);
export const clientSlashesFor = (engine?: string): Set<string> => (engine === "codex" ? CODEX_CLIENT_SLASHES : CLIENT_SLASHES);
// codex slash -> the prompt actually sent to codex (agentic; no TUI slash layer).
export const CODEX_PROMPTS: Record<string, string> = {
  review: "Review the current git changes (the diff) for correctness bugs, risks, and simplifications. Be concise.",
  init: "Create or update AGENTS.md at the repo root: a concise overview of this codebase, how to build/test/run it, and key conventions.",
};

// The command "token" the user is typing after "/", up to the first space.
// null when the input isn't an in-progress slash command (no leading "/", or a
// space already started the arguments). Drives the palette's show/hide.
export function slashToken(input: string): string | null {
  if (!input.startsWith("/")) return null;
  const after = input.slice(1);
  if (/\s/.test(after)) return null; // a space => choosing args, not the command
  return after;
}

// Commands whose slash starts with `token` (case-insensitive, prefix match).
export function matchCommands(token: string, engine?: string): Cmd[] {
  const t = token.toLowerCase();
  const list = engine === "codex" ? CODEX_CMD_LIST : CMD_LIST;
  return list.filter((c) => c.slash.toLowerCase().startsWith(t));
}

// Split "/slash rest of args" -> { slash, args }. null if not a slash line.
export function parseSlash(input: string): { slash: string; args: string } | null {
  if (!input.startsWith("/")) return null;
  const m = input.slice(1).match(/^(\S+)\s*([\s\S]*)$/);
  if (!m) return null;
  return { slash: m[1].toLowerCase(), args: m[2].trim() };
}

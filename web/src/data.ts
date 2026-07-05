// Slash commands, models, permission modes — lifted from design/prototype.html.
// Phase 1: slash commands are client-side prompt prefixes; model/perm chips are
// UI-only (wired to the backend in Phase 2).

export interface CmdGroup { g: string }
export interface Cmd { slash: string; name: string; ds: string; ic: string }
export type Command = CmdGroup | Cmd;

export const COMMANDS: Command[] = [
  { g: "模式" },
  { slash: "plan", name: "Plan mode", ds: "先给方案，确认后再动手", ic: "plan" },
  { slash: "normal", name: "普通模式", ds: "直接执行，边做边说", ic: "run" },
  { slash: "permissions", name: "权限模式", ds: "选择 cc 的权限模式", ic: "shield" },
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
  { slash: "clear", name: "清空会话", ds: "开新会话，清空上下文", ic: "close" },
  { slash: "context", name: "上下文用量", ds: "查看 token 占用", ic: "cpu" },
];

export interface Model { id: string; name: string; ds: string }
export const MODELS: Model[] = [
  { id: "claude-mythos-5", name: "Mythos 5", ds: "默认" },
  { id: "claude-opus-4-8", name: "Opus 4.8", ds: "最强推理" },
  { id: "claude-sonnet-5", name: "Sonnet 5", ds: "均衡 · 更快" },
  { id: "claude-haiku-4-5", name: "Haiku 4.5", ds: "轻量 · 极速" },
  { id: "claude-fable-5", name: "Fable 5", ds: "创意写作" },
];

// Map a cc-reported model id (e.g. "claude-mythos-5[1m]") to a MODELS entry id.
export function matchModelId(m: string): string {
  const base = m.replace(/\[.*\]$/, "");
  const hit = MODELS.find((x) => base === x.id || base.startsWith(x.id));
  return hit ? hit.id : m;
}

export interface Perm { id: string; name: string; ds: string; ic: string; danger?: boolean }
export const PERMS: Perm[] = [
  { id: "default", name: "默认", ds: "每次动作前询问", ic: "shield" },
  { id: "acceptEdits", name: "自动接受编辑", ds: "文件编辑免询问，命令仍询问", ic: "edit" },
  { id: "plan", name: "Plan 模式", ds: "只读 · 先出方案再执行", ic: "plan" },
  { id: "auto", name: "自动", ds: "自动执行常规操作", ic: "run" },
  { id: "bypassPermissions", name: "跳过所有权限", ds: "危险 · 不询问直接执行 · --dangerously-skip-permissions", ic: "bolt", danger: true },
];

export function isCmd(c: Command): c is Cmd {
  return (c as Cmd).slash !== undefined;
}

import type { Engine, EngineInfo } from "./protocol";

const ENGINE_ORDER: readonly Engine[] = ["claude", "codex", "dsh"];

const ENGINE_FALLBACKS: Record<Engine, EngineInfo> = {
  claude: {
    id: "claude",
    display_name: "Claude Code",
    available: true,
    spaces: ["code", "work"],
  },
  codex: {
    id: "codex",
    display_name: "Codex",
    available: true,
    spaces: ["code", "work"],
  },
  dsh: {
    id: "dsh",
    display_name: "DeepSeek Harness",
    available: false,
    spaces: ["code"],
    reason: "正在检测本机 DSH 服务",
  },
};

export function resolvedEngineOptions(
  catalog: readonly EngineInfo[],
): EngineInfo[] {
  return ENGINE_ORDER.map((id) => (
    catalog.find((candidate) => candidate.id === id) ?? ENGINE_FALLBACKS[id]
  ));
}

const MUTATING_FILE_TOOLS = new Set([
  "write", "edit", "multiedit", "notebookedit", "editfile",
  "apply_patch", "filechange",
]);
const READ_FILE_TOOLS = new Set([
  "read", "readfile", "listfiles", "view_image", "viewimage",
]);

export interface FileOperationPresentation {
  icon: string;
  action: string;
  group: string;
}

function normalizedChangeKinds(input: Record<string, unknown>): Set<string> {
  const kinds = new Set<string>();
  const add = (change: unknown) => {
    if (!change || typeof change !== "object" || Array.isArray(change)) return;
    const record = change as Record<string, unknown>;
    if (record.move_path || record.destination_path || record.to) {
      kinds.add("move");
      return;
    }
    const raw = String(record.kind ?? record.type ?? "").toLowerCase();
    if (/^(?:add|added|create|created|new)$/.test(raw)) kinds.add("create");
    else if (/^(?:delete|deleted|remove|removed)$/.test(raw)) kinds.add("delete");
    else if (/^(?:move|moved|rename|renamed)$/.test(raw)) kinds.add("move");
    else if (raw) kinds.add("update");
  };
  const changes = input.changes;
  if (Array.isArray(changes)) changes.forEach(add);
  else if (changes && typeof changes === "object") {
    Object.values(changes).forEach(add);
  }
  return kinds;
}

/** Shared semantic presentation for both ToolBlock and official ProcessBlock. */
export function presentFileOperation(
  tool: string,
  input: Record<string, unknown>,
): FileOperationPresentation | null {
  const lower = tool.toLowerCase();
  if (READ_FILE_TOOLS.has(lower)) {
    return { icon: "read", action: "读取", group: "读取文件" };
  }
  if (!MUTATING_FILE_TOOLS.has(lower)
      && !input.changes && !input.file_paths) return null;

  const kinds = normalizedChangeKinds(input);
  if (kinds.size > 1) {
    return { icon: "code", action: "修改", group: "文件变更" };
  }
  const kind = kinds.values().next().value;
  if (kind === "create") {
    return { icon: "file-plus", action: "创建", group: "创建文件" };
  }
  if (kind === "delete") {
    return { icon: "trash", action: "删除", group: "删除文件" };
  }
  if (kind === "move") {
    return { icon: "branch", action: "移动", group: "移动文件" };
  }
  if (lower === "write") {
    return { icon: "code", action: "写入", group: "写入文件" };
  }
  return { icon: "code", action: "修改", group: "修改文件" };
}

function pushPath(paths: string[], seen: Set<string>, value: unknown): void {
  if (typeof value !== "string") return;
  const path = value.trim();
  if (!path || seen.has(path)) return;
  seen.add(path);
  paths.push(path);
}

/** Read the canonical field and both engines' legacy mutation payloads. */
export function filePathsFromInput(input?: Record<string, unknown> | null): string[] {
  if (!input) return [];
  const paths: string[] = [];
  const seen = new Set<string>();
  const canonical = input.file_paths;
  if (Array.isArray(canonical)) {
    canonical.forEach((path) => pushPath(paths, seen, path));
  }
  for (const key of ["file_path", "path", "notebook_path"] as const) {
    pushPath(paths, seen, input[key]);
  }

  const changes = input.changes;
  if (Array.isArray(changes)) {
    for (const change of changes) {
      if (!change || typeof change !== "object" || Array.isArray(change)) continue;
      const record = change as Record<string, unknown>;
      for (const key of ["path", "move_path", "destination_path", "to"] as const) {
        pushPath(paths, seen, record[key]);
      }
    }
  } else if (changes && typeof changes === "object") {
    for (const [path, change] of Object.entries(changes)) {
      pushPath(paths, seen, path);
      if (!change || typeof change !== "object" || Array.isArray(change)) continue;
      const record = change as Record<string, unknown>;
      for (const key of ["path", "move_path", "destination_path", "to"] as const) {
        pushPath(paths, seen, record[key]);
      }
    }
  }
  return paths;
}

export function mutatedFilePaths(tool: string, input: Record<string, unknown>): string[] {
  if (!MUTATING_FILE_TOOLS.has(tool.toLowerCase())) return [];
  return filePathsFromInput(input);
}

interface MutationBlock {
  kind: string;
  tool?: string | null;
  input?: Record<string, unknown> | null;
  diff?: string | null;
  result?: { diff?: string | null };
}

/** Collect only the paths and authoritative diffs emitted by one turn.
 *
 * The returned diff is deliberately not recomputed from the current worktree:
 * doing that would mix unrelated dirty files into an older turn's change card.
 */
export function collectTurnFileChanges(blocks: MutationBlock[]): {
  paths: string[];
  diff: string;
} {
  const paths: string[] = [];
  const seenPaths = new Set<string>();
  const diffs: string[] = [];
  const seenDiffs = new Set<string>();
  for (const block of blocks) {
    if (block.kind !== "tool" || !block.tool || !block.input) continue;
    const blockPaths = mutatedFilePaths(block.tool, block.input);
    if (!blockPaths.length) continue;
    for (const path of blockPaths) pushPath(paths, seenPaths, path);
    const diff = block.result?.diff ?? block.diff;
    if (typeof diff !== "string" || !diff.trim() || seenDiffs.has(diff)) continue;
    seenDiffs.add(diff);
    diffs.push(diff.trimEnd());
  }
  return { paths, diff: diffs.join("\n") };
}

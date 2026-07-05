// Line-level diff (LCS) for rendering Edit old_string -> new_string.
// Output: add (blue +), del (pink −), ctx (dim) lines.

export interface DiffLine { type: "add" | "del" | "ctx"; text: string; }

// ---- git diff parser (Claude/GitHub-style: file sections + hunks + line numbers) ----

export interface GitDiffLine { oldNo: number | null; newNo: number | null; type: "add" | "del" | "ctx"; text: string; }
export interface GitDiffHunk { header: string; lines: GitDiffLine[]; }
export interface GitDiffSection { file: string; hunks: GitDiffHunk[]; }

/** Parse raw `git diff` text into structured sections with line numbers.
 * Handles both `diff --git a/X b/X` headers and `--no-index` (untracked) diffs
 * that only carry `+++ b/X`. */
export function parseGitDiff(raw: string): GitDiffSection[] {
  const sections: GitDiffSection[] = [];
  let cur: GitDiffSection | null = null;
  let hunk: GitDiffHunk | null = null;
  let oldNo = 0, newNo = 0;
  for (const line of raw.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { file: m ? m[1] : line.slice(11), hunks: [] };
      sections.push(cur);
      hunk = null;
      continue;
    }
    if (line.startsWith("+++ ")) {
      if (!cur) {
        const f = line.slice(4).replace(/^b\//, "");
        cur = { file: f === "/dev/null" ? "(new file)" : f, hunks: [] };
        sections.push(cur);
      }
      continue;
    }
    if (line.startsWith("--- ") || line.startsWith("index ") || line.startsWith("similarity ") || line.startsWith("rename ") || line.startsWith("new file ") || line.startsWith("deleted file ") || line.startsWith("old mode ") || line.startsWith("new mode ")) {
      continue;
    }
    if (line.startsWith("@@ ")) {
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      oldNo = m ? parseInt(m[1], 10) : 0;
      newNo = m ? parseInt(m[2], 10) : 0;
      hunk = { header: line, lines: [] };
      if (cur) cur.hunks.push(hunk);
      continue;
    }
    if (line.startsWith("\\ No newline")) continue;
    if (!cur || !hunk) continue;
    if (line.startsWith("+")) {
      hunk.lines.push({ oldNo: null, newNo: newNo++, type: "add", text: line.slice(1) });
    } else if (line.startsWith("-")) {
      hunk.lines.push({ oldNo: oldNo++, newNo: null, type: "del", text: line.slice(1) });
    } else {
      hunk.lines.push({ oldNo: oldNo++, newNo: newNo++, type: "ctx", text: line.slice(1) });
    }
  }
  return sections;
}

export function diffLines(oldStr: string, newStr: string): DiffLine[] {
  const a = oldStr.split("\n");
  const b = newStr.split("\n");
  const n = a.length, m = b.length;
  // dp[i][j] = LCS length of a[i..] and b[j..]
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ type: "ctx", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ type: "del", text: a[i] }); i++; }
    else { out.push({ type: "add", text: b[j] }); j++; }
  }
  while (i < n) { out.push({ type: "del", text: a[i++] }); }
  while (j < m) { out.push({ type: "add", text: b[j++] }); }
  return out;
}

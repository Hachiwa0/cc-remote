import { useEffect, useMemo, useState } from "react";
import type { Options as ReactMarkdownOptions } from "react-markdown";
import remarkGfm from "remark-gfm";

type RemarkPlugins = NonNullable<ReactMarkdownOptions["remarkPlugins"]>;
type RehypePlugins = NonNullable<ReactMarkdownOptions["rehypePlugins"]>;

export const STREAMING_REMARK_PLUGINS: RemarkPlugins = [remarkGfm];

export interface MarkdownMathPlugins {
  remarkPlugins: RemarkPlugins;
  rehypePlugins: RehypePlugins;
}

let loadedPlugins: MarkdownMathPlugins | null = null;
let loadingPlugins: Promise<MarkdownMathPlugins> | null = null;

/** Load KaTeX only for a completed message which actually contains math.
 * Keeping this dynamic avoids adding the 259 KiB renderer to every mobile
 * history first paint. Exported for the zero-browser SSR regression harness. */
export function preloadMarkdownMathPlugins(): Promise<MarkdownMathPlugins> {
  if (loadedPlugins) return Promise.resolve(loadedPlugins);
  if (!loadingPlugins) {
    loadingPlugins = import("./markdown-math-plugins").then((module) => {
      loadedPlugins = {
        remarkPlugins: module.remarkPlugins,
        rehypePlugins: module.rehypePlugins,
      };
      return loadedPlugins;
    }).catch((error: unknown) => {
      loadingPlugins = null;
      throw error;
    });
  }
  return loadingPlugins;
}

interface Fence {
  marker: "`" | "~";
  length: number;
}

function fenceAtLineStart(line: string): Fence | null {
  const match = /^(?: {0,3})(`{3,}|~{3,})/.exec(line);
  if (!match) return null;
  return {
    marker: match[1][0] as Fence["marker"],
    length: match[1].length,
  };
}

function closesFence(line: string, fence: Fence): boolean {
  const match = /^(?: {0,3})(`{3,}|~{3,})[ \t]*$/.exec(line);
  return !!match
    && match[1][0] === fence.marker
    && match[1].length >= fence.length;
}

function slashIsEscaped(line: string, index: number): boolean {
  let preceding = 0;
  for (let cursor = index - 1; cursor >= 0 && line[cursor] === "\\"; cursor--) {
    preceding += 1;
  }
  return preceding % 2 === 1;
}

/** Normalize MathJax-style bracket delimiters for remark-math.
 *
 * Codex and Claude commonly emit `\(...\)` / `\[...\]`, while remark-math
 * consumes dollar delimiters. The scanner deliberately skips fenced and inline
 * code so examples and shell snippets remain byte-for-byte readable.
 */
export function normalizeMathDelimiters(source: string): string {
  let fence: Fence | null = null;
  let inlineTicks = 0;
  const normalized = source.split("\n").map((line) => {
    if (fence) {
      if (closesFence(line, fence)) fence = null;
      return line;
    }
    if (inlineTicks === 0) {
      const opening = fenceAtLineStart(line);
      if (opening) {
        fence = opening;
        return line;
      }
    }

    let output = "";
    for (let index = 0; index < line.length;) {
      if (line[index] === "`") {
        let end = index + 1;
        while (end < line.length && line[end] === "`") end += 1;
        const run = end - index;
        if (inlineTicks === 0) inlineTicks = run;
        else if (inlineTicks === run) inlineTicks = 0;
        output += line.slice(index, end);
        index = end;
        continue;
      }
      if (inlineTicks === 0 && line[index] === "\\"
          && !slashIsEscaped(line, index)) {
        const delimiter = line[index + 1];
        if (delimiter === "(" || delimiter === ")") {
          output += "$";
          index += 2;
          continue;
        }
        if (delimiter === "[" || delimiter === "]") {
          output += "\n$$\n";
          index += 2;
          continue;
        }
      }
      output += line[index];
      index += 1;
    }
    return output;
  });
  return normalized.join("\n");
}

export function hasMathDelimiters(source: string): boolean {
  if (normalizeMathDelimiters(source) !== source) return true;
  // remark-math performs the authoritative dollar parsing. This cheap hint may
  // load the chunk for a literal currency sign; the parser still decides
  // whether it forms a valid delimiter pair.
  return source.includes("$");
}

export function useMarkdownMathPlugins(
  source: string,
  enabled: boolean,
): MarkdownMathPlugins | null {
  const shouldLoad = useMemo(
    () => enabled && hasMathDelimiters(source),
    [enabled, source],
  );
  const [plugins, setPlugins] = useState<MarkdownMathPlugins | null>(
    shouldLoad ? loadedPlugins : null,
  );
  useEffect(() => {
    let cancelled = false;
    if (!shouldLoad) return;
    void preloadMarkdownMathPlugins().then((loaded) => {
      if (!cancelled) setPlugins(loaded);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [shouldLoad]);
  return shouldLoad ? (plugins ?? loadedPlugins) : null;
}

export function isMathFenceClass(className: string | undefined): boolean {
  if (!className) return false;
  const classes = new Set(className.split(/\s+/));
  return classes.has("language-math")
    || classes.has("math-display")
    || classes.has("math-inline");
}

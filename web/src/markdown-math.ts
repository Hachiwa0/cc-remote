import { useEffect, useRef, useState } from "react";
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

/** Load KaTeX only for a message which actually contains complete math.
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

interface MathSpan {
  kind: "bracket-inline" | "bracket-display";
  open: number;
  close: number;
  width: number;
}

interface MathScan {
  bracketSpans: MathSpan[];
  hasDollarSpan: boolean;
}

/** Find complete math delimiters without allocating source-sized masks.
 *
 * A later bracket opener replaces an unmatched one. During streaming this
 * keeps a stale `\\(` from consuming the closer of a newer complete formula.
 */
function scanCompleteMath(source: string): MathScan {
  const bracketSpans: MathSpan[] = [];
  let hasDollarSpan = false;
  let fence: Fence | null = null;
  let inlineTicks = 0;
  let bracketOpen: { kind: "(" | "["; index: number } | null = null;
  let dollarOpen: { width: number; index: number } | null = null;
  let lineStart = 0;
  while (lineStart <= source.length) {
    const newline = source.indexOf("\n", lineStart);
    const lineEnd = newline < 0 ? source.length : newline;
    const line = source.slice(lineStart, lineEnd);
    if (fence) {
      if (closesFence(line, fence)) fence = null;
    } else {
      const opening = inlineTicks === 0 ? fenceAtLineStart(line) : null;
      if (opening) {
        fence = opening;
      } else {
        let slashRun = 0;
        for (let index = lineStart; index < lineEnd;) {
          const char = source[index];
          if (char === "`") {
            let end = index + 1;
            while (end < lineEnd && source[end] === "`") end += 1;
            const run = end - index;
            if (inlineTicks === 0) inlineTicks = run;
            else if (inlineTicks === run) inlineTicks = 0;
            slashRun = 0;
            index = end;
            continue;
          }

          if (inlineTicks !== 0) {
            slashRun = 0;
            index += 1;
            continue;
          }

          if (char === "\\") {
            const escaped = slashRun % 2 === 1;
            const token = source[index + 1];
            if (!escaped && (token === "(" || token === "[")) {
              bracketOpen = { kind: token, index };
              slashRun = 0;
              index += 2;
              continue;
            }
            const expected = bracketOpen?.kind === "(" ? ")" : "]";
            if (!escaped && bracketOpen && token === expected
              && index > bracketOpen.index + 2) {
              bracketSpans.push({
                kind: bracketOpen.kind === "("
                  ? "bracket-inline" : "bracket-display",
                open: bracketOpen.index,
                close: index,
                width: 2,
              });
              bracketOpen = null;
              slashRun = 0;
              index += 2;
              continue;
            }
            slashRun += 1;
            index += 1;
            continue;
          }

          if (char === "$") {
            const escaped = slashRun % 2 === 1;
            slashRun = 0;
            if (escaped) {
              index += 1;
              continue;
            }
            const width = source[index + 1] === "$" ? 2 : 1;
            if (dollarOpen === null || dollarOpen.width !== width) {
              dollarOpen = { width, index };
            } else if (index > dollarOpen.index + width) {
              hasDollarSpan = true;
              dollarOpen = null;
            }
            index += width;
            continue;
          }

          slashRun = 0;
          index += 1;
        }
      }
    }
    if (newline < 0) break;
    lineStart = newline + 1;
  }

  return { bracketSpans, hasDollarSpan };
}

interface MathAnalysis {
  active: boolean;
  normalized: string;
}

function analyzeMath(source: string): MathAnalysis {
  if (!source.includes("$") && !source.includes("\\(")
    && !source.includes("\\[")) {
    return { active: false, normalized: source };
  }
  const scan = scanCompleteMath(source);
  const replacements = new Map<number, { width: number; text: string }>();
  for (const span of scan.bracketSpans) {
    if (span.kind === "bracket-inline") {
      replacements.set(span.open, { width: 2, text: "$" });
      replacements.set(span.close, { width: 2, text: "$" });
    } else if (span.kind === "bracket-display") {
      replacements.set(span.open, { width: 2, text: "\n$$\n" });
      replacements.set(span.close, { width: 2, text: "\n$$\n" });
    }
  }
  if (replacements.size === 0) {
    return {
      active: scan.hasDollarSpan || scan.bracketSpans.length > 0,
      normalized: source,
    };
  }
  let normalized = "";
  for (let index = 0; index < source.length;) {
    const replacement = replacements.get(index);
    if (replacement) {
      normalized += replacement.text;
      index += replacement.width;
    } else {
      normalized += source[index];
      index += 1;
    }
  }
  return { active: true, normalized };
}

/** Normalize MathJax-style bracket delimiters for remark-math.
 *
 * Codex and Claude commonly emit `\(...\)` / `\[...\]`, while remark-math
 * consumes dollar delimiters. The scanner deliberately skips fenced and inline
 * code so examples and shell snippets remain byte-for-byte readable.
 */
export function normalizeMathDelimiters(source: string): string {
  return analyzeMath(source).normalized;
}

export function hasMathDelimiters(source: string): boolean {
  return analyzeMath(source).active;
}

export interface MarkdownMathRenderState {
  plugins: MarkdownMathPlugins | null;
  normalizedSource: string;
}

export function useMarkdownMathPlugins(
  source: string,
  enabled: boolean,
): MarkdownMathRenderState {
  const sticky = useRef({
    source: "",
    normalized: "",
    active: false,
  });
  if (!enabled) {
    sticky.current = { source: "", normalized: "", active: false };
  } else {
    const previous = sticky.current;
    const appended = source.startsWith(previous.source);
    const delta = appended ? source.slice(previous.source.length) : "";
    if (appended && !delta.includes("\\") && !delta.includes("$")) {
      sticky.current = {
        source,
        normalized: previous.normalized + delta,
        active: previous.active,
      };
    } else {
      const analysis = analyzeMath(source);
      sticky.current = {
        source,
        normalized: analysis.normalized,
        active: analysis.active,
      };
    }
  }
  const shouldLoad = enabled && sticky.current.active;
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
  const activePlugins = shouldLoad ? (plugins ?? loadedPlugins) : null;
  return {
    plugins: activePlugins,
    normalizedSource: activePlugins ? sticky.current.normalized : source,
  };
}

export function isMathFenceClass(className: string | undefined): boolean {
  if (!className) return false;
  const classes = new Set(className.split(/\s+/));
  return classes.has("language-math")
    || classes.has("math-display")
    || classes.has("math-inline");
}

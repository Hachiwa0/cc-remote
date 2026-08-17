const DIRECTIVE = ":codex-file-citation{";
const MAX_PATH_BYTES = 4096;
const MAX_BODY_CHARS = 8192;
const WINDOWS_ABSOLUTE_PATH = /^[A-Za-z]:[\\/]/;
const ATTRIBUTE = /\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*("(?:\\.|[^"\\])*")/gy;

export interface CodexFileCitation {
  path: string;
  purpose: "output" | "source";
  artifactKind?: string;
}

interface ParsedDirective {
  citation: CodexFileCitation;
  end: number;
}

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: MarkdownNode[];
}

function validCitation(
  path: string | undefined,
  purpose: string | undefined,
  artifactKind?: string,
): CodexFileCitation | null {
  if (!path || (!path.startsWith("/") && !WINDOWS_ABSOLUTE_PATH.test(path))
      || Array.from(path).some((char) => {
        const code = char.charCodeAt(0);
        return code < 32 || code === 127;
      })
      || new TextEncoder().encode(path).length > MAX_PATH_BYTES
      || (purpose !== undefined && purpose !== "output" && purpose !== "source")
      || (artifactKind?.length ?? 0) > 512) return null;
  return {
    path,
    purpose: purpose === "output" ? "output" : "source",
    ...(artifactKind ? { artifactKind } : {}),
  };
}

export function parseCodexFileCitationDirective(
  text: string,
  start = 0,
): ParsedDirective | null {
  if (!text.startsWith(DIRECTIVE, start)) return null;
  const bodyStart = start + DIRECTIVE.length;
  const close = text.indexOf("}", bodyStart);
  if (close < 0 || close - bodyStart > MAX_BODY_CHARS) return null;
  const body = text.slice(bodyStart, close);
  const attributes: Record<string, string> = {};
  let cursor = 0;
  while (cursor < body.length) {
    ATTRIBUTE.lastIndex = cursor;
    const match = ATTRIBUTE.exec(body);
    if (!match) {
      if (/^\s*$/.test(body.slice(cursor))) break;
      return null;
    }
    if (Object.hasOwn(attributes, match[1])) return null;
    try {
      attributes[match[1]] = JSON.parse(match[2]) as string;
    } catch {
      return null;
    }
    cursor = ATTRIBUTE.lastIndex;
  }
  const citation = validCitation(
    attributes.path, attributes.purpose, attributes.artifact_kind);
  return citation ? { citation, end: close + 1 } : null;
}

function citationNodes(text: string): MarkdownNode[] | null {
  const nodes: MarkdownNode[] = [];
  let cursor = 0;
  let search = 0;
  while (search < text.length) {
    const start = text.indexOf(DIRECTIVE, search);
    if (start < 0) break;
    const parsed = parseCodexFileCitationDirective(text, start);
    if (!parsed) {
      search = start + DIRECTIVE.length;
      continue;
    }
    if (start > cursor) {
      nodes.push({ type: "text", value: text.slice(cursor, start) });
    }
    nodes.push({
      type: "link",
      url: encodeURIComponent(parsed.citation.path),
      title: [
        "cc-remote-file-citation",
        parsed.citation.purpose,
        parsed.citation.artifactKind ?? "",
      ].join(":"),
      children: [{
        type: "text",
        value: parsed.citation.path.split(/[\\/]/).pop() || "文件",
      }],
    });
    cursor = parsed.end;
    search = parsed.end;
  }
  if (cursor === 0) return null;
  if (cursor < text.length) {
    nodes.push({ type: "text", value: text.slice(cursor) });
  }
  return nodes;
}

function transformCitations(node: MarkdownNode): void {
  if (!node.children || node.type === "link" || node.type === "linkReference") return;
  for (let index = 0; index < node.children.length;) {
    const child = node.children[index];
    const replacements = child.type === "text" && typeof child.value === "string"
      ? citationNodes(child.value) : null;
    if (replacements) {
      node.children.splice(index, 1, ...replacements);
      index += replacements.length;
    } else {
      transformCitations(child);
      index += 1;
    }
  }
}

/** Convert official file citations only in prose text nodes, not code/links. */
export function remarkCodexFileCitations(): (tree: MarkdownNode) => void {
  return transformCitations;
}

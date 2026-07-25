import type { MermaidConfig } from "mermaid";

export const MAX_MERMAID_SOURCE_CHARS = 32 * 1024;
export const MAX_MERMAID_SOURCE_LINES = 500;
const MAX_MERMAID_EDGES = 500;
const UNSAFE_CSS = /(?:@import|expression\s*\(|javascript:|url\s*\(\s*(?!['"]?#))/i;
const URL_ATTRIBUTES = new Set(["href", "xlink:href", "src"]);

export type MermaidTheme = "light" | "dark";

export function isMermaidFenceClass(className: string | undefined): boolean {
  return className?.split(/\s+/).includes("language-mermaid") ?? false;
}

export function mermaidSourceProblem(source: string): string | null {
  if (!source.trim()) return "Mermaid 图表源码为空";
  if (source.length > MAX_MERMAID_SOURCE_CHARS) {
    return `Mermaid 图表过长（最多 ${MAX_MERMAID_SOURCE_CHARS.toLocaleString()} 个字符）`;
  }
  if (source.split(/\r?\n/).length > MAX_MERMAID_SOURCE_LINES) {
    return `Mermaid 图表行数过多（最多 ${MAX_MERMAID_SOURCE_LINES} 行）`;
  }
  return null;
}

function configForTheme(theme: MermaidTheme): MermaidConfig {
  return {
    startOnLoad: false,
    securityLevel: "strict",
    secure: [
      "secure",
      "securityLevel",
      "startOnLoad",
      "maxTextSize",
      "maxEdges",
      "htmlLabels",
      "dompurifyConfig",
      "theme",
      "themeVariables",
      "themeCSS",
      "fontFamily",
      "altFontFamily",
      "look",
      "layout",
      "suppressErrorRendering",
    ],
    suppressErrorRendering: true,
    htmlLabels: false,
    maxTextSize: MAX_MERMAID_SOURCE_CHARS,
    maxEdges: MAX_MERMAID_EDGES,
    theme: theme === "dark" ? "dark" : "neutral",
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
    logLevel: "fatal",
  };
}

type MermaidModule = typeof import("mermaid");
type DomPurifyModule = typeof import("dompurify");

let modulesPromise: Promise<{
  mermaid: MermaidModule["default"];
  DOMPurify: DomPurifyModule["default"];
}> | null = null;
let renderQueue: Promise<void> = Promise.resolve();

function removeRootLayoutStyles(root: Element & ElementCSSInlineStyle): void {
  root.style.removeProperty("width");
  root.style.removeProperty("height");
  root.style.removeProperty("max-width");
  root.style.removeProperty("max-height");
  if (!root.getAttribute("style")?.trim()) root.removeAttribute("style");
}

function loadModules() {
  modulesPromise ??= Promise.all([
    import("mermaid"),
    import("dompurify"),
  ]).then(([mermaidModule, domPurifyModule]) => ({
    mermaid: mermaidModule.default,
    DOMPurify: domPurifyModule.default,
  }));
  return modulesPromise;
}

function sanitizeSvg(
  svg: string,
  DOMPurify: DomPurifyModule["default"],
): string {
  const clean = DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: [
      "script",
      "foreignObject",
      "iframe",
      "object",
      "embed",
      "image",
    ],
    FORBID_ATTR: ["src", "target"],
  });
  const sanitized = String(clean);
  const namespaced = sanitized.includes("xlink:")
    && !/\sxmlns:xlink=/.test(sanitized)
    ? sanitized.replace(
        /^<svg\b/,
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink"',
      )
    : sanitized;
  const document = new DOMParser().parseFromString(namespaced, "image/svg+xml");
  if (document.querySelector("parsererror")
      || document.documentElement.localName !== "svg") {
    throw new Error("Mermaid 返回了无效 SVG");
  }
  removeRootLayoutStyles(document.documentElement);

  for (const anchor of Array.from(document.documentElement.querySelectorAll("a"))) {
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    for (const attribute of Array.from(anchor.attributes)) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") || URL_ATTRIBUTES.has(name) || name === "target") continue;
      group.setAttribute(attribute.name, attribute.value);
    }
    while (anchor.firstChild) group.append(anchor.firstChild);
    anchor.replaceWith(group);
  }

  const elements = [
    document.documentElement,
    ...document.documentElement.querySelectorAll("*"),
  ];
  for (const element of elements) {
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim();
      const internalReference = (name === "href" || name === "xlink:href")
        && /^#[a-zA-Z_][\w:.-]*$/.test(value);
      if (name.startsWith("on") || (URL_ATTRIBUTES.has(name) && !internalReference)) {
        element.removeAttribute(attribute.name);
      } else if (name === "style" && UNSAFE_CSS.test(value)) {
        element.removeAttribute(attribute.name);
      }
    }
  }
  for (const style of document.documentElement.querySelectorAll("style")) {
    if (UNSAFE_CSS.test(style.textContent || "")) style.remove();
  }
  return new XMLSerializer().serializeToString(document.documentElement);
}

function enqueueRender<T>(task: () => Promise<T>): Promise<T> {
  const result = renderQueue.then(task, task);
  renderQueue = result.then(() => undefined, () => undefined);
  return result;
}

export async function renderMermaidSvg(
  source: string,
  id: string,
  theme: MermaidTheme,
): Promise<string> {
  const problem = mermaidSourceProblem(source);
  if (problem) throw new Error(problem);
  return enqueueRender(async () => {
    const { mermaid, DOMPurify } = await loadModules();
    mermaid.initialize(configForTheme(theme));
    const { svg } = await mermaid.render(id, source);
    return sanitizeSvg(svg, DOMPurify);
  });
}

/** Mermaid emits width="100%" SVGs for responsive inline layout. Replaced
 * image elements need concrete intrinsic dimensions, so the isolated preview
 * copy takes its dimensions from the already-sanitized viewBox. */
export function mermaidPreviewSvg(svg: string): string {
  const document = new DOMParser().parseFromString(svg, "image/svg+xml");
  const root = document.documentElement;
  const viewBox = root.getAttribute("viewBox")?.trim().split(/[\s,]+/)
    .map((value) => Number(value));
  if (document.querySelector("parsererror") || root.localName !== "svg"
      || !viewBox || viewBox.length !== 4
      || !Number.isFinite(viewBox[2]) || !Number.isFinite(viewBox[3])
      || viewBox[2] <= 0 || viewBox[3] <= 0) {
    throw new Error("Mermaid 预览尺寸无效");
  }
  removeRootLayoutStyles(root);
  root.setAttribute("width", String(viewBox[2]));
  root.setAttribute("height", String(viewBox[3]));
  return new XMLSerializer().serializeToString(root);
}

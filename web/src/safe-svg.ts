import type { DOMPurify as DOMPurifyInstance } from "dompurify";

export const MAX_SAFE_SVG_SOURCE_CHARS = 4 * 1024 * 1024;
export const MAX_SAFE_SVG_ELEMENTS = 10_000;
export const MAX_SAFE_SVG_DEPTH = 64;

const UNSAFE_CSS =
  /(?:@import|expression\s*\(|javascript:|url\s*\(\s*(?!['"]?#))/i;
const URL_ATTRIBUTES = new Set(["href", "xlink:href", "src"]);

export function removeSvgRootLayoutStyles(
  root: Element & ElementCSSInlineStyle,
): void {
  root.style.removeProperty("width");
  root.style.removeProperty("height");
  root.style.removeProperty("max-width");
  root.style.removeProperty("max-height");
  if (!root.getAttribute("style")?.trim()) root.removeAttribute("style");
}

function assertSvgComplexity(root: Element): void {
  const elements = [root, ...root.querySelectorAll("*")];
  if (elements.length > MAX_SAFE_SVG_ELEMENTS) {
    throw new Error("SVG 元素过多");
  }
  const stack: Array<{ element: Element; depth: number }> = [
    { element: root, depth: 1 },
  ];
  while (stack.length) {
    const current = stack.pop()!;
    if (current.depth > MAX_SAFE_SVG_DEPTH) {
      throw new Error("SVG 嵌套过深");
    }
    for (const child of Array.from(current.element.children)) {
      stack.push({ element: child, depth: current.depth + 1 });
    }
  }
}

export function sanitizeSvgMarkup(
  svg: string,
  DOMPurify: DOMPurifyInstance,
  options: { removeRootLayout?: boolean } = {},
): string {
  if (!svg.trim()) throw new Error("SVG 内容为空");
  if (svg.length > MAX_SAFE_SVG_SOURCE_CHARS) {
    throw new Error("SVG 超过 4 MiB 安全限制");
  }
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
  const document = new DOMParser().parseFromString(
    namespaced,
    "image/svg+xml",
  );
  const root = document.documentElement;
  if (document.querySelector("parsererror") || root.localName !== "svg") {
    throw new Error("SVG 根元素无效");
  }
  assertSvgComplexity(root);
  if (options.removeRootLayout) removeSvgRootLayoutStyles(root);

  for (const anchor of Array.from(root.querySelectorAll("a"))) {
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    for (const attribute of Array.from(anchor.attributes)) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") || URL_ATTRIBUTES.has(name)
          || name === "target") continue;
      group.setAttribute(attribute.name, attribute.value);
    }
    while (anchor.firstChild) group.append(anchor.firstChild);
    anchor.replaceWith(group);
  }

  for (const element of [root, ...root.querySelectorAll("*")]) {
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim();
      const internalReference = (name === "href" || name === "xlink:href")
        && /^#[a-zA-Z_][\w:.-]*$/.test(value);
      if (name.startsWith("on")
          || (URL_ATTRIBUTES.has(name) && !internalReference)) {
        element.removeAttribute(attribute.name);
      } else if (name === "style" && UNSAFE_CSS.test(value)) {
        element.removeAttribute(attribute.name);
      }
    }
  }
  for (const style of root.querySelectorAll("style")) {
    if (UNSAFE_CSS.test(style.textContent || "")) style.remove();
  }
  return new XMLSerializer().serializeToString(root);
}

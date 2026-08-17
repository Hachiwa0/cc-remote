const STATIC_PREVIEW_CSP =
  "default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; "
  + "font-src data:; object-src 'none'; frame-src 'none'; form-action 'none'; "
  + "base-uri 'none'";
const INTERACTIVE_PREVIEW_CSP =
  STATIC_PREVIEW_CSP + "; script-src 'unsafe-inline'";

const BASE_STYLE = "html{color-scheme:light dark}*{box-sizing:border-box}"
  + "body{margin:0;padding:18px;color:#25231f;background:#fff;font:15px/1.65 "
  + "system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
  + "overflow-wrap:anywhere}img,svg,canvas,video{max-width:100%;height:auto}"
  + "table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse}"
  + "pre{max-width:100%;white-space:pre-wrap;overflow-wrap:anywhere}"
  + "a{color:#6256b4}@media(prefers-color-scheme:dark){body{color:#e8e6ee;"
  + "background:#15151d}a{color:#aaa4ff}}";

function documentWithCsp(
  head: string,
  body: string,
  csp: string,
  viewport: string,
): string {
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="viewport" content="${viewport}"><style>${BASE_STYLE}</style>${head}</head><body>${body}</body></html>`;
}

export function buildSandboxDocument(
  body: string,
  head = "",
  viewport = "width=device-width,initial-scale=1",
): string {
  return documentWithCsp(head, body, STATIC_PREVIEW_CSP, viewport);
}

export function buildInteractiveSandboxDocument(
  body: string,
  head = "",
): string {
  return documentWithCsp(
    head, body, INTERACTIVE_PREVIEW_CSP, "width=device-width,initial-scale=1",
  );
}

export function decodePreviewHtmlData(data: string): string {
  const binary = globalThis.atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}

export function officePreviewViewport(source: string): string {
  const parsed = new DOMParser().parseFromString(source, "text/html");
  const content = parsed.querySelector('meta[name="viewport"]')
    ?.getAttribute("content") || "";
  const match = /(?:^|,)\s*width\s*=\s*(\d{2,4})(?:\s*,|$)/i.exec(content);
  if (!match) return "width=device-width,initial-scale=1";
  const width = Number(match[1]);
  if (!Number.isSafeInteger(width) || width < 240 || width > 4096) {
    return "width=device-width,initial-scale=1";
  }
  return `width=${width}`;
}

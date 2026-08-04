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

function documentWithCsp(head: string, body: string, csp: string): string {
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${BASE_STYLE}</style>${head}</head><body>${body}</body></html>`;
}

export function buildSandboxDocument(body: string, head = ""): string {
  return documentWithCsp(head, body, STATIC_PREVIEW_CSP);
}

export function buildInteractiveSandboxDocument(
  body: string,
  head = "",
): string {
  return documentWithCsp(head, body, INTERACTIVE_PREVIEW_CSP);
}

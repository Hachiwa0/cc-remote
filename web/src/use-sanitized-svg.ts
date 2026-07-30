import { useEffect, useState } from "react";
import { sanitizeSvgMarkup } from "./safe-svg.ts";

function decodeBase64Utf8(data: string): string {
  const binary = window.atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}

export function useSanitizedSvgUrl(
  data?: string,
  mediaType?: string,
): { url: string | null; error: string | null; loading: boolean } {
  const [state, setState] = useState<{
    key: string;
    url: string | null;
    error: string | null;
  }>({ key: "", url: null, error: null });
  const key = mediaType === "image/svg+xml" && data
    ? `${mediaType}:${data}`
    : "";

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    if (!key || !data) {
      setState({ key: "", url: null, error: null });
      return;
    }
    const prepare = async () => {
      try {
        const [{ default: DOMPurify }] = await Promise.all([
          import("dompurify"),
        ]);
        const safe = sanitizeSvgMarkup(decodeBase64Utf8(data), DOMPurify);
        objectUrl = URL.createObjectURL(
          new Blob([safe], { type: "image/svg+xml" }),
        );
        if (cancelled) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        setState({ key, url: objectUrl, error: null });
      } catch {
        if (!cancelled) {
          setState({ key, url: null, error: "SVG 安全处理失败" });
        }
      }
    };
    void prepare();
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [data, key]);

  return {
    url: state.key === key ? state.url : null,
    error: state.key === key ? state.error : null,
    loading: !!key && state.key !== key,
  };
}

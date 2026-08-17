const MAX_PDF_CANVAS_PIXELS = 12 * 1024 * 1024;

export function decodeBase64Bytes(data: string): Uint8Array<ArrayBuffer> {
  const binary = globalThis.atob(data);
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

export function residentPdfPages(page: number, pageCount: number): number[] {
  if (!Number.isSafeInteger(page) || !Number.isSafeInteger(pageCount)
      || pageCount < 1) return [];
  const current = Math.min(pageCount, Math.max(1, page));
  const pages = [current];
  if (current > 1) pages.unshift(current - 1);
  if (current < pageCount) pages.push(current + 1);
  return pages;
}

export function boundedPdfOutputScale(
  cssWidth: number,
  cssHeight: number,
  deviceScale: number,
): number {
  if (!(cssWidth > 0) || !(cssHeight > 0)) return 1;
  const scale = Math.min(2, Math.max(1, deviceScale || 1));
  return Math.min(
    scale,
    Math.sqrt(MAX_PDF_CANVAS_PIXELS / (cssWidth * cssHeight)),
  );
}

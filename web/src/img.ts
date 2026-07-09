import type { QueryImg } from "./protocol";

// Downscale an uploaded image so its long edge is <= IMG_MAX_EDGE before it's
// sent — shrinks the upload, the wire frame, the model's vision tokens (large
// images time out / cost more), AND the base64 that gets replayed in history.
// PNG (screenshots) stays PNG so text stays crisp; anything else -> JPEG.
// Shared by the composer and the new-chat page.
export const IMG_MAX_EDGE = 1568;

export function downscaleImage(f: File): Promise<QueryImg> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result as string;
      const raw = (): QueryImg => ({ media_type: f.type || "image/png", data: (dataUrl.split(",", 2)[1]) || "" });
      const img = new Image();
      img.onload = () => {
        const long = Math.max(img.width, img.height);
        if (!long || long <= IMG_MAX_EDGE) { resolve(raw()); return; }
        const scale = IMG_MAX_EDGE / long;
        const w = Math.round(img.width * scale), h = Math.round(img.height * scale);
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) { resolve(raw()); return; }
        ctx.drawImage(img, 0, 0, w, h);
        const mt = f.type === "image/png" ? "image/png" : "image/jpeg";
        try {
          const out = canvas.toDataURL(mt, 0.85);
          resolve({ media_type: mt, data: (out.split(",", 2)[1]) || "" });
        } catch { resolve(raw()); }
      };
      img.onerror = () => resolve(raw());
      img.src = dataUrl;
    };
    reader.onerror = () => resolve({ media_type: f.type || "image/png", data: "" });
    reader.readAsDataURL(f);
  });
}

// Split a FileList into images (downscaled) and other files (base64), invoking
// the callbacks as each finishes. Shared by the composer and new-chat page so
// both handle drag/drop/paste/pick identically.
export function pickFiles(
  fl: FileList | File[] | null,
  onImage: (img: QueryImg) => void,
  onFile: (file: { filename: string; data: string }) => void,
): void {
  if (!fl) return;
  Array.from(fl).forEach((f) => {
    if (f.type.startsWith("image/")) {
      downscaleImage(f).then((img) => { if (img.data) onImage(img); });
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const base64 = ((reader.result as string).split(",", 2)[1]) || "";
      onFile({ filename: f.name, data: base64 });
    };
    reader.readAsDataURL(f);
  });
}

import { useEffect, useRef } from "react";

import type { Turn } from "../domain/conversation";
import {
  historyImageDisplaySource,
} from "../turn-image-previews";
import type {
  HistoryImageAsset,
  HistoryImageVariant,
} from "../history-image-assets";

export function HistoryUserImage({
  turnId,
  imageId,
  width,
  height,
  asset,
  fallback,
  onLoad,
  onPreview,
}: {
  turnId: string;
  imageId: string;
  width: number;
  height: number;
  asset?: HistoryImageAsset;
  fallback?: NonNullable<Turn["images"]>[number];
  onLoad?: (
    turnId: string,
    imageId: string,
    variant: HistoryImageVariant,
  ) => boolean;
  onPreview: () => void;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (asset || !onLoad) return;
    const node = triggerRef.current;
    if (!node || typeof IntersectionObserver === "undefined") {
      onLoad(turnId, imageId, "thumbnail");
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      onLoad(turnId, imageId, "thumbnail");
      observer.disconnect();
    }, { rootMargin: "500px 0px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [asset, imageId, onLoad, turnId]);

  const src = historyImageDisplaySource(asset, fallback);
  return (
    <button ref={triggerRef} type="button"
      className="ubub-image-trigger history-image-trigger"
      style={{ aspectRatio: `${width} / ${height}` }}
      aria-label="预览用户发送的图片"
      disabled={!src}
      onClick={onPreview}>
      {src
        ? <img src={src} className="ubub-img" alt="用户发送的图片" />
        : <span className="history-image-placeholder" aria-hidden="true" />}
    </button>
  );
}

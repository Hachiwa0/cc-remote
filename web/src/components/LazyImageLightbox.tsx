import {
  useEffect,
  useState,
  type ComponentType,
} from "react";
import { Icon } from "../icons";
import type { ImageLightboxProps } from "./ImageLightbox";

type ImageLightboxComponent = ComponentType<ImageLightboxProps>;

let loadedImageLightbox: ImageLightboxComponent | null = null;
let imageLightboxRequest: Promise<ImageLightboxComponent> | null = null;

function loadImageLightbox(): Promise<ImageLightboxComponent> {
  if (loadedImageLightbox) return Promise.resolve(loadedImageLightbox);
  imageLightboxRequest ??= import("./ImageLightbox").then((module) => {
    loadedImageLightbox = module.ImageLightbox;
    return loadedImageLightbox;
  }).finally(() => {
    imageLightboxRequest = null;
  });
  return imageLightboxRequest;
}

// Fetch the immutable chunk after the initial shell is interactive. An
// already-open PWA can then survive a later atomic release switch without
// requesting an old hash from the new static root, while keeping preview code
// out of the critical startup request budget.
if (typeof window !== "undefined") {
  const prefetch = () => { void loadImageLightbox().catch(() => {}); };
  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(prefetch, { timeout: 2_000 });
  } else {
    window.setTimeout(prefetch, 1_000);
  }
}

export function LazyImageLightbox(props: ImageLightboxProps) {
  const { onClose } = props;
  const [Lightbox, setLightbox] = useState<ImageLightboxComponent | null>(
    () => loadedImageLightbox,
  );
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (Lightbox) return;
    let active = true;
    setFailed(false);
    void loadImageLightbox().then((component) => {
      if (active) setLightbox(() => component);
    }, () => {
      if (active) setFailed(true);
    });
    return () => { active = false; };
  }, [Lightbox]);

  useEffect(() => {
    if (Lightbox) return;
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [Lightbox, onClose]);

  if (Lightbox) return <Lightbox {...props} />;

  return <div className="image-lightbox entered" role="dialog"
    aria-modal="true" aria-busy={!failed}
    aria-label={props.dialogLabel ?? "图片预览"}
    onClick={() => props.onClose()}>
    {failed && <div className="image-lightbox-load-error" role="alert"
      onClick={(event) => event.stopPropagation()}>
      <strong>预览资源加载失败</strong>
      <span>网络恢复后或刚完成部署时，请重新载入页面。</span>
      <div className="image-lightbox-load-actions">
        <button type="button" onClick={() => window.location.reload()}>
          重新载入
        </button>
      </div>
    </div>}
    <button type="button" autoFocus className="image-lightbox-close"
      aria-label={props.closeLabel ?? "关闭图片预览"}
      onClick={(event) => {
        event.stopPropagation();
        props.onClose();
      }}>
      <Icon name="close" size={22} />
    </button>
  </div>;
}

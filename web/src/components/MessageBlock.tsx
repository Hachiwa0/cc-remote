import { isValidElement, useEffect, useMemo, useRef, useState,
  type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { parseLocalFileTarget } from "../file-link";
import { Icon } from "../icons";
import {
  classifyMessageImageTarget,
  type InlineImageAsset,
} from "../inline-image-assets";

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return "";
}

function CopyableCodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const code = nodeText(children).replace(/\n$/, "");
  const copy = () => {
    void navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  };
  return (
    <div className="message-code-block">
      <button type="button" className={"message-code-copy" + (copied ? " copied" : "")}
        onClick={copy} aria-label="复制代码" title={copied ? "已复制" : "复制代码"}>
        <Icon name={copied ? "check" : "copy"} size={13} />
        <span>{copied ? "已复制" : "复制"}</span>
      </button>
      <pre>{children}</pre>
    </div>
  );
}

function MessageImage({ src, alt, title, asset, onLoadImage, onPreviewImage }: {
  src: string;
  alt?: string;
  title?: string;
  asset?: InlineImageAsset;
  onLoadImage?: (path: string) => boolean;
  onPreviewImage?: (src: string, alt: string) => void;
}) {
  const target = useMemo(() => classifyMessageImageTarget(src), [src]);
  const [blocked, setBlocked] = useState(false);

  useEffect(() => {
    setBlocked(false);
    if (target.kind !== "local" || asset || !onLoadImage) return;
    setBlocked(!onLoadImage(target.value));
  }, [asset, onLoadImage, target]);

  if (target.kind === "blocked") {
    return <span className="message-image-error">图片不可用</span>;
  }
  if (target.kind === "local" && asset?.status === "error") {
    return <span className="message-image-error">图片不可用</span>;
  }
  if (target.kind === "local" && (blocked || (!onLoadImage && !asset))) {
    return <span className="message-image-error">图片暂时无法加载</span>;
  }

  const resolved = target.kind === "external"
    ? target.value
    : asset?.status === "ready" && asset.data && asset.mediaType
      ? `data:${asset.mediaType};base64,${asset.data}`
      : null;
  if (!resolved) {
    return <span className="message-image-loading" role="status">
      <span className="thinking"><span/><span/><span/></span>
      {alt || "正在加载图片"}
    </span>;
  }

  const image = <img className="message-inline-image" src={resolved}
    alt={alt || ""} title={title} loading="lazy" referrerPolicy="no-referrer" />;
  if (!onPreviewImage) return image;
  return <button type="button" className="message-image-trigger"
    aria-label={`预览图片${alt ? `：${alt}` : ""}`}
    onClick={() => onPreviewImage(resolved, alt || "图片预览")}>{image}</button>;
}

// Streams markdown with a ~50ms throttle: re-parsing react-markdown on every
// token delta is wasteful, so we hold a "shown" buffer that catches up on a
// timer while streaming, and snaps to the full text when the block is done.
export function MessageBlock({ text, done, onOpenFile, imageAssets,
  onLoadImage, onPreviewImage }: {
  text: string;
  done: boolean;
  onOpenFile?: (path: string, line?: number) => void;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string) => boolean;
  onPreviewImage?: (src: string, alt: string) => void;
}) {
  const [shown, setShown] = useState(text);
  const latest = useRef(text);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    latest.current = text;
    if (done) {
      if (timer.current) { clearTimeout(timer.current); timer.current = null; }
      setShown(text);
      return;
    }
    if (timer.current) return; // a catch-up is already scheduled
    timer.current = setTimeout(() => {
      setShown(latest.current);
      timer.current = null;
    }, 50);
  }, [text, done]);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const components = useMemo<Components>(() => ({
    pre: ({ children }) => <CopyableCodeBlock>{children}</CopyableCodeBlock>,
    img: ({ src, alt, title }) => {
      const source = typeof src === "string" ? src : "";
      const target = classifyMessageImageTarget(source);
      const asset = target.kind === "local" ? imageAssets?.[target.value] : undefined;
      return <MessageImage src={source} alt={alt} title={title} asset={asset}
        onLoadImage={onLoadImage} onPreviewImage={onPreviewImage} />;
    },
    a: ({ href = "", children, title }) => {
      const file = parseLocalFileTarget(href);
      if (file && onOpenFile) {
        const location = file.line ? `${file.path}:${file.line}` : file.path;
        return <button type="button" className="message-file-link"
          title={`在 Remote 中打开 ${location}`}
          onClick={() => onOpenFile(file.path, file.line)}>{children}</button>;
      }
      if (/^https?:\/\//i.test(href) || /^mailto:/i.test(href)) {
        return <a href={href} target="_blank" rel="noopener noreferrer"
          title={title}>{children}</a>;
      }
      if (href.startsWith("#")) return <a href={href} title={title}>{children}</a>;
      return <span className="message-link-disabled"
        title="该链接无法在当前会话中打开">{children}</span>;
    },
  }), [imageAssets, onLoadImage, onOpenFile, onPreviewImage]);

  if (!shown) return null;
  return (
    <div className="prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{shown}</ReactMarkdown>
      {!done && <span className="cursor" />}
    </div>
  );
}

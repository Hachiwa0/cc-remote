import { isValidElement, useEffect, useMemo, useRef, useState,
  type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { parseLocalFileTarget } from "../file-link";
import { Icon } from "../icons";

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

// Streams markdown with a ~50ms throttle: re-parsing react-markdown on every
// token delta is wasteful, so we hold a "shown" buffer that catches up on a
// timer while streaming, and snaps to the full text when the block is done.
export function MessageBlock({ text, done, onOpenFile }: {
  text: string;
  done: boolean;
  onOpenFile?: (path: string, line?: number) => void;
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
  }), [onOpenFile]);

  if (!shown) return null;
  return (
    <div className="prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{shown}</ReactMarkdown>
      {!done && <span className="cursor" />}
    </div>
  );
}

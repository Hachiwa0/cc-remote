import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Streams markdown with a ~50ms throttle: re-parsing react-markdown on every
// token delta is wasteful, so we hold a "shown" buffer that catches up on a
// timer while streaming, and snaps to the full text when the block is done.
export function MessageBlock({ text, done }: { text: string; done: boolean }) {
  const [shown, setShown] = useState(text);
  const latest = useRef(text);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    latest.current = text;
    if (done) {
      if (timer.current) {
        clearTimeout(timer.current);
        timer.current = null;
      }
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

  if (!shown) return null;
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{shown}</ReactMarkdown>
      {!done && <span className="cursor">▋</span>}
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import type { Turn } from "../reducer";
import { MessageBlock } from "./MessageBlock";
import { ToolCallCard } from "./ToolCallCard";
import { Icon } from "../icons";

function formatTime(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function ChatView({ turns, onEdit, onGetDiff }: { turns: Turn[]; onEdit: (prompt: string) => void; onGetDiff: (file: string) => void }) {
  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    // "at bottom" = within 80px of the bottom edge
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    setAtBottom(dist < 80);
  };

  // Auto-scroll to bottom on new turns only if the user is already there
  // (so reading older history isn't yanked away by streaming deltas).
  useEffect(() => {
    if (atBottom) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, atBottom]);

  const scrollToBottom = () => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
    setAtBottom(true);
  };

  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copyText = (id: string, text: string) => {
    navigator.clipboard?.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 1500);
  };
  const aiText = (t: Turn) =>
    t.blocks.filter((b) => b.kind === "text").map((b) => (b as { text: string }).text).join("\n\n");

  // collect the file_paths this turn mutated (Edit/Write) — a summary button +
  // a list of file chips. Click summary => all diffs; click a file => that file.
  const fileChips = (t: Turn) => {
    const files = new Set<string>();
    t.blocks.forEach((b) => {
      if (b.kind === "tool" && (b.tool === "Edit" || b.tool === "Write")) {
        const fp = (b.input as { file_path?: string }).file_path;
        if (fp) files.add(fp);
      }
    });
    if (!files.size) return null;
    const arr = [...files];
    return (
      <div className="turn-files">
        <button className="turn-files-summary" onClick={() => onGetDiff("")}>
          <Icon name="edit" size={13} />改动 {arr.length} 个文件
        </button>
        <div className="turn-files-list">
          {arr.map((f) => (
            <button key={f} className="turn-file-chip" onClick={() => onGetDiff(f)} title={f}>
              {f.split("/").pop()}
            </button>
          ))}
        </div>
      </div>
    );
  };

  if (turns.length === 0) {
    return (
      <div className="empty">
        <div className="glyph"><Icon name="spark" size={28} /></div>
        <h2>已连接</h2>
        <p>发一条消息开始，或用 <code>/</code> 唤起命令面板（Plan mode、review、技能…）。</p>
      </div>
    );
  }

  return (
    <div className="thread" ref={scrollRef} onScroll={onScroll}>
      <div className="thread-in">
        {turns.map((t) => (
          <div className="turn" key={t.id}>
            {(t.prompt || (t.images && t.images.length) || (t.files && t.files.length)) && (
              <div className="ubub-wrap">
                {t.prompt && <div className="ubub">{t.prompt}</div>}
                {t.images && t.images.length > 0 && (
                  <div className="ubub-imgs">
                    {t.images.map((img, i) => (
                      <img key={i} src={`data:${img.media_type};base64,${img.data}`} className="ubub-img" alt="" />
                    ))}
                  </div>
                )}
                {t.files && t.files.length > 0 && (
                  <div className="ubub-files">
                    {t.files.map((f, i) => (
                      <span key={i} className="ubub-file"><Icon name="read" size={14} />{f.filename}</span>
                    ))}
                  </div>
                )}
                <div className="ubub-meta">
                  {t.ts && <span className="ubub-time">{formatTime(t.ts)}</span>}
                  {t.prompt && <button className="ubub-act" onClick={() => onEdit(t.prompt!)} aria-label="编辑"><Icon name="edit" size={13} /></button>}
                  {t.prompt && <button className={"ubub-act" + (copiedId === t.id ? " copied" : "")} onClick={() => copyText(t.id, t.prompt!)} aria-label="复制"><Icon name="check" size={13} /></button>}
                </div>
              </div>
            )}
            {t.blocks.length > 0 ? (
              <>
                <div className="arole">
                  <span className="av"><Icon name="spark" size={14} /></span>
                  <span className="nm">Claude</span>
                </div>
                {t.blocks.map((b) =>
                  b.kind === "text" ? (
                    <MessageBlock key={b.message_id} text={b.text} done={b.done} />
                  ) : (
                    <ToolCallCard key={b.tool_use_id} block={b} />
                  )
                )}
                {t.done && (
                  <div className="ubub-meta ai-meta">
                    {t.doneTs && <span className="ubub-time">{formatTime(t.doneTs)}</span>}
                    <button className={"ubub-act" + (copiedId === t.id + "-ai" ? " copied" : "")} onClick={() => copyText(t.id + "-ai", aiText(t))} aria-label="复制"><Icon name="check" size={13} /></button>
                  </div>
                )}
              </>
            ) : (!t.done && t.prompt) ? (
              <div className="arole">
                <span className="av"><Icon name="spark" size={14} /></span>
                <span className="nm">Claude<span className="thinking"><span/><span/><span/></span></span>
              </div>
            ) : null}
            {fileChips(t)}
            {t.interrupted && <div className="note interrupted">— 已打断 —</div>}
            {t.error && <div className="note interrupted">{t.error}</div>}
          </div>
        ))}
        <div ref={endRef} />
      </div>
      {!atBottom && (
        <div className="scroll-bottom-wrap">
          <button className="scroll-bottom-btn" onClick={scrollToBottom} aria-label="滚动到底部">
            <Icon name="chev" size={20} />
          </button>
        </div>
      )}
    </div>
  );
}

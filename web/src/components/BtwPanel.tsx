import { useRef, useState } from "react";
import { ChatView } from "./ChatView";
import { Icon } from "../icons";
import { PanelTabs } from "./PanelTabs";
import type { SessionRuntime } from "../reducer";

/** /btw side panel: a mini chat over an ephemeral fork of the current session.
 * Reuses ChatView for the transcript; a minimal textarea for input. Closing
 * discards the fork (the main thread never sees any of this). */
export function BtwPanel({ sid, rt, engine, active, hasDiff, onTab, onSend, onClose }: {
  sid: string;
  rt: SessionRuntime | undefined;
  engine?: string;
  active: "diff" | "btw";
  hasDiff: boolean;
  onTab: (v: "diff" | "btw") => void;
  onSend: (prompt: string) => void;
  onClose: () => void;
}) {
  const [text, setText] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);
  const turns = rt?.turns ?? [];
  const busy = rt?.state === "running";

  const send = () => {
    const t = text.trim();
    if (!t || busy) return;
    onSend(t);
    setText("");
    if (taRef.current) taRef.current.style.height = "auto";
  };
  const grow = (el: HTMLTextAreaElement) => { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 120) + "px"; };

  return (
    <div className="btw-panel">
      <div className="btw-head">
        {hasDiff
          ? <PanelTabs active={active} onTab={onTab} />
          : <div className="btw-titles">
              <span className="btw-title">btw · 侧边对话{engine === "codex" ? " · Codex" : ""}</span>
              <span className="btw-sub">基于当前会话上下文,不写回主线</span>
            </div>}
        <button className="iconbtn" onClick={onClose} aria-label="关闭 btw" title="关闭并丢弃这个侧边对话">
          <Icon name="chevrons-right" />
        </button>
      </div>
      <div className="btw-body">
        {turns.length === 0
          ? <div className="btw-empty">问一个基于当前会话的侧边问题 —— 回答不会写进主线,关闭即丢弃。</div>
          : <ChatView sid={sid} turns={turns} onEdit={() => {}} onGetDiff={() => {}} />}
      </div>
      <div className="btw-input">
        <textarea
          ref={taRef}
          value={text}
          placeholder={busy ? "回答中…" : "问点什么(Enter 发送 · Shift+Enter 换行)"}
          rows={1}
          onChange={(e) => { setText(e.target.value); grow(e.target); }}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
        />
        <button className="btw-send" onClick={send} disabled={busy || !text.trim()} aria-label="发送">
          <Icon name="send" size={18} />
        </button>
      </div>
    </div>
  );
}

// Empty-state "new chat" page: a centered composer (a la Claude app / Codex)
// with the working directory shown above it. The first message triggers
// new_session(cwd) on the host; once that session is ready the pending query
// is fired and the normal chat UI takes over.
import { useState } from "react";
import { Icon } from "../icons";

interface Props {
  cwd: string;
  creating: boolean;  // true while the new session is being created (pendingNewQuery set)
  engine?: "claude" | "codex";  // which backend this new chat will use
  onPickCwd: () => void;  // open the directory picker
  onSend: (prompt: string) => void;
}

export function NewChatView({ cwd, creating, engine = "claude", onPickCwd, onSend }: Props) {
  const [text, setText] = useState("");
  const send = () => {
    const p = text.trim();
    if (!p || creating) return;
    onSend(p);
  };
  return (
    <div className="newchat">
      <div className="newchat-card">
        <div className="newchat-greet">{engine === "codex" ? "开始 Codex 新对话" : "开始新对话"}
          <span className={`newchat-engine ${engine}`}>{engine === "codex" ? "◇ Codex" : "✳ Claude"}</span>
        </div>
        <button className="newchat-cwd" onClick={onPickCwd} title="更改工作目录" disabled={creating}>
          <Icon name="folder" size={16} />
          <span className="newchat-cwd-path">{cwd || "未指定目录"}</span>
          <Icon name="edit" size={13} />
        </button>
        <textarea className="newchat-input" placeholder="发条消息开始…"
          value={text} onChange={(e) => setText(e.target.value)} autoFocus rows={3}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <div className="newchat-foot">
          <span className="newchat-hint">{creating ? "正在创建会话…" : "Enter 发送"}</span>
          <button className="newchat-send" onClick={send} disabled={!text.trim() || creating}>
            <Icon name="send" size={16} />开始
          </button>
        </div>
      </div>
    </div>
  );
}

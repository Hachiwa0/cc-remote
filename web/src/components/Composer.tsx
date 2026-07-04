import type { ConnState } from "../ws";

interface Props {
  input: string;
  setInput: (s: string) => void;
  onSend: () => void;
  onStop: () => void;
  busy: boolean;
  canSend: boolean;
  connState: ConnState;
}

export function Composer({ input, setInput, onSend, onStop, busy, canSend, connState }: Props) {
  const placeholder = canSend
    ? "发消息… (Enter 发送, Shift+Enter 换行)"
    : connState === "connected"
      ? "工作中…"
      : "未连接…";
  return (
    <div className="composer">
      <textarea
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onSend();
          }
        }}
        placeholder={placeholder}
        rows={1}
        disabled={!canSend && !busy}
      />
      {busy ? (
        <button className="btn stop" onClick={onStop}>停止</button>
      ) : (
        <button className="btn send" onClick={onSend} disabled={!canSend}>发送</button>
      )}
    </div>
  );
}

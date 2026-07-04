import { useEffect, useRef } from "react";
import type { Turn } from "../reducer";
import { MessageBlock } from "./MessageBlock";
import { ToolCallCard } from "./ToolCallCard";

export function ChatView({ turns }: { turns: Turn[] }) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  if (turns.length === 0) {
    return <div className="chat empty">已连接，等待你的第一条消息…</div>;
  }
  return (
    <div className="chat">
      {turns.map((t) => (
        <div className="turn" key={t.id}>
          {t.prompt && <div className="bubble user">{t.prompt}</div>}
          <div className="assistant">
            {t.blocks.map((b) =>
              b.kind === "text" ? (
                <MessageBlock key={b.message_id} text={b.text} done={b.done} />
              ) : (
                <ToolCallCard key={b.tool_use_id} block={b} />
              )
            )}
            {t.interrupted && <div className="interrupted">— 已打断 —</div>}
            {t.error && <div className="err">{t.error}</div>}
          </div>
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}

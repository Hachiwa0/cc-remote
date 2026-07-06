import { useEffect, useState } from "react";
import type { ToolBlock } from "../reducer";
import { Icon } from "../icons";
import { ToolCallCard } from "./ToolCallCard";

/** Collapsible group for consecutive tool calls within a turn (Claude-app
 * style: a gray summary line "N 个工具调用 · Bash ×2 · Edit ×1" that expands to
 * the individual tool cards). Auto-expands while the turn is running and
 * auto-collapses when the turn completes. */
export function ToolGroup({ tools, done }: { tools: ToolBlock[]; done: boolean }) {
  const running = tools.some((t) => !t.result);
  const hasErr = tools.some((t) => t.result?.is_error);
  const [open, setOpen] = useState(!done);
  useEffect(() => { if (done) setOpen(false); }, [done]);

  const counts: Record<string, number> = {};
  tools.forEach((t) => { counts[t.tool] = (counts[t.tool] || 0) + 1; });
  const sub = Object.entries(counts).map(([n, c]) => (c > 1 ? `${n} ×${c}` : n)).join(" · ");

  return (
    <details className="tool-group" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="tool-group-h">
        <span className={"tool-group-ic" + (running ? " running" : "")}>
          {running ? <span className="spin-dot" /> : <Icon name="verify" size={13} />}
        </span>
        <span className="tool-group-nm">
          {running ? `正在调用 ${tools.length} 个工具` : `${tools.length} 个工具调用`}
          {hasErr && !running && <span className="tool-group-err"> · 有错</span>}
        </span>
        <span className="tool-group-sub">{sub}</span>
        <span className="tool-group-chev"><Icon name="chev" size={14} sw={2} /></span>
      </summary>
      <div className="tool-group-b">
        {tools.map((t) => <ToolCallCard key={t.tool_use_id} block={t} />)}
      </div>
    </details>
  );
}

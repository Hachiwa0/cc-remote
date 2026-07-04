import { useState } from "react";
import type { ToolBlock } from "../reducer";

export function ToolCallCard({ block }: { block: ToolBlock }) {
  const [open, setOpen] = useState(false);
  const status = block.result ? (block.result.is_error ? "error" : "done") : "running";
  return (
    <div className="tool-card">
      <button className="tool-head" onClick={() => setOpen((o) => !o)}>
        <span className={`tool-status ${status}`}>●</span>
        <span className="tool-name">{block.tool}</span>
        <span className="tool-chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="tool-body">
          <div className="tool-label">input</div>
          <pre className="tool-input">{JSON.stringify(block.input, null, 2)}</pre>
          {block.result && (
            <>
              <div className="tool-label">output{block.result.is_error ? " (error)" : ""}</div>
              <pre className={`tool-output ${block.result.is_error ? "err" : ""}`}>
                {block.result.content}
                {block.result.truncated && "\n…(truncated)"}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

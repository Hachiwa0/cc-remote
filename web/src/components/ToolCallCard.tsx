import type { ToolBlock } from "../reducer";
import { Icon } from "../icons";

const TOOL_IC: Record<string, string> = { Read: "read", Bash: "bash", Edit: "edit", Grep: "bash" };

function argPreview(input: Record<string, unknown>): string {
  const v = input.file_path || input.command || input.pattern || input.path || input.url || "";
  return String(v).slice(0, 80);
}

export function ToolCallCard({ block }: { block: ToolBlock }) {
  const status = block.result ? (block.result.is_error ? "err" : "done") : "run";
  return (
    <details className="tool">
      <summary className="tool-h">
        <span className="tool-ic"><Icon name={TOOL_IC[block.tool] || "bash"} size={15} /></span>
        <span className="tool-nm">{block.tool}</span>
        <span className="tool-arg">{argPreview(block.input)}</span>
        <span className={`tool-st ${status}`}>
          {status === "done" ? <Icon name="verify" size={16} />
            : status === "err" ? <Icon name="close" size={16} />
            : null}
        </span>
        <span className="tool-chev"><Icon name="chev" size={16} sw={2} /></span>
      </summary>
      <div className="tool-b">
        <div className="tool-lbl">输入</div>
        <pre className="tool-pre">{JSON.stringify(block.input, null, 2)}</pre>
        {block.result && (
          <>
            <div className="tool-lbl">输出{block.result.is_error ? " (error)" : ""}</div>
            <pre className="tool-pre">
              {block.result.content}
              {block.result.truncated && "\n…(truncated)"}
            </pre>
          </>
        )}
      </div>
    </details>
  );
}

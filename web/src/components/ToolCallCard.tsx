import type { ToolBlock } from "../reducer";
import { Icon } from "../icons";
import { diffLines } from "../diff";
import { isToolFailure, presentTool } from "../tool-presentation";

export function ToolCallCard({ block }: { block: ToolBlock }) {
  const status = !block.done ? "run" : isToolFailure(block) ? "err" : "done";
  const presentation = presentTool(block);
  const inp = block.input as { file_path?: string; old_string?: string; new_string?: string; content?: string };
  const isEdit = block.tool === "Edit";
  const isWrite = block.tool === "Write";
  const streamedOutput = block.output?.trim();
  const finalOutput = block.result?.content?.trim();
  const output = finalOutput || streamedOutput;
  const resultSummary = block.result?.summary?.trim();
  const diff = block.result?.diff || block.diff;
  const hasInput = Object.keys(block.input).length > 0;
  return (
    <details className="tool">
      <summary className="tool-h">
        <span className="tool-ic"><Icon name={presentation.icon} size={15} /></span>
        <span className="tool-nm">{presentation.title}</span>
        <span className="tool-arg">{presentation.subtitle}</span>
        <span className={`tool-st ${status}`}>
          {status === "done" ? <Icon name="verify" size={16} />
            : status === "err" ? <Icon name="close" size={16} />
            : null}
        </span>
        <span className="tool-chev"><Icon name="chev" size={16} sw={2} /></span>
      </summary>
      <div className="tool-b">
        {block.progress && <div className="tool-progress">{block.progress}</div>}
        {resultSummary && resultSummary !== output && (
          <div className="tool-progress">{resultSummary}</div>
        )}
        {isEdit ? (
          <>
            <div className="tool-lbl">{inp.file_path}</div>
            <pre className="diff-pre">
              {diffLines(inp.old_string || "", inp.new_string || "").map((l, i) => (
                <span key={i} className={"diff-" + l.type}>{(l.type === "add" ? "+" : l.type === "del" ? "−" : " ") + " " + l.text + "\n"}</span>
              ))}
            </pre>
          </>
        ) : isWrite ? (
          <>
            <div className="tool-lbl">{inp.file_path}</div>
            <pre className="tool-pre">{inp.content}</pre>
          </>
        ) : hasInput ? (
          <>
            <div className="tool-lbl">输入</div>
            <pre className="tool-pre">{JSON.stringify(block.input, null, 2)}</pre>
          </>
        ) : null}
        {diff && !isEdit && (
          <>
            <div className="tool-lbl">Diff</div>
            <pre className="tool-pre tool-diff">{diff}</pre>
          </>
        )}
        {output && (
          <>
            <div className="tool-lbl">输出{block.result?.is_error ? " (error)" : ""}</div>
            <pre className="tool-pre">
              {output}
              {block.result?.truncated && "\n…(truncated)"}
            </pre>
          </>
        )}
        {block.result && (block.result.exit_code != null || block.result.duration_ms != null) && (
          <div className="tool-meta">
            {block.result.exit_code != null && <span>exit {block.result.exit_code}</span>}
            {block.result.duration_ms != null && <span>{Math.max(0, block.result.duration_ms / 1000).toFixed(1)}s</span>}
          </div>
        )}
      </div>
    </details>
  );
}

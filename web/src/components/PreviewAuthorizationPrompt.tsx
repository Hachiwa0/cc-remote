import type { PreviewAuthorizationState } from "../reducer";

export function PreviewAuthorizationPrompt({
  authorization,
  compact = false,
  onDecision,
}: {
  authorization: PreviewAuthorizationState;
  compact?: boolean;
  onDecision?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
}) {
  const pending = authorization.status !== "required";
  return <div className={
    `preview-authorization${compact ? " compact" : ""}`
  } role="alert">
    <div className="preview-authorization-copy">
      <strong>允许本会话查看这个外部文件？</strong>
      {!compact && <span className="preview-authorization-requested"
        title={authorization.path}>请求路径：{authorization.path}</span>}
      <span className="preview-authorization-resolved"
        title={authorization.resolvedPath}>
        {compact ? authorization.resolvedPath
          : `实际路径：${authorization.resolvedPath}`}
      </span>
      {!compact && <small>仅授权当前会话读取这个文件；文件被替换后会再次确认。</small>}
    </div>
    <div className="preview-authorization-actions">
      <button type="button" disabled={pending || !onDecision}
        onClick={() => onDecision?.(authorization, "allow")}>
        {pending ? "处理中…" : "允许查看"}
      </button>
      <button type="button" className="secondary"
        disabled={pending || !onDecision}
        onClick={() => onDecision?.(authorization, "deny")}>取消</button>
    </div>
  </div>;
}

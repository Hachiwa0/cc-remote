import type { CodexFileCitation } from "../codex-file-citation";
import { Icon } from "../icons";
import { LocalFileLink } from "./LocalFileLink";

function citationKind(citation: CodexFileCitation): string {
  const kind = citation.artifactKind?.toLowerCase();
  if (kind === "presentation") return "演示文稿";
  if (kind === "document") return "文档";
  if (kind === "workbook" || kind === "spreadsheet") return "工作簿";
  if (kind === "pdf") return "PDF";
  const extension = /\.([^.\\/]+)$/.exec(citation.path)?.[1].toLowerCase();
  if (extension === "ppt" || extension === "pptx") return "PowerPoint";
  if (extension === "doc" || extension === "docx") return "Word";
  if (extension === "xls" || extension === "xlsx") return "Excel";
  return extension === "pdf" ? "PDF" : "文件";
}

export function CodexFileCitationCard({ citation, onOpenFile }: {
  citation: CodexFileCitation;
  onOpenFile?: (path: string, line?: number) => void;
}) {
  const fileName = citation.path.split(/[\\/]/).pop() || citation.path;
  const contents = <>
    <span className="message-file-citation-icon"><Icon name="read" size={15} /></span>
    <span className="message-file-citation-main">
      <span className="message-file-citation-name">{fileName}</span>
      <span className="message-file-citation-meta">{
        citation.purpose === "output" ? "已生成" : "源文件"
      } · {citationKind(citation)}</span>
    </span>
    {onOpenFile && <span className="message-file-citation-action">预览</span>}
  </>;
  if (!onOpenFile) {
    return <span className="message-file-citation disabled"
      title={citation.path}>{contents}</span>;
  }
  return <LocalFileLink location={citation.path}
    className="message-file-citation"
    onOpen={() => onOpenFile(citation.path)}>{contents}</LocalFileLink>;
}

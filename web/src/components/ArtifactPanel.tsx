import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useEffect, useRef } from "react";
import type { Artifact } from "../reducer";
import { Icon } from "../icons";

export function ArtifactPanel({ artifact, onClose }: { artifact: Artifact; onClose: () => void }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const sections = artifact.kind === "gitdiff" ? (artifact.sections || []) : [];
  const empty = artifact.kind === "gitdiff" && sections.length === 0;

  // Click anywhere outside the panel (the blank chat area) closes it — not only
  // the × button. mousedown so a text-selection drag that starts inside the
  // panel doesn't close it; deferred attach so the click that OPENED the panel
  // isn't the one that closes it.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) onClose();
    };
    const id = window.setTimeout(() => document.addEventListener("mousedown", onDown), 0);
    return () => { clearTimeout(id); document.removeEventListener("mousedown", onDown); };
  }, [onClose]);

  return (
    <div className="artifact-panel" ref={panelRef}>
      <div className="artifact-head">
        <span className="artifact-title">{artifact.file.split("/").pop()}</span>
        <span className="artifact-path" title={artifact.file}>{artifact.file || "所有改动"}</span>
        <button className="iconbtn" onClick={onClose} aria-label="关闭"><Icon name="close" /></button>
      </div>
      <div className="artifact-body">
        {artifact.kind === "gitdiff" ? (
          empty ? (
            <div className="diff-empty">没有未提交的改动。</div>
          ) : (
            <div className="diff-table">
              {sections.map((s, si) => (
                <div className="diff-file" key={si}>
                  <div className="diff-file-h" title={s.file}>
                    <Icon name="edit" size={13} />
                    <span className="diff-file-nm">{s.file}</span>
                  </div>
                  {s.hunks.map((h, hi) => (
                    <div className="diff-hunk" key={hi}>
                      <div className="diff-hunk-h">{h.header}</div>
                      {h.lines.map((l, li) => (
                        <div className={"drow " + l.type} key={li}>
                          <span className="dno">{l.oldNo ?? ""}</span>
                          <span className="dno">{l.newNo ?? ""}</span>
                          <span className="dline">{l.text || " "}</span>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )
        ) : artifact.kind === "diff" ? (
          <pre className="diff-pre">
            {artifact.diff?.map((l, i) => (
              <span key={i} className={"diff-" + l.type}>{(l.type === "add" ? "+" : l.type === "del" ? "−" : " ") + " " + l.text + "\n"}</span>
            ))}
          </pre>
        ) : (
          <div className="prose"><ReactMarkdown remarkPlugins={[remarkGfm]}>{artifact.content || ""}</ReactMarkdown></div>
        )}
      </div>
    </div>
  );
}

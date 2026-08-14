import { useEffect, useState } from "react";

import type { ComposerPaste } from "../composer-pastes";
import { makeComposerPaste } from "../composer-pastes";
import { Icon } from "../icons";

interface Props {
  pastes: readonly ComposerPaste[];
  onChange: (pastes: ComposerPaste[]) => void;
  disabled?: boolean;
}

export function PasteCards({ pastes, onChange, disabled = false }: Props) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const editingPaste = editingId
    ? pastes.find((paste) => paste.id === editingId) ?? null
    : null;

  useEffect(() => {
    if (editingId && !pastes.some((paste) => paste.id === editingId)) {
      setEditingId(null);
      setEditingText("");
    }
  }, [editingId, pastes]);

  if (pastes.length === 0) return null;

  const closeEditor = () => {
    setEditingId(null);
    setEditingText("");
  };

  return <>
    {pastes.map((paste) => (
      <article key={paste.id} className="paste-card">
        <button className="paste-open" type="button" disabled={disabled}
          onClick={() => {
            setEditingId(paste.id);
            setEditingText(paste.text);
          }}>
          <span className="paste-card-icon"><Icon name="read" size={15} /></span>
          <span className="paste-card-body">
            <span className="paste-card-preview">
              {paste.text.replace(/\s+/g, " ").trim()}
            </span>
            <span className="paste-card-meta">
              粘贴内容 · {paste.chars} 字符 · {paste.lines} 行
            </span>
          </span>
        </button>
        <button className="attach-x" type="button" aria-label="移除粘贴内容"
          disabled={disabled}
          onClick={() => onChange(pastes.filter(
            (candidate) => candidate.id !== paste.id))}>
          <Icon name="close" size={12} />
        </button>
      </article>
    ))}

    {editingPaste && (
      <div className="paste-preview-backdrop" role="presentation"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeEditor();
        }}>
        <section className="paste-preview" role="dialog" aria-modal="true"
          aria-label="编辑粘贴内容" onKeyDown={(event) => {
            if (event.key === "Escape") closeEditor();
          }}>
          <header>
            <span>
              <b>编辑粘贴内容</b>
              <small>{editingText.length} 字符 · {editingText.split("\n").length} 行</small>
            </span>
            <button type="button" aria-label="关闭" onClick={closeEditor}>
              <Icon name="close" size={16} />
            </button>
          </header>
          <textarea value={editingText} autoFocus
            onChange={(event) => setEditingText(event.target.value)} />
          <footer>
            <button type="button" onClick={closeEditor}>取消</button>
            <button type="button" className="primary" disabled={!editingText}
              onClick={() => {
                onChange(pastes.map((paste) => paste.id === editingPaste.id
                  ? makeComposerPaste(editingText, paste.id)
                  : paste));
                closeEditor();
              }}>保存</button>
          </footer>
        </section>
      </div>
    )}
  </>;
}

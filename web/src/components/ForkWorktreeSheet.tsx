import { useEffect, useState, type FormEvent } from "react";
import type { SessionInfo } from "../protocol";
import {
  isWorktreeForkNameValid,
  normalizeWorktreeForkName,
  WORKTREE_FORK_NAME_MAX,
} from "../session-worktree";
import { Icon } from "../icons";

interface Props {
  open: boolean;
  session: SessionInfo | null;
  creating: boolean;
  error: string | null;
  onConfirm: (name: string) => void;
  onClose: () => void;
}

export function ForkWorktreeSheet({ open, session, creating, error, onConfirm, onClose }: Props) {
  const [name, setName] = useState("");

  useEffect(() => {
    if (open) setName("");
  }, [open, session?.session_id]);

  if (!open || !session) return null;

  const normalized = normalizeWorktreeForkName(name);
  const valid = isWorktreeForkNameValid(name);
  const close = () => { if (!creating) onClose(); };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!creating && valid) onConfirm(normalized);
  };

  return <>
    <div className="scrim show" onClick={close} />
    <section className="sheet show fork-worktree-sheet" role="dialog" aria-modal="true"
      aria-label="派生到新工作树" aria-busy={creating}>
      <div className="sheet-grip" />
      <header className="fork-worktree-head">
        <span className="fork-worktree-icon"><Icon name="branch" size={19} /></span>
        <span><b>派生到新工作树</b><small>创建独立的 Codex 会话</small></span>
        <button onClick={close} disabled={creating} aria-label="关闭"><Icon name="close" size={17} /></button>
      </header>
      <form onSubmit={submit}>
        <div className="fork-worktree-body">
          <div className="fork-worktree-notice">
            <b>从当前 Git HEAD 创建</b>
            <span>新工作树不会复制当前目录中的未提交修改。</span>
          </div>
          <div className="fork-worktree-source">
            <span>源目录</span><code title={session.cwd || undefined}>{session.cwd || "未知目录"}</code>
            {session.git_branch && <><span>当前分支</span><code>{session.git_branch}</code></>}
          </div>
          <label className="fork-worktree-field">
            <span>名称</span>
            <input autoFocus value={name} maxLength={WORKTREE_FORK_NAME_MAX}
              disabled={creating} placeholder="例如：fix-login-flow"
              onChange={(event) => setName(event.target.value)} />
            <small>用于生成新分支和工作树目录，最多 {WORKTREE_FORK_NAME_MAX} 个字符。</small>
          </label>
          {error && <div className="fork-worktree-error" role="alert">{error}</div>}
        </div>
        <footer className="fork-worktree-actions">
          <button type="button" className="fork-worktree-cancel" onClick={close} disabled={creating}>取消</button>
          <button type="submit" className="fork-worktree-primary" disabled={!valid || creating}>
            {creating && <span className="fork-worktree-spinner" />}
            {creating ? "创建中…" : "创建工作树并派生"}
          </button>
        </footer>
      </form>
    </section>
  </>;
}

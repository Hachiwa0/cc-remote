import { useEffect, useRef, useState } from "react";
import { COMMANDS, MODELS, PERMS, isCmd, type Cmd, type CmdGroup } from "../data";
import { Icon } from "../icons";

interface Props {
  open: boolean;
  kind: "commands" | "models" | "perms";
  onClose: () => void;
  onPickCommand?: (slash: string) => void;
  currentModel?: string;
  onPickModel?: (model: string) => void;
  currentPerm?: string;
  onPickPerm?: (perm: string) => void;
}

export function CommandSheet({ open, kind, onClose, onPickCommand, currentModel, onPickModel, currentPerm, onPickPerm }: Props) {
  const [q, setQ] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const filter = q.toLowerCase();
  const isCmdMode = kind === "commands";
  const isPermMode = kind === "perms";

  // When the command sheet opens, focus the search box + clear the filter so
  // typing narrows the list (Claude-app style: "/" => palette, type to filter).
  useEffect(() => {
    if (open && isCmdMode) {
      setQ("");
      const t = setTimeout(() => searchRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
  }, [open, isCmdMode]);

  const groups: { name: string; cmds: Cmd[] }[] = [];
  if (isCmdMode) {
    for (const c of COMMANDS) {
      if (isCmd(c)) {
        if (!filter || c.slash.toLowerCase().includes(filter) || c.name.toLowerCase().includes(filter)) {
          if (!groups.length) groups.push({ name: "", cmds: [] });
          groups[groups.length - 1].cmds.push(c);
        }
      } else {
        groups.push({ name: (c as CmdGroup).g, cmds: [] });
      }
    }
  }
  const visible = groups.filter((g) => g.cmds.length > 0);

  const title = isCmdMode ? "命令面板" : isPermMode ? "选择权限模式" : "选择模型";

  return (
    <>
      <div className={"scrim" + (open ? " show" : "")} onClick={onClose} />
      <div className={"sheet" + (open ? " show" : "")} role="dialog" aria-label={title}>
        <div className="sheet-grip" />
        {isCmdMode ? (
          <div className="sheet-search">
            <Icon name="term" size={17} />
            <input ref={searchRef} value={q} onChange={(e) => setQ(e.target.value)} placeholder="运行命令或技能…" />
          </div>
        ) : (
          <div className="sheet-title">{title}</div>
        )}
        <div className="sheet-scroll">
          {isCmdMode ? (
            visible.map((g, gi) => (
              <div key={gi}>
                <div className="cmd-group">{g.name}</div>
                {g.cmds.map((c) => (
                  <button key={c.slash} className="cmd" onClick={() => onPickCommand?.(c.slash)}>
                    <span className="cmd-ic"><Icon name={c.ic} size={17} /></span>
                    <span className="cmd-tx">
                      <span className="cmd-nm"><span className="slash">/{c.slash}</span></span>
                      <span className="cmd-ds">{c.name} — {c.ds}</span>
                    </span>
                    <span className="cmd-kbd">↵</span>
                  </button>
                ))}
              </div>
            ))
          ) : isPermMode ? (
            PERMS.map((p) => (
              <button
                key={p.id}
                className={"cmd" + (p.id === currentPerm ? " sel" : "") + (p.danger ? " danger" : "")}
                onClick={() => onPickPerm?.(p.id)}
              >
                <span className="cmd-ic"><Icon name={p.ic} size={17} /></span>
                <span className="cmd-tx">
                  <span className="cmd-nm">{p.name}</span>
                  <span className="cmd-ds">{p.ds}</span>
                </span>
                {p.id === currentPerm
                  ? <span className="cmd-check"><Icon name="check" size={19} /></span>
                  : <span className="cmd-kbd" />}
              </button>
            ))
          ) : (
            MODELS.map((m) => (
              <button
                key={m.id}
                className={"cmd" + (m.id === currentModel ? " sel" : "")}
                onClick={() => onPickModel?.(m.id)}
              >
                <span className="cmd-ic"><Icon name="cpu" size={17} /></span>
                <span className="cmd-tx">
                  <span className="cmd-nm">{m.name}</span>
                  <span className="cmd-ds">{m.ds}</span>
                </span>
                {m.id === currentModel
                  ? <span className="cmd-check"><Icon name="check" size={19} /></span>
                  : <span className="cmd-kbd" />}
              </button>
            ))
          )}
        </div>
      </div>
    </>
  );
}

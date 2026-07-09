// Empty-state "new chat" page: a centered composer (a la Claude app / Codex)
// with the working directory, model, effort, and attachments chosen UP FRONT.
// The first message triggers new_session(cwd, engine, model, effort) on the
// host; once that session is ready the pending query (with its images/files) is
// fired and the normal chat UI takes over. model/effort are null until the user
// picks one — null means "use the wrapper's engine default".
import { useRef, useState, type ClipboardEvent } from "react";
import { Icon } from "../icons";
import { CommandSheet } from "./CommandSheet";
import { modelsFor, effortsFor } from "../data";
import { pickFiles } from "../img";
import type { QueryImg, QueryFile } from "../protocol";

interface Props {
  cwd: string;
  creating: boolean;  // true while the new session is being created (pendingNewQuery set)
  engine?: "claude" | "codex";  // which backend this new chat will use
  model: string | null;   // pre-selected model (null = engine default)
  effort: string | null;  // pre-selected reasoning effort (null = engine default)
  onPickCwd: () => void;  // open the directory picker
  onPickModel: (model: string) => void;
  onPickEffort: (effort: string) => void;
  onSend: (prompt: string, images?: QueryImg[], files?: QueryFile[]) => void;
}

export function NewChatView({ cwd, creating, engine = "claude", model, effort, onPickCwd, onPickModel, onPickEffort, onSend }: Props) {
  const [text, setText] = useState("");
  const [images, setImages] = useState<QueryImg[]>([]);
  const [files, setFiles] = useState<QueryFile[]>([]);
  const [sheetKind, setSheetKind] = useState<"models" | "efforts" | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const MODELS_E = modelsFor(engine), EFFORTS_E = effortsFor(engine);
  const modelName = MODELS_E.find((m) => m.id === model)?.name ?? MODELS_E[0].name;
  // null effort => show the engine's nominal default (cc's real default is "max";
  // codex's comes from its config, so just say "默认"). The chip corrects itself
  // once the spawned session reports its actual effort.
  const effortName = EFFORTS_E.find((e) => e.id === effort)?.name ?? (engine === "codex" ? "默认" : "最大");

  const hasAttachments = images.length > 0 || files.length > 0;
  const canSend = (text.trim().length > 0 || hasAttachments) && !creating;

  const onPick = (fl: FileList | File[] | null) =>
    pickFiles(fl,
      (img) => setImages((prev) => [...prev, img]),
      (file) => setFiles((prev) => [...prev, file]));

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const fs: File[] = [];
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.kind === "file") { const f = it.getAsFile(); if (f) fs.push(f); }
    }
    if (fs.length) { e.preventDefault(); onPick(fs); }
  };

  const send = () => {
    if (!canSend) return;
    onSend(text.trim(), images.length ? images : undefined, files.length ? files : undefined);
  };

  return (
    <div className="newchat">
      <div className="newchat-card">
        <div className="newchat-greet">{engine === "codex" ? "开始 Codex 新对话" : "开始新对话"}
          <span className={`newchat-engine ${engine}`}>{engine === "codex" ? "◇ Codex" : "✳ Claude"}</span>
        </div>
        <button className="newchat-cwd" onClick={onPickCwd} title="更改工作目录" disabled={creating}>
          <Icon name="folder" size={16} />
          <span className="newchat-cwd-path">{cwd === "~" ? "~ · 主目录" : (cwd || "未指定目录")}</span>
          <Icon name="edit" size={13} />
        </button>

        {hasAttachments && (
          <div className="attach show newchat-attach">
            {images.map((img, i) => (
              <span key={i} className="attach-img">
                <img src={`data:${img.media_type};base64,${img.data}`} alt="" />
                <button className="attach-x" onClick={() => setImages(images.filter((_, j) => j !== i))} aria-label="移除"><Icon name="close" size={12} /></button>
              </span>
            ))}
            {files.map((f, i) => (
              <span key={i} className="attach-file">
                <Icon name="read" size={14} />
                <span className="attach-fn">{f.filename}</span>
                <button className="attach-x" onClick={() => setFiles(files.filter((_, j) => j !== i))} aria-label="移除"><Icon name="close" size={12} /></button>
              </span>
            ))}
          </div>
        )}

        <textarea className="newchat-input" placeholder="发条消息开始…"
          value={text} onChange={(e) => setText(e.target.value)} onPaste={onPaste} autoFocus rows={3}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />

        <div className="newchat-foot">
          <div className="newchat-ctls">
            <button className="cmdbtn" onClick={() => fileRef.current?.click()} aria-label="添加图片或文件" title="添加图片或文件" disabled={creating}>
              <Icon name="plus" size={18} />
            </button>
            <input ref={fileRef} type="file" multiple hidden onChange={(e) => { onPick(e.target.files); e.target.value = ""; }} />
            <button className="hint-ctl" onClick={() => setSheetKind("models")} title="选择模型" disabled={creating}>{modelName}</button>
            <button className="hint-ctl" onClick={() => setSheetKind("efforts")} title="思考强度" disabled={creating}>{effortName}</button>
          </div>
          <div className="newchat-foot-right">
            <span className="newchat-hint">{creating ? "正在创建会话…" : "Enter 发送"}</span>
            <button className="newchat-send" onClick={send} disabled={!canSend}>
              <Icon name="send" size={16} />开始
            </button>
          </div>
        </div>
      </div>

      <CommandSheet
        open={sheetKind !== null}
        kind={sheetKind ?? "models"}
        engine={engine}
        onClose={() => setSheetKind(null)}
        currentModel={model ?? MODELS_E[0]?.id}
        onPickModel={(m) => { onPickModel(m); setSheetKind(null); }}
        currentEffort={effort ?? (engine === "codex" ? undefined : "max")}
        onPickEffort={(ef) => { onPickEffort(ef); setSheetKind(null); }}
      />
    </div>
  );
}

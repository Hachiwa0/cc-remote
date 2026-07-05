import { useEffect, useRef, useState } from "react";
import type { State, QueryImg, QueryFile } from "../protocol";
import type { ConnState } from "../ws";
import { Icon } from "../icons";
import { MODELS } from "../data";
import { CommandSheet } from "./CommandSheet";

interface Props {
  state: State;
  connState: ConnState;
  wrapperOnline: boolean;
  sendMode: "interrupt" | "queue";
  setSendMode: (m: "interrupt" | "queue") => void;
  queue: string[];
  model: string;
  perm: string;
  editPrompt: string | null;
  onEditConsumed: () => void;
  onSendQuery: (prompt: string, images?: QueryImg[], files?: QueryFile[]) => void;
  onInterrupt: () => void;
  onEnqueue: (prompt: string) => void;
  onSetPending: (prompt: string) => void;
  onDequeue: (i: number) => void;
  onSetModel: (model: string) => void;
  onSetPerm: (perm: string) => void;
  onClear: () => void;
  onContext: () => void;
}

export function Composer(p: Props) {
  const [input, setInput] = useState("");
  const [sheetKind, setSheetKind] = useState<"commands" | "models" | "perms" | null>(null);
  const [images, setImages] = useState<QueryImg[]>([]);
  const [files, setFiles] = useState<QueryFile[]>([]);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const busy = p.state === "running" || p.state === "interrupting";
  const offline = !p.wrapperOnline;
  const hasText = input.trim().length > 0;
  const hasAttachments = images.length > 0 || files.length > 0;

  // edit: refill the input box with a past prompt (user-bubble edit button)
  useEffect(() => {
    if (p.editPrompt != null) {
      setInput(p.editPrompt);
      p.onEditConsumed();
      setTimeout(() => taRef.current?.focus(), 0);
      if (taRef.current) {
        taRef.current.style.height = "auto";
        taRef.current.style.height = Math.min(taRef.current.scrollHeight, 132) + "px";
      }
    }
  }, [p.editPrompt]);

  const onInput = (v: string) => {
    setInput(v);
    if (v === "/") setSheetKind("commands");
    if (taRef.current) {
      taRef.current.style.height = "auto";
      taRef.current.style.height = Math.min(taRef.current.scrollHeight, 132) + "px";
    }
  };

  const onPickFiles = (fl: FileList | null) => {
    if (!fl) return;
    Array.from(fl).forEach((f) => {
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = reader.result as string;  // data:<type>;base64,<data>
        const base64 = (dataUrl.split(",", 2)[1]) || "";
        if (f.type.startsWith("image/")) setImages((prev) => [...prev, { media_type: f.type, data: base64 }]);
        else setFiles((prev) => [...prev, { filename: f.name, data: base64 }]);
      };
      reader.readAsDataURL(f);
    });
  };

  const send = () => {
    if (offline) return;
    const prompt = input.trim();
    if (busy) {
      // empty input while busy => stop (interrupt); non-empty => interrupt+send or enqueue
      if (!prompt && !hasAttachments) { p.onInterrupt(); return; }
      if (p.sendMode === "queue") { p.onEnqueue(prompt); setInput(""); return; }
      p.onInterrupt(); p.onSetPending(prompt);
      setInput(""); setImages([]); setFiles([]);
      if (taRef.current) taRef.current.style.height = "auto";
      return;
    }
    if (!prompt && !hasAttachments) return;
    p.onSendQuery(prompt, images.length ? images : undefined, files.length ? files : undefined);
    setInput(""); setImages([]); setFiles([]);
    if (taRef.current) taRef.current.style.height = "auto";
  };

  const stopping = busy && !hasText && !hasAttachments;
  const sendIcon = !busy ? "send" : stopping ? "stop" : p.sendMode === "interrupt" ? "bolt" : "queue";
  const sendClass = "sendbtn" + ((stopping || (busy && p.sendMode === "interrupt" && (hasText || hasAttachments))) ? " interrupt" : "");
  const disabled = offline || (!busy && !hasText && !hasAttachments);
  const model = MODELS.find((m) => m.id === p.model) || MODELS[0];

  return (
    <div className="composer">
      <div className="composer-in">
        {p.queue.length > 0 && (
          <div className="queued show">
            {p.queue.map((m, i) => (
              <span className="qchip" key={i}>
                <span className="qbadge">排队</span>
                <span className="qt">{m}</span>
                <span className="qx" onClick={() => p.onDequeue(i)}><Icon name="close" size={12} /></span>
              </span>
            ))}
          </div>
        )}

        {hasAttachments && (
          <div className="attach show">
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

        {busy && (
          <div className="runbar show">
            <div className="seg">
              <button className={p.sendMode === "interrupt" ? "on" : ""} onClick={() => p.setSendMode("interrupt")}>
                <Icon name="bolt" size={14} />打断并发送
              </button>
              <button className={p.sendMode === "queue" ? "on" : ""} onClick={() => p.setSendMode("queue")}>
                <Icon name="queue" size={14} />排队
              </button>
            </div>
          </div>
        )}

        <div className="inrow">
          <button className="cmdbtn" onClick={() => fileRef.current?.click()} aria-label="附件" disabled={offline}><Icon name="plus" size={19} /></button>
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => { onPickFiles(e.target.files); e.target.value = ""; }} />
          <textarea
            ref={taRef}
            rows={1}
            value={input}
            placeholder={offline ? "机器离线 — 等待重连…" : "发消息…  输入 / 唤起命令"}
            disabled={offline}
            onChange={(e) => onInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
          />
          <button className="cchip mini" aria-label="选择模型" onClick={() => setSheetKind("models")}>
            <span className="ci"><Icon name="cpu" size={15} /></span>
            <span className="cv">{model.name}</span>
          </button>
          <button className={sendClass} onClick={send} disabled={disabled} aria-label="发送">
            <Icon name={sendIcon} size={19} />
          </button>
        </div>
        <div className="hint"><kbd>Enter</kbd> 发送 · <kbd>Shift+Enter</kbd> 换行 · <kbd>/</kbd> 命令</div>
      </div>

      <CommandSheet
        open={sheetKind !== null}
        kind={sheetKind || "commands"}
        onClose={() => setSheetKind(null)}
        onPickCommand={(slash) => {
          if (slash === "clear") { p.onClear(); setSheetKind(null); return; }
          if (slash === "context") { p.onContext(); setSheetKind(null); return; }
          if (slash === "permissions") { setSheetKind("perms"); return; }
          setInput("/" + slash + " ");
          setSheetKind(null);
          setTimeout(() => taRef.current?.focus(), 0);
        }}
        currentModel={p.model}
        onPickModel={(m) => { p.onSetModel(m); setSheetKind(null); }}
        currentPerm={p.perm}
        onPickPerm={(perm) => { p.onSetPerm(perm); setSheetKind(null); }}
      />
    </div>
  );
}

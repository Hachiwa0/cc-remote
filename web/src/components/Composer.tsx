import { useEffect, useRef, useState, type ClipboardEvent } from "react";
import type { State, QueryImg, QueryFile, ContextReport } from "../protocol";
import type { ConnState } from "../ws";
import { Icon } from "../icons";
import { clientSlashesFor, CODEX_PROMPTS, slashToken, matchCommands, parseSlash, modelsFor, effortsFor, permsFor } from "../data";
import { CommandSheet } from "./CommandSheet";

interface Props {
  state: State;
  connState: ConnState;
  wrapperOnline: boolean;
  sendMode: "interrupt" | "queue";
  setSendMode: (m: "interrupt" | "queue") => void;
  queue: string[];
  model: string;
  effort: string;
  perm: string;
  fast?: boolean;   // codex Fast-mode state (from wrapper)
  engine?: "claude" | "codex";
  editPrompt: string | null;
  onEditConsumed: () => void;
  onSendQuery: (prompt: string, images?: QueryImg[], files?: QueryFile[]) => void;
  onInterrupt: () => void;
  onEnqueue: (prompt: string) => void;
  onSetPending: (prompt: string) => void;
  onDequeue: (i: number) => void;
  onSetModel: (model: string) => void;
  onSetEffort: (effort: string) => void;
  onSetServiceTier?: (tier: string) => void;
  onSetPerm: (perm: string) => void;
  onClear: () => void;
  onContext: () => void;
  contextReport: ContextReport | null;
}

export function Composer(p: Props) {
  const [input, setInput] = useState("");
  // Only the modal pickers live in state now; the "/" command palette is a live
  // popover DERIVED from the composer text (no second input box).
  const [sheetKind, setSheetKind] = useState<"models" | "efforts" | "perms" | null>(null);
  const [ctxOpen, setCtxOpen] = useState(false);
  const ctxWrapRef = useRef<HTMLDivElement>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const [images, setImages] = useState<QueryImg[]>([]);
  const [files, setFiles] = useState<QueryFile[]>([]);
  const [dragDepth, setDragDepth] = useState(0);
  const dragOver = dragDepth > 0;
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // context popover: close on outside click
  useEffect(() => {
    if (!ctxOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (ctxWrapRef.current && !ctxWrapRef.current.contains(e.target as Node)) setCtxOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [ctxOpen]);

  // Whole-window drag-drop overlay (ChatGPT/Claude-app style): any file drag
  // over the window shows a drop overlay; drop anywhere to attach. A depth
  // counter avoids flicker when the pointer crosses child element boundaries.
  useEffect(() => {
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types || []).includes("Files");
    const onEnter = (e: DragEvent) => { if (hasFiles(e)) setDragDepth((d) => d + 1); };
    const onLeave = (e: DragEvent) => { if (hasFiles(e)) setDragDepth((d) => Math.max(0, d - 1)); };
    const onOver = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };  // allow drop
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      setDragDepth(0);
      if (e.dataTransfer?.files?.length) onPickFiles(e.dataTransfer.files);
    };
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("dragover", onOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("drop", onDrop);
    };
  }, []);

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

  const resetTaHeight = () => { if (taRef.current) taRef.current.style.height = "auto"; };
  const growTa = () => {
    if (taRef.current) {
      taRef.current.style.height = "auto";
      taRef.current.style.height = Math.min(taRef.current.scrollHeight, 132) + "px";
    }
  };
  const focusTa = () => setTimeout(() => taRef.current?.focus(), 0);
  const flash = (msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2200);
  };

  const onInput = (v: string) => {
    setInput(v);
    growTa();
  };

  // Command palette = live suggestions derived from the input. Visible while the
  // user is typing a command name (after "/", before a space) that prefix-matches
  // at least one command. Typing past a match (/pet) hides it; deleting back
  // (/pe) shows it again — no separate input, no modal scrim over the composer.
  const cmdToken = slashToken(input);
  const cmdMatches = cmdToken !== null ? matchCommands(cmdToken, p.engine) : [];
  const cmdOpen = cmdMatches.length > 0;

  const onPickFiles = (fl: FileList | File[] | null) => {
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

  // paste images/files straight into the textarea (clipboard API)
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) { e.preventDefault(); onPickFiles(files); }
  };

  // Send prompt text to cc, honoring busy/queue/interrupt rules.
  const submitPrompt = (prompt: string) => {
    if (busy) {
      // empty input while busy => stop (interrupt); non-empty => interrupt+send or enqueue
      if (!prompt && !hasAttachments) { p.onInterrupt(); return; }
      if (p.sendMode === "queue") { p.onEnqueue(prompt); setInput(""); return; }
      p.onInterrupt(); p.onSetPending(prompt);
      setInput(""); setImages([]); setFiles([]); resetTaHeight();
      return;
    }
    if (!prompt && !hasAttachments) return;
    p.onSendQuery(prompt, images.length ? images : undefined, files.length ? files : undefined);
    setInput(""); setImages([]); setFiles([]); resetTaHeight();
  };

  // Client-side slash commands — never forwarded to cc. "/model <id>" passes the
  // id straight to set_model (hidden models included), aligning with Claude Code.
  const runClientSlash = (slash: string, args: string) => {
    switch (slash) {
      case "model":
        if (args) { p.onSetModel(args); flash(`已切换模型：${args}`); }
        else setSheetKind("models");
        break;
      case "permissions": setSheetKind("perms"); break;
      case "clear": p.onClear(); break;
      // open the popup too (not just fetch) — same as clicking the context ring.
      case "context": p.onContext(); setCtxOpen(true); break;
      // codex /status = session config + token usage; surface the context readout.
      case "status": p.onContext(); setCtxOpen(true); break;
      // codex /fast: flip the Fast service tier. The wrapper toggles off the
      // actual config (source of truth); p.fast is the current state, so the
      // resulting state is its negation. Applies on the next message.
      case "fast": {
        p.onSetServiceTier?.("toggle");
        flash(!p.fast ? "⚡ 已开启快速模式 · 下条生效" : "已回到标准模式 · 下条生效");
        break;
      }
      case "plan": p.onSetPerm("plan"); if (args) { submitPrompt(args); return; } break;
      case "normal": p.onSetPerm("bypassPermissions"); if (args) { submitPrompt(args); return; } break;
    }
    setInput(""); resetTaHeight();
  };

  // Pick a command from the palette. Client commands run now; cc skills
  // (/code-review …) fill the composer so the user can add args, then send.
  const pickCommand = (slash: string) => {
    if (clientSlashesFor(p.engine).has(slash)) { runClientSlash(slash, ""); focusTa(); return; }
    setInput("/" + slash + " ");
    focusTa(); growTa();
  };

  const send = () => {
    if (offline) return;
    const raw = input.trim();
    if (raw === "/") return;
    const parsed = parseSlash(raw);
    if (parsed && clientSlashesFor(p.engine).has(parsed.slash)) { runClientSlash(parsed.slash, parsed.args); return; }
    // Codex has no TUI slash layer over the app-server, so a codex command
    // (/review, /init) is sent as the natural-language prompt codex handles.
    if (p.engine === "codex" && parsed && CODEX_PROMPTS[parsed.slash]) {
      submitPrompt(CODEX_PROMPTS[parsed.slash] + (parsed.args ? "\n\n" + parsed.args : ""));
      return;
    }
    // Codex has no TUI slash layer, so an unknown "/xxx" is NOT a command — don't
    // ship it to the model as literal text (it would just improvise a fake reply,
    // e.g. "/fast" -> "已切到快节奏…"). Hint and keep the input. (cc is different:
    // it has its own slash/skill layer, so unknown slashes fall through to it.)
    if (p.engine === "codex" && parsed) { flash(`Codex 无此指令：/${parsed.slash}`); return; }
    // plain text, or a cc skill slash (/code-review …) forwarded verbatim to cc
    submitPrompt(raw);
  };

  const stopping = busy && !hasText && !hasAttachments;
  const sendIcon = !busy ? "send" : stopping ? "stop" : p.sendMode === "interrupt" ? "bolt" : "queue";
  const sendClass = "sendbtn" + ((stopping || (busy && p.sendMode === "interrupt" && (hasText || hasAttachments))) ? " interrupt" : "");
  const disabled = offline || (!busy && !hasText && !hasAttachments);
  // Fall back to the raw id (not MODELS[0]) so a hidden model set via
  // "/model <id>" shows its actual id on the chip instead of "Mythos 5".
  const MODELS_E = modelsFor(p.engine), EFFORTS_E = effortsFor(p.engine), PERMS_E = permsFor(p.engine);
  // Codex: if the session hasn't reported a codex model yet (or it's a stale
  // claude id), show the codex default instead of a leftover "Mythos 5".
  const model = MODELS_E.find((m) => m.id === p.model)
    || (p.engine === "codex" ? MODELS_E[0] : { id: p.model, name: p.model || MODELS_E[0].name, ds: "", ic: "cpu" });
  const effort = EFFORTS_E.find((e) => e.id === p.effort) || EFFORTS_E[EFFORTS_E.length - 1];
  const perm = PERMS_E.find((x) => x.id === p.perm) || PERMS_E[0];
  const stateZh: Record<State, string> = { idle: "空闲", running: "运行中", interrupting: "打断中", draining: "收尾中" };
  const modeCls = perm.id === "plan" ? " plan" : perm.danger ? " danger" : "";
  const hintBusy = p.state !== "idle";

  return (
    <div className="composer">
      <div className="composer-in">
        {cmdOpen && (
          <div className="cmd-pop" role="listbox" aria-label="命令">
            {cmdMatches.map((c) => (
              <button key={c.slash} type="button" className="cmd" onClick={() => pickCommand(c.slash)}>
                <span className="cmd-ic"><Icon name={c.ic} size={17} /></span>
                <span className="cmd-tx">
                  <span className="cmd-nm"><span className="slash">/{c.slash}</span></span>
                  <span className="cmd-ds">{c.name} — {c.ds}</span>
                </span>
                <span className="cmd-kbd">↵</span>
              </button>
            ))}
          </div>
        )}
        {notice && <div className="composer-notice">{notice}</div>}
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
            onPaste={onPaste}
            onKeyDown={(e) => {
              if (e.key === "Escape" && cmdOpen) { setInput(""); resetTaHeight(); return; }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                // typing a command name (no args) => Enter completes/executes the top match
                if (cmdOpen && cmdToken) { pickCommand(cmdMatches[0].slash); return; }
                if (cmdToken === "") return; // just "/" — do nothing
                send();
              }
            }}
          />
          <button className={sendClass} onClick={send} disabled={disabled} aria-label="发送">
            <Icon name={sendIcon} size={19} />
          </button>
        </div>
        <div className="hint">
          <button
            type="button"
            className={"hint-mode" + modeCls + (hintBusy ? " busy" : "")}
            onClick={() => setSheetKind("perms")}
            title="点击切换权限模式"
          >
            {perm.short}模式 · {stateZh[p.state]} <span className="hint-mode-ch">▾</span>
          </button>
          <span className="hint-kbds"><kbd>Enter</kbd> 发送 · <kbd>Shift+Tab</kbd> 切模式 · <kbd>/</kbd> 命令</span>
          <div className="hint-right" ref={ctxWrapRef}>
            <button className="hint-ctl" onClick={() => setSheetKind("models")} title="选择模型">{model.name}</button>
            <button className="hint-ctl" onClick={() => setSheetKind("efforts")} title="思考强度">{effort.name}</button>
            {p.engine === "codex" && (
              <button
                className={"hint-ctl fast-chip" + (p.fast ? " on" : "")}
                onClick={() => p.onSetServiceTier?.("toggle")}
                title="Fast 服务档位:快 / 标准(下条消息生效)"
              >{p.fast ? "⚡ 快" : "标准"}</button>
            )}
            <button
              className="hint-ring"
              aria-label="上下文占用"
              title="上下文占用"
              onClick={() => { p.onContext(); setCtxOpen((o) => !o); }}
            >
              <svg viewBox="0 0 36 36" width="20" height="20" aria-hidden="true">
                <circle className="hr-track" cx="18" cy="18" r="15" />
                <circle
                  className="hr-fill"
                  cx="18" cy="18" r="15"
                  strokeDasharray="94.25"
                  strokeDashoffset={94.25 * (1 - Math.min(p.contextReport?.percentage ?? 0, 100) / 100)}
                  transform="rotate(-90 18 18)"
                />
              </svg>
            </button>
            {ctxOpen && (
              <div className="ctx-pop" role="dialog" aria-label="上下文占用">
                {p.contextReport ? (
                  <>
                    <div className="ctx-pop-row">
                      <span>上下文窗口</span>
                      <span className="ctx-pop-nums">
                        {p.contextReport.total_tokens.toLocaleString()} / {p.contextReport.max_tokens.toLocaleString()} ({p.contextReport.percentage.toFixed(0)}%)
                      </span>
                    </div>
                    <div className="ctx-pop-bar"><i style={{ width: `${Math.min(p.contextReport.percentage, 100)}%` }} /></div>
                    {p.contextReport.categories.length > 0 && (
                      <div className="ctx-pop-cats">
                        {p.contextReport.categories.map((c, i) => (
                          <div className="ctx-pop-cat" key={i}>
                            <span className="ctx-cat-dot" style={{ background: c.color }} />
                            <span className="ctx-pop-cat-name">{c.name}</span>
                            <span className="ctx-pop-cat-tok">{c.tokens.toLocaleString()}</span>
                          </div>
                        ))}
                      </div>
                    )}
                    {p.contextReport.is_auto_compact_enabled && <div className="ctx-pop-auto">autocompact 已启用</div>}
                    <div className="ctx-pop-foot">{p.contextReport.model || ""}</div>
                  </>
                ) : (
                  <div className="ctx-pop-loading">读取上下文占用…</div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      <CommandSheet
        open={sheetKind !== null}
        kind={sheetKind ?? "models"}
        engine={p.engine}
        onClose={() => setSheetKind(null)}
        currentModel={p.model}
        onPickModel={(m) => { p.onSetModel(m); setSheetKind(null); }}
        currentEffort={p.effort}
        onPickEffort={(ef) => { p.onSetEffort(ef); setSheetKind(null); }}
        currentPerm={p.perm}
        onPickPerm={(perm) => { p.onSetPerm(perm); setSheetKind(null); }}
      />

      {dragOver && (
        <div className="drop-overlay" aria-hidden="true">
          <div className="drop-card">
            <span className="dc-ic"><Icon name="plus" size={36} /></span>
            <div className="dc-tx">拖拽文件到此处发送</div>
            <div className="dc-sub">图片直接进对话 · 其他文件写到 /tmp 用 @ 引用</div>
          </div>
        </div>
      )}
    </div>
  );
}

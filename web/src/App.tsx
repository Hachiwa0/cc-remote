import { useEffect, useReducer, useRef, useState, type TouchEvent } from "react";
import { RelayWs } from "./ws";
import { reduce, initialState, createRuntime } from "./reducer";
import { uuid } from "./util";
import { Icon, ClaudeMark } from "./icons";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { ReconnectBanner } from "./components/ReconnectBanner";
import { LoginForm } from "./components/LoginForm";
import { SessionsSidebar } from "./components/SessionsSidebar";
import { DirPicker } from "./components/DirPicker";
import { NewChatView } from "./components/NewChatView";
import { ArtifactPanel } from "./components/ArtifactPanel";
import { QuestionSheet } from "./components/QuestionSheet";
import type { Snapshot, QueryImg, QueryFile } from "./protocol";

const SESSION_KEY = "cc_remote_session";
const THEME_KEY = "cc_remote_theme";

// The sidebar is an overlay on mobile (<980px, matches index.css) but a
// persistent grid column on desktop. So auto-close it after picking a session
// ONLY on mobile; on desktop keep it open.
const isMobile = () => window.matchMedia("(max-width: 979px)").matches;

export default function App() {
  const [theme, setTheme] = useState<string>(() => localStorage.getItem(THEME_KEY) || "light");
  const [authed, setAuthed] = useState<boolean>(() => !!localStorage.getItem(SESSION_KEY));
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dirPickerOpen, setDirPickerOpen] = useState(false);
  const [editPrompt, setEditPrompt] = useState<string | null>(null);
  const [state, dispatch] = useReducer(reduce, initialState);
  const wsRef = useRef<RelayWs | null>(null);
  const drainingRef = useRef(false);
  const touchStartX = useRef(0);

  // The focused session's runtime (turns/state/model/perm/queue/...). Falls back
  // to an empty runtime before any session is focused.
  const focusedSid = state.focusedSid;
  const rt = state.runtimes[focusedSid ?? ""] ?? createRuntime();

  // swipe right -> open sidebar, swipe left -> close (mobile)
  const onTouchStart = (e: TouchEvent) => { touchStartX.current = e.touches[0].clientX; };
  const onTouchEnd = (e: TouchEvent) => {
    const dx = e.changedTouches[0].clientX - touchStartX.current;
    if (dx > 50 && touchStartX.current < window.innerWidth / 3) setSidebarOpen(true);
    else if (dx < -50) setSidebarOpen(false);
  };

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  const toggleTheme = () => setTheme((t) => (t === "dark" ? "light" : "dark"));

  // WebSocket lifecycle
  useEffect(() => {
    if (!authed) return;
    const token = localStorage.getItem(SESSION_KEY) || "";

    // A snapshot announces a session (cc_session_id/state/cwd); the wrapper's
    // full-buffer replay (right after) supplies the turns. IDB cache + delta
    // cursors are Phase 2. Seed a 0 cursor so the next hello requests a replay.
    async function handleSnapshot(e: Snapshot) {
      dispatch({ type: "event", event: e });
      const sid = e.cc_session_id ?? e.sid;
      if (sid) wsRef.current?.setLastSeq(sid, 0);
    }

    const ws = new RelayWs(token, {
      onEvent: (msg) => {
        if (msg.type === "snapshot") { void handleSnapshot(msg); return; }
        dispatch({ type: "event", event: msg });
        if (msg.type === "wrapper_reconnected") { ws.sendHello(); ws.sendListSessions(); }
      },
      onConnState: (s, detail) => {
        dispatch({ type: "conn", connState: s, detail });
        if (s === "connected") ws.sendListSessions();
      },
      onAuthFail: () => {
        localStorage.removeItem(SESSION_KEY);
        setAuthed(false);
      },
    });
    wsRef.current = ws;
    ws.start();
    return () => {
      ws.stop();
      wsRef.current = null;
      drainingRef.current = false;
    };
  }, [authed]);

  // interrupt-and-send: when the focused session returns to idle, fire its pending message
  useEffect(() => {
    if (rt.state === "idle" && rt.pendingSend && wsRef.current) {
      const prompt = rt.pendingSend;
      const msg_id = uuid();
      wsRef.current.sendQuery(prompt, msg_id);
      dispatch({ type: "query_sent", prompt, msg_id, ts: Date.now() });
      dispatch({ type: "clear_pending" });
    }
  }, [focusedSid, rt.state, rt.pendingSend]);

  // queue drain for the focused session
  useEffect(() => {
    if (rt.state === "idle" && rt.queue.length > 0 && !drainingRef.current && wsRef.current) {
      drainingRef.current = true;
      const next = rt.queue[0];
      const msg_id = uuid();
      wsRef.current.sendQuery(next, msg_id);
      dispatch({ type: "query_sent", prompt: next, msg_id, ts: Date.now() });
      dispatch({ type: "dequeue_at", i: 0 });
    }
    if (rt.state !== "idle") drainingRef.current = false;
  }, [focusedSid, rt.state, rt.queue]);

  // new-chat first message: the wrapper's new_session → session_focus bumps
  // switchTick; this effect then fires the held query (ws stamps sid=focused).
  useEffect(() => {
    if (!state.pendingNewQuery || !wsRef.current) return;
    const q = state.pendingNewQuery;
    wsRef.current.sendQuery(q.prompt, q.msg_id, q.images, q.files);
    dispatch({ type: "query_sent", prompt: q.prompt, msg_id: q.msg_id, images: q.images, files: q.files, ts: Date.now() });
    dispatch({ type: "clear_pending_new_query" });
  }, [state.switchTick]);

  // Persist the focused session's turns to IndexedDB (Phase-2 will write through
  // background sessions too). Coalesced in cache.ts.
  useEffect(() => {
    const sid = rt.ccSessionId;
    if (!sid || rt.turns.length === 0) return;
    import("./cache").then(({ saveSession }) => {
      const live = wsRef.current?.lastSeqValue || 0;
      saveSession(sid, rt.turns, live);
    });
  }, [focusedSid, rt.turns, rt.ccSessionId]);

  // Cmd/Ctrl+B => toggle sidebar; Cmd/Ctrl+Option+B => open latest turn's diff
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      const b = e.key === "b" || e.key === "B";
      if (!b) return;
      if (e.metaKey && e.ctrlKey) {
        e.preventDefault();
        if (state.artifact) dispatch({ type: "clear_artifact" });
        else getDiff("");
      } else if (e.metaKey || e.ctrlKey) {
        e.preventDefault();
        setSidebarOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed, state.artifact]);

  // Shift+Tab => cycle permission mode for the focused session
  useEffect(() => {
    if (!authed) return;
    const CYCLE: Record<string, string> = {
      bypassPermissions: "acceptEdits",
      acceptEdits: "plan",
      plan: "bypassPermissions",
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" && e.shiftKey) {
        e.preventDefault();
        setPerm(CYCLE[rt.perm] || "bypassPermissions");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed, focusedSid, rt.perm]);

  if (!authed) {
    return <LoginForm onLogin={(t) => { localStorage.setItem(SESSION_KEY, t); setAuthed(true); }} theme={theme} onToggleTheme={toggleTheme} />;
  }

  const sendQuery = (prompt: string, images?: QueryImg[], files?: QueryFile[]) => {
    if (!wsRef.current) return;
    const msg_id = uuid();
    wsRef.current.sendQuery(prompt, msg_id, images, files);
    dispatch({ type: "query_sent", prompt, msg_id, images, files, ts: Date.now() });
  };
  // First message of a new chat: create the session in the chosen cwd; the
  // switchTick effect fires the actual query once session_focus arrives.
  const sendFirstMessage = (prompt: string) => {
    if (!wsRef.current || !state.newChat) return;
    const cwd = state.newChat.cwd;
    const msg_id = uuid();
    dispatch({ type: "start_new_query", prompt, msg_id });
    wsRef.current.sendNewSession(cwd);
  };
  const interrupt = () => wsRef.current?.sendInterrupt();
  const setModel = (model: string) => {
    wsRef.current?.sendSetModel(model);
    dispatch({ type: "set_model", model });
  };
  const setPerm = (perm: string) => {
    wsRef.current?.sendSetPerm(perm);
    dispatch({ type: "set_perm", perm });
  };
  const getDiff = (file: string) => { dispatch({ type: "open_artifact_loading", file }); wsRef.current?.sendGetDiff(file, theme); };
  const logout = () => {
    localStorage.removeItem(SESSION_KEY);
    wsRef.current?.stop();
    setAuthed(false);
  };

  return (
    <div className={"shell" + (sidebarOpen ? " sidebar-open" : "")} onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      <SessionsSidebar
        open={sidebarOpen}
        sessions={state.sessions}
        liveStates={Object.fromEntries(Object.entries(state.runtimes).map(([sid, r]) => [sid, r.state]))}
        activeSessionId={focusedSid}
        onSelect={(id) => { dispatch({ type: "exit_new_chat" }); dispatch({ type: "focus_session", sid: id }); wsRef.current?.setFocusedSid(id); wsRef.current?.sendSwitchSession(id); if (isMobile()) setSidebarOpen(false); }}
        onNew={() => { dispatch({ type: "enter_new_chat", cwd: state.currentCwd }); if (isMobile()) setSidebarOpen(false); }}
        onNewInDir={(cwd) => { dispatch({ type: "enter_new_chat", cwd }); if (isMobile()) setSidebarOpen(false); }}
        onClose={() => setSidebarOpen(false)}
        onRename={(id, title) => wsRef.current?.sendRenameSession(id, title)}
        onArchive={(id, archived) => wsRef.current?.sendArchiveSession(id, archived)}
      />
      <DirPicker
        open={dirPickerOpen}
        path={state.dirPicker?.path ?? null}
        parent={state.dirPicker?.parent ?? null}
        dirs={state.dirPicker?.dirs ?? []}
        onBrowse={(p) => wsRef.current?.sendListDir(p)}
        onConfirm={(cwd) => { if (state.newChat) dispatch({ type: "set_new_chat_cwd", cwd }); setDirPickerOpen(false); }}
        onClose={() => setDirPickerOpen(false)}
      />
      <section className="pane">
        <header className="c-head">
          <div className="titlewrap">
            <div className="ttl">
              <span className="brand" onClick={() => setSidebarOpen(true)} style={{ cursor: "pointer" }}>
                <span className="brand-mark"><ClaudeMark size={18} /></span>
                <span className="name serif"><b>cc</b><span>·remote</span></span>
              </span>
            </div>
            <div className="sub">{rt.ccSessionId ? `session ${rt.ccSessionId.slice(0, 8)}` : "connected"}</div>
          </div>
          <span className={`hstat ${rt.state}`}><span className="sd" />{rt.state}</span>
          <button className="iconbtn" onClick={toggleTheme} aria-label="切换主题">
            <Icon name={theme === "dark" ? "sun" : "moon"} />
          </button>
          <button className="iconbtn" onClick={logout} aria-label="退出"><Icon name="dots" /></button>
        </header>

        <ReconnectBanner banner={state.banner} replaying={rt.replaying} truncated={rt.truncated} />

        {state.newChat ? (
          <NewChatView cwd={state.newChat.cwd} creating={!!state.pendingNewQuery}
            onPickCwd={() => setDirPickerOpen(true)}
            onSend={sendFirstMessage} />
        ) : (
          <>
            <ChatView sid={focusedSid} turns={rt.turns} onEdit={(prompt) => setEditPrompt(prompt)} onGetDiff={getDiff} />

            <Composer
          state={rt.state}
          connState={state.connState}
          wrapperOnline={state.wrapperOnline}
          sendMode={state.sendMode}
          setSendMode={(m) => dispatch({ type: "set_send_mode", mode: m })}
          queue={rt.queue}
          model={rt.model}
          perm={rt.perm}
          editPrompt={editPrompt}
          onEditConsumed={() => setEditPrompt(null)}
          onSendQuery={sendQuery}
          onInterrupt={interrupt}
          onEnqueue={(prompt) => dispatch({ type: "enqueue", prompt })}
          onSetPending={(prompt) => dispatch({ type: "set_pending", prompt })}
          onDequeue={(i) => dispatch({ type: "dequeue_at", i })}
          onSetModel={setModel}
          onSetPerm={setPerm}
          onClear={() => dispatch({ type: "enter_new_chat", cwd: state.currentCwd })}
          onContext={() => wsRef.current?.sendGetContext()}
        />
          </>
        )}
        {rt.contextReport && (
          <>
            <div className="scrim show" onClick={() => dispatch({ type: "clear_context" })} />
            <div className="sheet show" role="dialog" aria-label="上下文用量">
              <div className="sheet-grip" />
              <div className="sheet-title">上下文用量 · {rt.contextReport.model || ""}</div>
              <div className="sheet-scroll">
                <div className="ctx-overview">
                  <div className="ctx-pct">{rt.contextReport.percentage.toFixed(1)}%</div>
                  <div className="ctx-bar"><div className="ctx-bar-fill" style={{ width: `${Math.min(rt.contextReport.percentage, 100)}%` }} /></div>
                  <div className="ctx-numbers">{rt.contextReport.total_tokens.toLocaleString()} / {rt.contextReport.max_tokens.toLocaleString()} tokens</div>
                  {rt.contextReport.is_auto_compact_enabled && <div className="ctx-auto">autocompact 已启用</div>}
                </div>
                <div className="ctx-cats">
                  {rt.contextReport.categories.map((c, i) => (
                    <div className="ctx-cat" key={i}>
                      <span className="ctx-cat-dot" style={{ background: c.color }} />
                      <span className="ctx-cat-name">{c.name}</span>
                      <span className="ctx-cat-tokens">{c.tokens.toLocaleString()}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="s-foot">
                <button className="newbtn" onClick={() => dispatch({ type: "clear_context" })}>关闭</button>
              </div>
            </div>
          </>
        )}
      </section>
      {state.artifact && (
        <ArtifactPanel artifact={state.artifact} onClose={() => dispatch({ type: "clear_artifact" })} />
      )}
      {rt.pendingQuestion && (
        <QuestionSheet
          question={rt.pendingQuestion.question}
          options={rt.pendingQuestion.options}
          onAnswer={(answer) => {
            wsRef.current?.sendAnswerQuestion(rt.pendingQuestion!.ask_id, answer);
            dispatch({ type: "answer_question" });
          }}
        />
      )}
    </div>
  );
}

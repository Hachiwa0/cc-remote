import { useEffect, useReducer, useRef, useState, type TouchEvent } from "react";
import { RelayWs } from "./ws";
import { reduce, initialState } from "./reducer";
import { uuid } from "./util";
import { Icon } from "./icons";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { ReconnectBanner } from "./components/ReconnectBanner";
import { LoginForm } from "./components/LoginForm";
import { SessionsSidebar } from "./components/SessionsSidebar";
import { ArtifactPanel } from "./components/ArtifactPanel";
import { QuestionSheet } from "./components/QuestionSheet";
import { loadSession, saveSession } from "./cache";
import type { Turn } from "./reducer";
import type { Snapshot, QueryImg, QueryFile } from "./protocol";

const SESSION_KEY = "cc_remote_session";
const THEME_KEY = "cc_remote_theme";

export default function App() {
  const [theme, setTheme] = useState<string>(() => localStorage.getItem(THEME_KEY) || "light");
  const [authed, setAuthed] = useState<boolean>(() => !!localStorage.getItem(SESSION_KEY));
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [editPrompt, setEditPrompt] = useState<string | null>(null);
  const [state, dispatch] = useReducer(reduce, initialState);
  const wsRef = useRef<RelayWs | null>(null);
  const drainingRef = useRef(false);
  const lastSeqRef = useRef(0);  // highest seq of the locally-cached turns (cache read or live events)
  const touchStartX = useRef(0);

  // swipe right -> open sidebar, swipe left -> close (mobile)
  const onTouchStart = (e: TouchEvent) => { touchStartX.current = e.touches[0].clientX; };
  const onTouchEnd = (e: TouchEvent) => {
    const dx = e.changedTouches[0].clientX - touchStartX.current;
    // Open only on swipe-right starting from the left third — so swiping right
    // to read horizontally-overflowing text in the rest of the pane doesn't
    // pop the sidebar. Close on swipe-left anywhere.
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

    // First hello -> wrapper sends a snapshot(cc_session_id). Read the IndexedDB
    // cache for that session: if present, restore turns locally + ask the wrapper
    // only for the delta (seq > cached lastSeq); else ask for the full buffer.
    async function handleSnapshot(e: Snapshot) {
      dispatch({ type: "event", event: e });  // reducer learns cc_session_id + state
      const sid = e.cc_session_id;
      if (!sid) { wsRef.current?.sendHello(0); return; }
      const cached = await loadSession(sid);
      if (cached && cached.turns.length > 0) {
        dispatch({ type: "set_turns", turns: cached.turns as Turn[] });
        lastSeqRef.current = cached.lastSeq;
        wsRef.current?.sendHello(cached.lastSeq);
      } else {
        wsRef.current?.sendHello(0);
      }
    }

    const ws = new RelayWs(token, {
      onEvent: (msg) => {
        if (msg.type === "snapshot") { void handleSnapshot(msg); return; }
        dispatch({ type: "event", event: msg });
        if (msg.type === "wrapper_reconnected") { ws.sendHello(null); ws.sendListSessions(); }
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

  // interrupt-and-send: when state returns to idle, fire the pending message
  useEffect(() => {
    if (state.state === "idle" && state.pendingSend && wsRef.current) {
      const prompt = state.pendingSend;
      const msg_id = uuid();
      wsRef.current.sendQuery(prompt, msg_id);
      dispatch({ type: "query_sent", prompt, msg_id, ts: Date.now() });
      dispatch({ type: "clear_pending" });
    }
  }, [state.state, state.pendingSend]);

  // queue drain: when idle and queue non-empty, send the head (guard against
  // re-firing before the wrapper's state:running event arrives)
  useEffect(() => {
    if (state.state === "idle" && state.queue.length > 0 && !drainingRef.current && wsRef.current) {
      drainingRef.current = true;
      const next = state.queue[0];
      const msg_id = uuid();
      wsRef.current.sendQuery(next, msg_id);
      dispatch({ type: "query_sent", prompt: next, msg_id, ts: Date.now() });
      dispatch({ type: "dequeue_at", i: 0 });
    }
    if (state.state !== "idle") drainingRef.current = false;
  }, [state.state, state.queue]);

  // Persist turns to IndexedDB (coalesced) so reopening restores instantly.
  // Prefer the live seq cursor (ws.lastSeqValue): after a wrapper restart the
  // buffered seq namespace resets, and a stale cached lastSeqRef from the
  // previous lifetime would otherwise be saved forever (Math.max kept it).
  useEffect(() => {
    const sid = state.ccSessionId;
    if (!sid || state.turns.length === 0) return;
    const live = wsRef.current?.lastSeqValue || 0;
    saveSession(sid, state.turns, live || lastSeqRef.current);
  }, [state.turns, state.ccSessionId]);

  // Cmd/Ctrl+B => toggle sidebar; Cmd/Ctrl+Option+B => open latest turn's diff
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      const b = e.key === "b" || e.key === "B";
      if (!b) return;
      // Ctrl/Cmd+B => toggle left sidebar; Ctrl+Cmd+B => toggle right diff panel
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

  if (!authed) {
    return <LoginForm onLogin={(t) => { localStorage.setItem(SESSION_KEY, t); setAuthed(true); }} theme={theme} onToggleTheme={toggleTheme} />;
  }

  const sendQuery = (prompt: string, images?: QueryImg[], files?: QueryFile[]) => {
    if (!wsRef.current) return;
    const msg_id = uuid();
    wsRef.current.sendQuery(prompt, msg_id, images, files);
    dispatch({ type: "query_sent", prompt, msg_id, images, files, ts: Date.now() });
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
  // open the right-side artifact panel for a file changed in a turn:
  // Edit => diff (old→new), Write => render content (markdown)
  // fetch a git diff (context + line numbers) from the wrapper; "" = all files.
  // Pass the current theme so delta renders light/dark-appropriate colors.
  const getDiff = (file: string) => wsRef.current?.sendGetDiff(file, theme);
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
        activeSessionId={state.activeSessionId}
        onSelect={(id) => { wsRef.current?.sendSwitchSession(id); setSidebarOpen(false); }}
        onNew={() => { wsRef.current?.sendNewSession(); setSidebarOpen(false); }}
        onClose={() => setSidebarOpen(false)}
        onRename={(id, title) => wsRef.current?.sendRenameSession(id, title)}
        onArchive={(id, archived) => wsRef.current?.sendArchiveSession(id, archived)}
      />
      <section className="pane">
        <header className="c-head">
          <div className="titlewrap">
            <div className="ttl">
              <span className="brand" onClick={() => setSidebarOpen(true)} style={{ cursor: "pointer" }}>
                <span className="dot" />
                <span className="name serif"><b>cc</b><span>·remote</span></span>
              </span>
            </div>
            <div className="sub">{state.ccSessionId ? `session ${state.ccSessionId.slice(0, 8)}` : "connected"}</div>
          </div>
          <span className={`hstat ${state.state}`}><span className="sd" />{state.state}</span>
          <button className="iconbtn" onClick={toggleTheme} aria-label="切换主题">
            <Icon name={theme === "dark" ? "sun" : "moon"} />
          </button>
          <button className="iconbtn" onClick={logout} aria-label="退出"><Icon name="dots" /></button>
        </header>

        <ReconnectBanner banner={state.banner} replaying={state.replaying} truncated={state.truncated} />

        <ChatView turns={state.turns} onEdit={(prompt) => setEditPrompt(prompt)} onGetDiff={getDiff} />

        <Composer
          state={state.state}
          connState={state.connState}
          wrapperOnline={state.wrapperOnline}
          sendMode={state.sendMode}
          setSendMode={(m) => dispatch({ type: "set_send_mode", mode: m })}
          queue={state.queue}
          model={state.model}
          perm={state.perm}
          editPrompt={editPrompt}
          onEditConsumed={() => setEditPrompt(null)}
          onSendQuery={sendQuery}
          onInterrupt={interrupt}
          onEnqueue={(prompt) => dispatch({ type: "enqueue", prompt })}
          onSetPending={(prompt) => dispatch({ type: "set_pending", prompt })}
          onDequeue={(i) => dispatch({ type: "dequeue_at", i })}
          onSetModel={setModel}
          onSetPerm={setPerm}
          onClear={() => wsRef.current?.sendNewSession()}
          onContext={() => wsRef.current?.sendGetContext()}
        />
        {state.contextReport && (
          <>
            <div className="scrim show" onClick={() => dispatch({ type: "clear_context" })} />
            <div className="sheet show" role="dialog" aria-label="上下文用量">
              <div className="sheet-grip" />
              <div className="sheet-title">上下文用量 · {state.contextReport.model || ""}</div>
              <div className="sheet-scroll">
                <div className="ctx-overview">
                  <div className="ctx-pct">{state.contextReport.percentage.toFixed(1)}%</div>
                  <div className="ctx-bar"><div className="ctx-bar-fill" style={{ width: `${Math.min(state.contextReport.percentage, 100)}%` }} /></div>
                  <div className="ctx-numbers">{state.contextReport.total_tokens.toLocaleString()} / {state.contextReport.max_tokens.toLocaleString()} tokens</div>
                  {state.contextReport.is_auto_compact_enabled && <div className="ctx-auto">autocompact 已启用</div>}
                </div>
                <div className="ctx-cats">
                  {state.contextReport.categories.map((c, i) => (
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
      {state.pendingQuestion && (
        <QuestionSheet
          question={state.pendingQuestion.question}
          options={state.pendingQuestion.options}
          onAnswer={(answer) => {
            wsRef.current?.sendAnswerQuestion(state.pendingQuestion!.ask_id, answer);
            dispatch({ type: "answer_question" });
          }}
        />
      )}
    </div>
  );
}

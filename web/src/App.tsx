import { useEffect, useReducer, useRef, useState } from "react";
import { RelayWs } from "./ws";
import { uuid } from "./util";
import { reduce, initialState } from "./reducer";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { ReconnectBanner } from "./components/ReconnectBanner";
import { LoginForm } from "./components/LoginForm";

const SESSION_KEY = "cc_remote_session";

export default function App() {
  const [authed, setAuthed] = useState<boolean>(() => !!localStorage.getItem(SESSION_KEY));
  const [state, dispatch] = useReducer(reduce, initialState);
  const wsRef = useRef<RelayWs | null>(null);
  const [input, setInput] = useState("");

  useEffect(() => {
    if (!authed) return;
    const token = localStorage.getItem(SESSION_KEY) || "";
    const ws = new RelayWs(token, {
      onEvent: (msg) => {
        dispatch({ type: "event", event: msg });
        if (msg.type === "wrapper_reconnected") ws.resendHello();
      },
      onConnState: (s, detail) => dispatch({ type: "conn", connState: s, detail }),
      onAuthFail: () => {
        // session token invalid/expired — back to login
        localStorage.removeItem(SESSION_KEY);
        setAuthed(false);
      },
    });
    wsRef.current = ws;
    ws.start();
    return () => {
      ws.stop();
      wsRef.current = null;
    };
  }, [authed]);

  if (!authed) {
    return (
      <LoginForm
        onLogin={(token) => {
          localStorage.setItem(SESSION_KEY, token);
          setAuthed(true);
        }}
      />
    );
  }

  const busy = state.state === "running" || state.state === "interrupting";
  const canSend = state.connState === "connected" && !busy;

  const send = () => {
    const prompt = input.trim();
    if (!prompt || !wsRef.current) return;
    const msg_id = uuid();
    wsRef.current.sendQuery(prompt, msg_id);
    dispatch({ type: "query_sent", prompt, msg_id });
    setInput("");
  };
  const stop = () => wsRef.current?.sendInterrupt();

  const logout = () => {
    localStorage.removeItem(SESSION_KEY);
    wsRef.current?.stop();
    setAuthed(false);
  };

  return (
    <div className="app">
      <header className="topbar">
        <span className="title">cc-remote</span>
        <span className={`state state-${state.state}`}>{state.state}</span>
        <button className="logout" onClick={logout}>退出</button>
      </header>
      <ReconnectBanner banner={state.banner} replaying={state.replaying} truncated={state.truncated} />
      <ChatView turns={state.turns} />
      <Composer
        input={input}
        setInput={setInput}
        onSend={send}
        onStop={stop}
        busy={busy}
        canSend={canSend}
        connState={state.connState}
      />
    </div>
  );
}

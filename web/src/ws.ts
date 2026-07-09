// WebSocket client to the relay. Browsers can't set Authorization on a
// WebSocket, so the token goes in ?token=. Auto-reconnects with backoff.
//
// Multi-session: every inbound frame is demuxed by `msg.sid` (the wrapper
// stamps it). lastSeq is tracked PER session (lastSeqBySession) so catch-up
// cursors are per-session. session_focus just sets focusedSid + dispatches —
// no cursor reset, no re-hello (background turns keep streaming). All outbound
// commands that target a session stamp `sid: focusedSid`.
import type { ServerEvent, QueryImg, QueryFile } from "./protocol";
import { PROTOCOL_VERSION } from "./protocol";
import { uuid } from "./util";

export type ConnState = "connecting" | "connected" | "reconnecting" | "disconnected";

export interface WsCallbacks {
  onEvent: (msg: ServerEvent) => void;
  onConnState: (s: ConnState, detail?: string) => void;
  onAuthFail?: () => void;
}

function nowTs(): number {
  return Date.now() / 1000;
}

export class RelayWs {
  private ws: WebSocket | null = null;
  private lastSeqBySession: Record<string, number> = {};
  private focusedSid: string | null = null;
  // set while a new_session is in flight: the wrapper assigns + focuses it, so
  // that ONE SessionFocus must be honored even though it won't match focusedSid.
  private awaitingNewFocus = false;
  private readonly clientId: string;
  private readonly url: string;
  private backoff = 1;
  private stopped = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly token: string;
  private readonly cb: WsCallbacks;

  constructor(token: string, cb: WsCallbacks) {
    this.token = token;
    this.cb = cb;
    this.clientId = uuid();
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const u = new URL(`${proto}//${window.location.host}/ws`);
    u.searchParams.set("token", this.token);
    this.url = u.toString();
  }

  start(): void {
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }

  /** Highest seq across all known sessions (used by the App's IDB persist). */
  get lastSeqValue(): number {
    const vals = Object.values(this.lastSeqBySession);
    return vals.length ? Math.max(...vals) : 0;
  }

  /** Track the highest seq seen for a session (so reconnect replays from there). */
  noteSeq(sid: string | null | undefined, seq: number | null | undefined): void {
    if (sid && typeof seq === "number") {
      this.lastSeqBySession[sid] = Math.max(this.lastSeqBySession[sid] ?? 0, seq);
    }
  }

  /** Seed a session's cursor (e.g. from the IndexedDB cache on load). */
  setLastSeq(sid: string, seq: number): void {
    this.lastSeqBySession[sid] = seq;
  }

  /** Bulk-seed cursors from the IndexedDB cache BEFORE the first hello, so the
   *  wrapper replays only the delta (seq > lastSeq) instead of the whole history
   *  of every resident session — that flood is what wedged reconnect into a loop. */
  seedCursors(cursors: Record<string, number>): void {
    for (const [sid, seq] of Object.entries(cursors)) {
      if (typeof seq === "number" && seq > (this.lastSeqBySession[sid] ?? 0)) this.lastSeqBySession[sid] = seq;
    }
  }

  setFocusedSid(sid: string | null): void {
    this.focusedSid = sid;
    this.awaitingNewFocus = false; // an explicit switch supersedes a pending new-session focus
  }

  private sidObj(): Record<string, unknown> {
    return this.focusedSid ? { sid: this.focusedSid } : {};
  }

  sendQuery(prompt: string, msg_id: string, images?: QueryImg[], files?: QueryFile[]): void {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "query", prompt, msg_id, ts: nowTs(), ...this.sidObj() };
    if (images && images.length) obj.images = images;
    if (files && files.length) obj.files = files;
    this.send(obj);
  }

  // ---- /btw ephemeral side-fork ----
  sendOpenBtw(parentSid: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "open_btw", sid: parentSid, client_id: this.clientId, ts: nowTs() });
  }
  sendCloseBtw(btwSid: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "close_btw", sid: btwSid, ts: nowTs() });
  }
  // a turn targeted at an explicit sid (the btw fork), NOT the focused session.
  sendQueryTo(sid: string, prompt: string, msg_id: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "query", prompt, msg_id, sid, ts: nowTs() });
  }

  sendInterrupt(): void {
    this.send({ v: PROTOCOL_VERSION, type: "interrupt", ts: nowTs(), ...this.sidObj() });
  }

  sendSetModel(model: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_model", model, ts: nowTs(), ...this.sidObj() });
  }

  sendSetEffort(effort: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_effort", effort, ts: nowTs(), ...this.sidObj() });
  }

  sendSetServiceTier(service_tier: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_service_tier", service_tier, ts: nowTs(), ...this.sidObj() });
  }

  sendSetPerm(mode: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_perm", mode, ts: nowTs(), ...this.sidObj() });
  }

  sendGetContext(): void {
    this.send({ v: PROTOCOL_VERSION, type: "get_context", ts: nowTs(), ...this.sidObj() });
  }

  sendGetDiff(file: string, theme: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "get_diff", file, theme, ts: nowTs(), ...this.sidObj() });
  }

  /** Fetch a session's history as ONE bulk frame, read on-demand from its
   *  transcript (like a web chat's GET /conversation). client_id lets the wrapper
   *  route the History reply to=this client. Replaces per-hello buffer replay. */
  sendGetHistory(sessionId: string, before?: string | null, limit?: number | null): void {
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_history", session_id: sessionId,
      client_id: this.clientId, ts: nowTs(),
    };
    if (before) obj.before = before;
    if (limit) obj.limit = limit;
    this.send(obj);
  }

  sendAnswerQuestion(askId: string, answer: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "answer_question", ask_id: askId, answer, ts: nowTs(), ...this.sidObj() });
  }

  sendListSessions(engine?: "claude" | "codex"): void {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "list_sessions", ts: nowTs() };
    if (engine && engine !== "claude") obj.engine = engine;
    this.send(obj);
  }

  sendSwitchSession(sessionId: string, engine?: "claude" | "codex"): void {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "switch_session", session_id: sessionId, ts: nowTs() };
    if (engine && engine !== "claude") obj.engine = engine;
    this.send(obj);
  }

  sendNewSession(cwd?: string | null, engine?: "claude" | "codex", model?: string | null, effort?: string | null): void {
    this.awaitingNewFocus = true; // honor the wrapper's focus for the freshly-created session
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "new_session", ts: nowTs() };
    if (cwd) obj.cwd = cwd;
    // only include `engine` for codex, so claude new_session frames stay byte-identical
    // (an old relay that doesn't know the field never sees it on cc traffic).
    if (engine && engine !== "claude") obj.engine = engine;
    // model/effort only when the user explicitly pre-picked one on the new-chat
    // page; omitted => the wrapper uses its own defaults (no behavior change).
    if (model) obj.model = model;
    if (effort) obj.effort = effort;
    this.send(obj);
  }

  sendRenameSession(sessionId: string, title: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "rename_session", session_id: sessionId, title, ts: nowTs() });
  }

  sendArchiveSession(sessionId: string, archived: boolean): void {
    this.send({ v: PROTOCOL_VERSION, type: "archive_session", session_id: sessionId, archived, ts: nowTs() });
  }

  sendListDir(path?: string | null): void {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "list_dir", ts: nowTs() };
    if (path) obj.path = path;
    this.send(obj);
  }

  /** Send hello with the per-session cursor map (multi-session catch-up). */
  sendHello(): void {
    this.send({
      v: PROTOCOL_VERSION, type: "hello", role: "client", client_id: this.clientId,
      cursors: { ...this.lastSeqBySession }, ts: nowTs(),
    });
  }

  private send(obj: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  private connect(): void {
    this.cb.onConnState("connecting");
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.backoff = 1;
      this.sendHello();  // cursors = whatever we remember (empty on first connect)
      this.cb.onConnState("connected");  // triggers sendListSessions
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as ServerEvent;
        if (msg.type === "session_focus") {
          // Drop a STALE switch-confirmation: when you click through several
          // sessions quickly, the wrapper processes each switch in turn and emits
          // a SessionFocus for every one. Honoring the late ones would "replay"
          // your clicks — yanking the view through each session. Only honor a
          // focus that matches your current intent (or the one new_session is
          // waiting for, or the very first focus when we have none yet).
          if (this.focusedSid != null && msg.session_id !== this.focusedSid && !this.awaitingNewFocus) {
            return; // superseded — ignore
          }
          this.awaitingNewFocus = false;
          this.focusedSid = msg.session_id;
          this.cb.onEvent(msg);
          return;
        }
        if (msg.type === "session_rekey") {
          // Runtime re-key (tmp -> real id): migrate the cursor and, ONLY if we
          // were viewing old_key, the focus. Never a focus change by itself.
          const { old_key, session_id } = msg;
          if (old_key !== session_id) {
            if (this.lastSeqBySession[old_key] != null && this.lastSeqBySession[session_id] == null) {
              this.lastSeqBySession[session_id] = this.lastSeqBySession[old_key];
            }
            delete this.lastSeqBySession[old_key];
            if (this.focusedSid === old_key) this.focusedSid = session_id;
          }
          this.cb.onEvent(msg);
          return;
        }
        if (msg.type === "replay_start" && msg.rebuild && msg.sid) {
          // The wrapper is REBUILDING this session (evicted + re-spawned, seq
          // reset to 0). Drop our stale cursor so the rebuild frames — whose
          // seqs restart low — actually advance it instead of being ignored by
          // the Math.max in noteSeq.
          this.lastSeqBySession[msg.sid] = 0;
        }
        this.noteSeq(msg.sid, (msg as { seq?: number | null }).seq);
        this.cb.onEvent(msg);
      } catch (err) {
        console.warn("dropping malformed frame", err);
      }
    };
    ws.onclose = (ev: CloseEvent) => {
      this.ws = null;
      if (ev.code === 1008) {
        this.cb.onAuthFail?.();
        return;
      }
      if (!this.stopped) this.scheduleReconnect();
    };
    ws.onerror = () => {
      /* onclose will follow */
    };
  }

  private scheduleReconnect(): void {
    this.cb.onConnState("reconnecting");
    this.reconnectTimer = setTimeout(() => this.connect(), this.backoff * 1000);
    this.backoff = Math.min(this.backoff * 2, 5);  // cap at 5s so reconnect recovers fast
  }
}

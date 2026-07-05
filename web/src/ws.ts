// WebSocket client to the relay. Browsers can't set Authorization on a
// WebSocket, so the token goes in ?token=. Derives the URL from the page
// location so it works both in Vite dev (proxied /ws) and in production
// (relay serves the build on the same origin). Auto-reconnects with backoff
// and re-hellos with last_seq on every (re)connect and on wrapper_reconnected.
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
  private lastSeq = 0;
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
    this.connect(false);
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }

  get lastSeqValue(): number {
    return this.lastSeq;
  }

  /** Track the highest seq seen (so reconnect replays from there). */
  noteSeq(seq: number | null | undefined): void {
    if (typeof seq === "number" && seq > this.lastSeq) this.lastSeq = seq;
  }

  sendQuery(prompt: string, msg_id: string, images?: QueryImg[], files?: QueryFile[]): void {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "query", prompt, msg_id, ts: nowTs() };
    if (images && images.length) obj.images = images;
    if (files && files.length) obj.files = files;
    this.send(obj);
  }

  sendInterrupt(): void {
    this.send({ v: PROTOCOL_VERSION, type: "interrupt", ts: nowTs() });
  }

  sendSetModel(model: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_model", model, ts: nowTs() });
  }

  sendSetPerm(mode: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_perm", mode, ts: nowTs() });
  }

  sendGetContext(): void {
    this.send({ v: PROTOCOL_VERSION, type: "get_context", ts: nowTs() });
  }

  sendListSessions(): void {
    this.send({ v: PROTOCOL_VERSION, type: "list_sessions", ts: nowTs() });
  }

  sendSwitchSession(sessionId: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "switch_session", session_id: sessionId, ts: nowTs() });
  }

  sendNewSession(): void {
    this.send({ v: PROTOCOL_VERSION, type: "new_session", ts: nowTs() });
  }

  sendRenameSession(sessionId: string, title: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "rename_session", session_id: sessionId, title, ts: nowTs() });
  }

  sendArchiveSession(sessionId: string, archived: boolean): void {
    this.send({ v: PROTOCOL_VERSION, type: "archive_session", session_id: sessionId, archived, ts: nowTs() });
  }

  /** Send hello with an explicit last_seq. `null` = first hello (wrapper replies
   *  with a snapshot so the app can read its IndexedDB cache); an int = catch-up
   *  (wrapper replays only events with seq > last_seq). */
  sendHello(lastSeq: number | null): void {
    this.send({
      v: PROTOCOL_VERSION, type: "hello", role: "client", client_id: this.clientId,
      last_seq: lastSeq, ts: nowTs(),
    });
  }

  private send(obj: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  private connect(reconnect: boolean): void {
    this.cb.onConnState(reconnect ? "reconnecting" : "connecting");
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.backoff = 1;
      this.sendHello(null);  // first hello -> wrapper sends snapshot -> app reads cache + catchUp
      this.cb.onConnState("connected");  // triggers sendListSessions after hello
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as ServerEvent;
        if (msg.type === "session_switched") {
          // new active session: reset the seq cursor; the snapshot from the new
          // session will trigger a cache read + catchUp in the app.
          this.lastSeq = 0;
          this.cb.onEvent(msg);
          this.sendHello(null);
          return;
        }
        this.noteSeq(msg.seq);
        this.cb.onEvent(msg);
      } catch (err) {
        console.warn("dropping malformed frame", err);
      }
    };
    ws.onclose = (ev: CloseEvent) => {
      this.ws = null;
      if (ev.code === 1008) {
        // Auth rejected (invalid/expired session token). Stop retrying — let
        // the app drop the session and show the login page.
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
    this.reconnectTimer = setTimeout(() => this.connect(true), this.backoff * 1000);
    this.backoff = Math.min(this.backoff * 2, 30);
  }
}

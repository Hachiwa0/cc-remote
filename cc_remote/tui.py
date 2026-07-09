"""Interactive terminal client for cc-remote — Option B: the terminal as a client.

Runs in your terminal and ATTACHES to a wrapper-owned session over the relay, so
the terminal and the phone/browser are all clients of the SAME session and sync
bidirectionally: send here → the phone sees it; send on the phone → it shows up
here. The agent still runs on the wrapper host (this is a thin view+input, like
the web client), reusing the exact same wire protocol — so the relay/wrapper need
zero changes.

This is NOT the native `claude` TUI; it's a cc-remote client rendered in the
terminal. That's the trade for true two-way sync (see the design discussion).

Env:
  RELAY_URL     default wss://muggle-remote.cc/ws
  CLIENT_TOKEN  the relay's client token (Bearer auth); required
  ENGINE        claude | codex   (which store to list / which backend for /new)

Usage:
  python -m cc_remote.tui [session_id]     # attach to session_id, or pick from a list

Commands (each on its own line):
  <text>            send as a query to the attached session
  /sessions         list sessions (then /attach <n>)
  /attach <n|id>    attach to a session by list-number or id
  /new [cwd]        start a new session (cwd defaults to ~)
  /stop             interrupt the running turn
  /model [id]       set model (no arg = list options)
  /effort <id>      set reasoning effort (low|medium|high|xhigh|max)
  /context          show context-window usage
  /engine <e>       switch engine (claude|codex) for listing/new
  /help             show commands
  /quit
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from typing import Optional

import cc_remote.config  # noqa: F401  (import side-effect: loads .env)
from cc_remote.log import logger, setup
from cc_remote.protocol import (
    Hello, Query, Interrupt, SetModel, SetEffort, GetContext,
    GetHistory, ListSessions, SwitchSession, NewSession, AnswerQuestion,
    Ping, serialize,
)
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

setup("cc_remote.tui", os.environ.get("LOG_LEVEL", "WARNING"))
log = logger("cc_remote.tui")

RELAY_URL = os.environ.get("RELAY_URL", "wss://muggle-remote.cc/ws")
CLIENT_TOKEN = os.environ.get("CLIENT_TOKEN", "")
ENGINE = os.environ.get("ENGINE", "claude")

# ---- ANSI helpers (disabled when stdout isn't a tty) ----
_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def DIM(s: str) -> str: return _c("2", s)
def GREEN(s: str) -> str: return _c("32", s)
def CYAN(s: str) -> str: return _c("36", s)
def RED(s: str) -> str: return _c("31", s)
def YELLOW(s: str) -> str: return _c("33", s)
def BOLD(s: str) -> str: return _c("1", s)


def _short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Tui:
    def __init__(self, url: str, token: str, engine: str, want_sid: Optional[str]):
        self.url = url
        self.token = token
        self.engine = engine
        self.client_id = uuid.uuid4().hex
        self.attached_sid: Optional[str] = want_sid
        self.want_sid = want_sid            # attach target passed on the CLI
        self.cursors: dict[str, int] = {}
        self.sessions: list[dict] = []      # last session_list payload
        self.sent_msg_ids: set[str] = set()  # our own queries — dedup the user_msg echo
        self.pending_ask: Optional[dict] = None  # ask_user awaiting a numeric answer
        self._awaiting_new = False          # a /new is in flight; honor its SessionFocus
        self.state = "idle"
        self._midline = False               # cursor is mid-line from streaming deltas
        self.ws = None
        self.last_recv = 0.0                 # monotonic time of the last inbound frame (heartbeat)
        self._ping_n = 0
        self._quitting = False
        self._cmd_q: asyncio.Queue = asyncio.Queue()
        self._stdin_task: Optional[asyncio.Task] = None

    # ---- output helpers (keep streaming deltas and block prints from colliding) ----

    def _nl(self) -> None:
        if self._midline:
            sys.stdout.write("\n")
            self._midline = False

    def _line(self, s: str) -> None:
        self._nl()
        sys.stdout.write(s + "\n")
        sys.stdout.flush()

    def _write(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()
        self._midline = not s.endswith("\n")

    # ---- lifecycle ----

    async def run(self) -> None:
        if not self.token:
            print(RED("CLIENT_TOKEN is not set — cannot authenticate. Set it in the env / .env."))
            return
        self._stdin_task = asyncio.create_task(self._stdin_reader())
        self._line(BOLD("cc-remote tui") + DIM(f"  client={self.client_id[:8]}  engine={self.engine}"))
        self._line(DIM("commands: /sessions /attach <n|id> /new [cwd] /stop /model /effort /context /engine <e> /help /quit"))
        try:
            await self._connection_loop()
        finally:
            if self._stdin_task:
                self._stdin_task.cancel()

    async def _stdin_reader(self) -> None:
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                await self._cmd_q.put("/quit")
                return
            await self._cmd_q.put(line.decode(errors="replace").rstrip("\n"))

    async def _connection_loop(self) -> None:
        backoff = 1.0
        while not self._quitting:
            try:
                async with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                                   max_size=32 * 1024 * 1024, close_timeout=3, open_timeout=30) as ws:
                    self.ws = ws
                    await self._send(Hello(role="client", client_id=self.client_id,
                                           cursors=dict(self.cursors) or None))
                    self._line(GREEN(f"[connected to {self.url}]"))
                    # ask for the session list; attach flow continues when it arrives
                    await self._send(ListSessions(engine=self.engine))
                    if self.attached_sid:
                        await self._attach(self.attached_sid)
                    backoff = 1.0
                    await self._session(ws)
            except Exception as e:  # noqa: BLE001 — surface + retry
                if not self._quitting:
                    self._line(RED(f"[conn error: {e}] retry in {backoff:.0f}s"))
            if not self._quitting:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def _session(self, ws) -> None:
        self.last_recv = time.monotonic()
        recv = asyncio.create_task(self._receiver(ws))
        cmd = asyncio.create_task(self._cmd_consumer(ws))
        hb = asyncio.create_task(self._heartbeat(ws))
        _, pending = await asyncio.wait({recv, cmd, hb}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

    async def _heartbeat(self, ws) -> None:
        # Same half-open guard as the web client. If the WS silently dies (dead TCP,
        # no close frame — common on flaky/proxied links), `async for raw in ws`
        # never returns and we'd stop receiving broadcasts without noticing (no
        # error → no reconnect → the terminal misses messages other clients send).
        # Ping every 20s; if NO frame arrives for 45s, close the ws so the
        # connection loop reconnects — which re-attaches + re-pulls history,
        # backfilling anything missed during the gap.
        while True:
            await asyncio.sleep(20)
            if time.monotonic() - self.last_recv > 45:
                self._line(DIM("[链路空闲,重连中…]"))
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            try:
                self._ping_n += 1
                await ws.send(serialize(Ping(n=self._ping_n)))
            except Exception:
                return

    async def _receiver(self, ws) -> None:
        try:
            async for raw in ws:
                self.last_recv = time.monotonic()  # any frame proves the link is alive
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                self._handle(d)
        except ConnectionClosed:
            pass  # normal on /quit or a dropped link — the loop reconnects if needed

    async def _send(self, msg) -> None:
        try:
            if self.ws:
                await self.ws.send(serialize(msg))
        except Exception as e:  # noqa: BLE001
            self._line(RED(f"[send failed: {e}]"))

    # ---- commands ----

    async def _attach(self, sid: str) -> None:
        self.attached_sid = sid
        self.sent_msg_ids.clear()
        self._line(CYAN(f"[attaching {sid}…]"))
        # switch_session makes it resident (spawns/resumes) so queries land; then
        # pull its history from the transcript like the web client does.
        await self._send(SwitchSession(session_id=sid, engine=self.engine))
        await self._send(GetHistory(session_id=sid, client_id=self.client_id))

    async def _cmd_consumer(self, ws) -> None:
        while True:
            line = await self._cmd_q.get()
            s = line.strip()
            if not s:
                continue
            # answering an ask_user prompt: a bare number picks an option
            if self.pending_ask and s.isdigit():
                await self._answer_ask(int(s))
                continue
            if s.startswith("/"):
                await self._command(s)
            else:
                await self._query(s)
            if self._quitting:
                return

    async def _query(self, text: str) -> None:
        if not self.attached_sid:
            self._line(YELLOW("[no session attached — /sessions then /attach <n>, or /new]"))
            return
        mid = uuid.uuid4().hex
        self.sent_msg_ids.add(mid)
        self._line(GREEN("» ") + text)
        await self._send(Query(prompt=text, msg_id=mid, sid=self.attached_sid))

    async def _command(self, s: str) -> None:
        parts = s.split(maxsplit=1)
        cmd = parts[0][1:].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("quit", "q", "exit"):
            self._quitting = True
            try:
                if self.ws:
                    await self.ws.close()
            except Exception:
                pass
        elif cmd in ("help", "h"):
            self._line(DIM("/sessions /attach <n|id> /new [cwd] /stop /model [id] /effort <id> /context /engine <e> /quit"))
        elif cmd == "sessions":
            await self._send(ListSessions(engine=self.engine))
            self._line(DIM("[refreshing sessions…]"))
        elif cmd == "attach":
            await self._do_attach_arg(arg)
        elif cmd == "new":
            cwd = arg or "~"
            self._awaiting_new = True   # honor the wrapper's SessionFocus for it
            self._line(CYAN(f"[new {self.engine} session in {cwd}]"))
            await self._send(NewSession(cwd=cwd, engine=self.engine))
        elif cmd == "stop":
            if self.attached_sid:
                await self._send(Interrupt(sid=self.attached_sid))
                self._line(DIM("[interrupt sent]"))
        elif cmd == "model":
            if arg:
                await self._send(SetModel(model=arg, sid=self.attached_sid))
                self._line(CYAN(f"[model → {arg}]"))
            else:
                self._line(DIM("usage: /model <id>   (e.g. claude-opus-4-8 / gpt-5.5)"))
        elif cmd == "effort":
            if arg:
                await self._send(SetEffort(effort=arg, sid=self.attached_sid))
                self._line(CYAN(f"[effort → {arg}]"))
            else:
                self._line(DIM("usage: /effort <low|medium|high|xhigh|max>"))
        elif cmd == "context":
            if self.attached_sid:
                await self._send(GetContext(sid=self.attached_sid))
        elif cmd == "engine":
            if arg in ("claude", "codex"):
                self.engine = arg
                await self._send(ListSessions(engine=self.engine))
                self._line(CYAN(f"[engine → {arg}] (applies to /sessions and /new)"))
            else:
                self._line(DIM("usage: /engine <claude|codex>"))
        else:
            self._line(YELLOW(f"[unknown command: /{cmd}] — /help"))

    async def _do_attach_arg(self, arg: str) -> None:
        if not arg:
            self._line(DIM("usage: /attach <list-number|session-id>"))
            return
        sid = arg
        if arg.isdigit():
            i = int(arg) - 1
            if 0 <= i < len(self.sessions):
                sid = self.sessions[i]["session_id"]
            else:
                self._line(YELLOW(f"[no session #{arg}] — /sessions"))
                return
        await self._attach(sid)

    async def _answer_ask(self, n: int) -> None:
        ask = self.pending_ask
        if not ask:
            return
        opts = ask.get("options") or []
        if not (1 <= n <= len(opts)):
            self._line(YELLOW(f"[pick 1..{len(opts)}]"))
            return
        answer = opts[n - 1].get("label", str(n))
        await self._send(AnswerQuestion(ask_id=ask["ask_id"], answer=answer, sid=self.attached_sid))
        self._line(CYAN(f"[answered: {answer}]"))
        self.pending_ask = None

    # ---- inbound event rendering ----

    def _for_me(self, d: dict) -> bool:
        """Narrative events are session-scoped; only render the attached one (so a
        background session another device is driving doesn't bleed into our view).
        Before attaching, render nothing narrative — the user picks a session first."""
        return self.attached_sid is not None and d.get("sid") == self.attached_sid

    def _handle(self, d: dict) -> None:
        t = d.get("type")
        # track per-session cursor for reconnect
        sid, seq = d.get("sid"), d.get("seq")
        if isinstance(sid, str) and isinstance(seq, int):
            self.cursors[sid] = max(self.cursors.get(sid, 0), seq)

        if t == "session_list":
            self._render_sessions(d.get("sessions") or [])
        elif t == "history":
            self._render_history(d)
        elif t == "session_focus":
            # only auto-attach to a session WE just created via /new; ignore other
            # focus frames (a switch confirmation, or a focus another device caused).
            nsid = d.get("session_id")
            if nsid and self._awaiting_new:
                self._awaiting_new = False
                self.attached_sid = nsid
                self.sent_msg_ids.clear()
                self._line(CYAN(f"[new session {nsid} — start typing]"))
        elif t == "session_rekey":
            if d.get("old_key") == self.attached_sid:
                self.attached_sid = d.get("session_id")
        elif t == "user_msg":
            if self._for_me(d):
                if d.get("msg_id") in self.sent_msg_ids:
                    return  # our own — already echoed locally
                self._line(GREEN("» ") + str(d.get("prompt", "")))
        elif t == "assistant_msg_start":
            if self._for_me(d):
                self._nl()
                self._write(DIM("🤖 "))
        elif t == "delta":
            if self._for_me(d):
                self._write(str(d.get("text", "")))
        elif t == "assistant_msg_end":
            if self._for_me(d):
                self._nl()
        elif t == "tool_use":
            if self._for_me(d):
                inp = _short(json.dumps(d.get("input", {}), ensure_ascii=False), 120)
                self._line(DIM(f"  ⚙ {d.get('tool')} {inp}"))
        elif t == "tool_result":
            if self._for_me(d):
                tag = RED("err") if d.get("is_error") else DIM("ok")
                trunc = DIM(" (truncated)") if d.get("truncated") else ""
                self._line(DIM("  ↳ ") + tag + trunc + " " + DIM(_short(d.get("content", ""), 160)))
        elif t == "turn_end":
            if self._for_me(d):
                r = d.get("result") or {}
                bad = r.get("is_error")
                msg = f"— turn {r.get('subtype','?')} {r.get('duration_ms','?')}ms —"
                self._line(RED(msg) if bad else DIM(msg))
        elif t == "state":
            if self._for_me(d):
                new = d.get("state", "idle")
                if new != self.state:
                    self.state = new
                    if new in ("running", "idle"):
                        self._line(DIM(f"[{new}]"))
        elif t == "model":
            if self._for_me(d):
                self._line(CYAN(f"[model: {d.get('model')}]"))
        elif t == "effort":
            if self._for_me(d):
                self._line(CYAN(f"[effort: {d.get('effort')}]"))
        elif t == "perm":
            if self._for_me(d):
                self._line(CYAN(f"[perm: {d.get('mode')}]"))
        elif t == "fast":
            if self._for_me(d):
                self._line(CYAN("[fast: " + ("on" if d.get("on") else "off") + "]"))
        elif t == "error":
            self._line(RED(f"!! {d.get('code')}: {d.get('message')}"))
        elif t == "ask_user":
            if self._for_me(d):
                self._render_ask(d)
        elif t == "context_report":
            self._render_context(d)
        elif t == "wrapper_disconnected":
            self._line(YELLOW("[wrapper offline — waiting…]"))
        elif t == "wrapper_reconnected":
            self._line(GREEN("[wrapper back]"))
        # snapshot / dir_list / diff_report / replay_* / pong: quietly ignored

    def _render_sessions(self, sessions: list[dict]) -> None:
        # hide archived; keep order (wrapper sorts by recency)
        self.sessions = [s for s in sessions if s.get("tag") != "archived"]
        if not self.sessions:
            self._line(DIM("[no sessions] — /new to start one"))
            return
        self._line(BOLD(f"sessions ({self.engine}):"))
        for i, s in enumerate(self.sessions[:20], 1):
            sid = (s.get("session_id") or "")[:8]
            mark = "●" if s.get("state") in ("running", "idle") else "·"
            here = GREEN(" ←attached") if s.get("session_id") == self.attached_sid else ""
            label = s.get("summary") or s.get("first_prompt") or "(empty)"
            self._line(f"  {i:>2} {mark} {DIM(sid)} {_short(label, 60)}{here}")
        self._line(DIM("→ /attach <n>"))

    def _render_history(self, d: dict) -> None:
        if d.get("session_id") != self.attached_sid:
            return
        events = d.get("events") or []
        if not events:
            self._line(DIM("── (no history) ──"))
            return
        self._line(DIM(f"── history ({len(events)} events) ──"))
        for ev in events:
            # render through the same path; suppress own-echo dedup for history
            self._handle_history_event(ev)
        self._nl()
        self._line(DIM("── live ──"))

    def _handle_history_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "user_msg":
            self._line(GREEN("» ") + str(ev.get("prompt", "")))
        elif t == "assistant_msg_start":
            self._nl(); self._write(DIM("🤖 "))
        elif t == "delta":
            self._write(str(ev.get("text", "")))
        elif t == "assistant_msg_end":
            self._nl()
        elif t == "tool_use":
            inp = _short(json.dumps(ev.get("input", {}), ensure_ascii=False), 120)
            self._line(DIM(f"  ⚙ {ev.get('tool')} {inp}"))
        elif t == "tool_result":
            tag = RED("err") if ev.get("is_error") else DIM("ok")
            self._line(DIM("  ↳ ") + tag + " " + DIM(_short(ev.get("content", ""), 160)))
        # turn_end/state/model in history: skip (noise)

    def _render_ask(self, d: dict) -> None:
        self.pending_ask = {"ask_id": d.get("ask_id"), "options": d.get("options") or []}
        self._line(YELLOW("? ") + BOLD(str(d.get("question", ""))))
        for i, o in enumerate(self.pending_ask["options"], 1):
            ds = o.get("ds")
            self._line(f"  {i}. {o.get('label','')}" + (DIM(f"  — {ds}") if ds else ""))
        self._line(DIM("→ type the number to answer"))

    def _render_context(self, d: dict) -> None:
        tot, mx = d.get("total_tokens", 0), d.get("max_tokens", 0)
        pct = d.get("percentage", 0)
        self._line(CYAN(f"[context: {tot:,}/{mx:,} tokens ({pct:.0f}%)  {d.get('model','')}]"))


def main() -> None:
    want_sid = sys.argv[1] if len(sys.argv) > 1 else None
    tui = Tui(RELAY_URL, CLIENT_TOKEN, ENGINE if ENGINE in ("claude", "codex") else "claude", want_sid)
    try:
        asyncio.run(tui.run())
    except KeyboardInterrupt:
        print("\n" + DIM("[bye]"))


if __name__ == "__main__":
    main()

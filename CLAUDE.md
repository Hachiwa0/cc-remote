# CLAUDE.md — cc-remote

## What this is
Self-hosted remote control for Claude Code. A phone/browser drives a cc session
on a company machine via a WebSocket relay. Two independent links: the **model
link** (cc → local proxy `127.0.0.1:19191` → z.AI GLM) is untouched and
inherited from `~/.claude/settings.json`; we only build the **control link**
(client ⇄ relay ⇄ wrapper ⇄ `ClaudeSDKClient` ⇄ cc).

## Critical constraints / traps
- **Drain footgun**: after `ClaudeSDKClient.interrupt()`, the SDK does NOT kill
  the session — the current turn's stream still emits a terminal
  `ResultMessage(subtype="error_during_execution")`. You MUST keep consuming
  `receive_response()` until that ResultMessage before issuing the next
  `query()`, else stale deltas from the interrupted turn bleed into the new
  turn. The wrapper handles this structurally: one `async for` per turn runs
  through the interrupt to the terminal ResultMessage; state only returns to
  `idle` (and the next query is only accepted) after that break. MVP also uses
  reject-while-busy so no second query can race the drain.
- **cwd must match resume**: session jsonl lives at
  `~/.claude/projects/<cwd-with-/-as->/<uuid>.jsonl`. `ClaudeAgentOptions.cwd`
  MUST equal the original session's cwd or `resume` can't find it.
- **SDK pinned to `claude-agent-sdk==0.2.110`**: message-type shapes and the
  interrupt/drain contract can shift between minor versions. Re-run the
  interrupt+drain verification after any upgrade.
- **`include_partial_messages`** is a `ClaudeAgentOptions` dataclass field
  (set at construction, not on `query()`). Partial/streaming events arrive as
  `StreamEvent` (with `.event` = raw Anthropic API stream event dict) — NOT
  `SDKPartialAssistantMessage` (that type does not exist in 0.2.110). Extract
  `content_block_delta` → `delta.text` from `StreamEvent.event`.
- **tool_use is batched, not streamed**: emit one `tool_use` event from the
  assembled `AssistantMessage` (full `input`), never as JSON-fragment deltas.
  Text deltas still stream live via `StreamEvent`.
- **Don't set `setting_sources=[]`**: we WANT `~/.claude/settings.json` loaded
  so cc inherits the model link (`ANTHROPIC_BASE_URL`), model id, and
  `bypassPermissions`. The Orca hooks are no-op without `ORCA_*` env vars, so
  leaving them loaded is safe.
- **Auth is header-only**: `Authorization: Bearer <token>` at WS upgrade. Never
  put tokens in message bodies; the logger redacts token-named fields.
- **Multi-session routing key**: the wrapper runs a POOL of resident sessions
  (`WrapperMachine.sessions: dict[key, SessionContext]`, cap
  `MAX_CONCURRENT_SESSIONS`). `ctx.key` is the routing identity = the real cc sid
  once known, else `tmp-<uuid>`. Every emit stamps `sid = ctx.session_id or
  ctx.key`, so a brand-new session's pre-capture frames route deterministically
  (never leak into the focused runtime). Keep `ctx.key` in sync with the pool
  dict key on every re-key.
- **Focus vs re-key (don't conflate)**: switching the viewed session is
  `SessionFocus` (focus only, no disconnect — the previous session keeps
  streaming). A new session capturing its real id mid-turn is `SessionRekey`
  (rename tmp-key→sid + migrate cursor), which moves focus ONLY if the client was
  already viewing the temp key. Emitting SessionFocus on id-capture = focus-steal
  by background sessions.
- **Evict/re-focus rebuilds, never merges**: over the cap, an idle non-focused
  session is evicted (subprocess torn down); re-focusing re-spawns it with a
  FRESH ring buffer (seq resets to 0). Catch-up MUST use the rebuild replay
  (`replay_from(..., rebuild=True)` → `ReplayStart(rebuild=True)`) so the client
  discards its stale turns and rebuilds; the client also resets its per-session
  cursor to 0 on a rebuild frame. Token-aware: resume = cold prompt cache = full
  context re-send, so it only happens on first spawn / re-focus-after-eviction —
  raising the cap trades RAM for fewer cold re-sends.

## Module map
- `cc_remote/protocol.py` — pydantic wire schema; all modules depend on it.
  `serialize`/`deserialize` with `v` check; `is_downstream` for seq/buffer.
  Multi-session control frames: `SessionFocus` / `SessionRekey` / `SessionInfo.state`.
- `cc_remote/config.py` — env-driven config (RelayConfig, WrapperConfig,
  `max_concurrent_sessions`).
- `cc_remote/log.py` — JSON logging with token redaction; use `logger("...")`.
- `cc_remote/wrapper/` — sdk.py (client lifecycle), machine.py (session pool +
  per-ctx state machine + drain), session_ctx.py (one SessionContext per resident
  session), stream.py (SDK→protocol translate), ringbuffer.py (seq + replay +
  rebuild), transport.py (WS client to relay), session.py (session id persistence).
- `cc_remote/relay/` — server.py (FastAPI /ws + static), auth.py (bearer),
  pairing.py (single wrapper slot + `to=`/broadcast fan-out), forward.py (per-client queues).

## Run / test
```bash
pip install -r requirements.txt
python -m cc_remote.relay        # terminal 1
python -m cc_remote.wrapper      # terminal 2 (on the cc machine)
python -m tests.cli_client       # terminal 3
pytest                           # unit tests
```
End-to-end verification cases are in the plan file
(`~/.claude/plans/keen-spinning-breeze.md`), especially the interrupt+drain
test (#2) which directly checks the footgun is handled.

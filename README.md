# cc-remote

Self-hosted remote control for Claude Code — drive a cc session running on a
machine from a phone/browser, relayed through a VPS. A clone of Claude Code's
official Remote Control, built because the official feature requires
`api.anthropic.com` + a claude.ai subscription and is incompatible with a
custom `ANTHROPIC_BASE_URL` (this setup routes cc through a local proxy to
z.AI GLM).

## Architecture — two independent links

```
MODEL LINK (untouched):    cc -> 127.0.0.1:19191 local proxy -> z.AI GLM

CONTROL LINK (this repo):  client ⇄ relay(WebSocket) ⇄ wrapper ⇄ ClaudeSDKClient ⇄ cc
```

The relay is a pure WebSocket forwarder — it never imports `claude_agent_sdk`,
never touches `ANTHROPIC_BASE_URL` or the model API. cc + wrapper + the local
proxy all run on the company machine; the relay is portable and can move to a
VPS with only env changes.

## Components

- `cc_remote/wrapper/` — company-machine daemon. Holds a `ClaudeSDKClient`
  session, translates SDK events to the wire protocol, assigns monotonic `seq`,
  keeps a ring buffer for reconnect replay, handles `interrupt` + drain.
- `cc_remote/relay/` — FastAPI WebSocket relay. Bearer-token auth, single
  wrapper slot, multi-client fan-out, per-client send queues.
- `tests/cli_client.py` — Phase 1 test client (REPL).
- `web/` — Phase 2 React client (phone browser, later Capacitor APK).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit tokens + CC_CWD
```

`claude` CLI (v2.1.51+) must be on PATH; `claude-agent-sdk==0.2.110` is pinned.

## Run (Phase 1, three terminals)

```bash
# 1) relay
python -m cc_remote.relay

# 2) wrapper (on the machine where cc runs)
python -m cc_remote.wrapper

# 3) CLI test client
python -m tests.cli_client
```

Type a prompt → see streamed tokens; Ctrl-C sends `interrupt`; type `r` to
reconnect and test replay.

## Security notes

- `settings.json` has `skipDangerousModePermissionPrompt: true` and the live
  session runs `permissionMode: bypassPermissions`, so the agent can run
  arbitrary shell/edits on the company machine with **no prompt**. That is
  intentional for unattended remote control — make sure you trust anyone who
  can reach the relay's client token.
- Tokens (`CLIENT_TOKEN` / `WRAPPER_TOKEN`) are the only thing standing between
  a phone and full control of the machine. Rotate them; never log them (the
  logger redacts them, but don't paste them elsewhere).
- Local/LAN runs use plain `ws://`. Before exposing the relay on a VPS, put TLS
  in front (Caddy → `wss://`) — see Phase 3 in the plan.

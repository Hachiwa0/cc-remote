# Changelog

## v3.0.0 — 2026-07-23

cc-remote v3 keeps the established Claude Code + Codex and Code + Work product
surface, while rebuilding the control plane for very long histories, native
client coexistence, multiple wrapper machines, and reliable mobile use.

### Local-first history

- Paint the browser's last validated IndexedDB projection before network
  validation.
- Materialize source-fingerprinted turn summaries in a rebuildable wrapper
  SQLite index.
- Load newest turns first, page older history, and fetch heavy tool/reasoning
  detail only when that turn is expanded.
- Preserve the viewport while prepending pages and converge appended sources in
  the background.
- Resolve historical image assets on demand instead of embedding them in every
  history page.

### Long Codex sessions and native lifecycle

- Read Codex rollouts backward by turn without re-uploading history to the model
  or replacing app-server-native resume and compaction state.
- Add a narrowly gated official HTTP transport fallback for oversized Codex
  Desktop + OpenAI resumes whose WebSocket closes before completion.
- Keep Codex shared-daemon CLI activity distinct from private Codex App
  ownership.
- Bind prompts, steering, commentary, tools, compaction, aborts, and completion
  to their authoritative turn so history cannot drift to the bottom.
- Mirror interrupted and externally running work without stale read-only locks
  or permanent thinking indicators.

### Devices and ownership

- Add Device Center, expiring single-use pairing codes, hashed machine
  credentials, rename/revoke controls, and online state.
- Enforce account-to-machine authorization on discovery, commands, events, and
  push subscriptions.
- Scope working directories and delayed focus/rekey frames by device, surface,
  engine, socket generation, and session ownership.
- Add shared Darwin/Linux process identity scanning for native Claude ownership
  while keeping takeover limited to an exact same-user process.

### Mobile and artifact experience

- Add stable upward history pagination, local-first session switching, and
  bounded live-tail replay.
- Add on-demand conversation images and a touch-friendly lightbox with
  tap-to-close and pinch zoom.
- Keep Markdown relative links/images, source files, sanitized HTML, PDFs, and
  sandbox-converted Office previews inside the wrapper security boundary.
- Refresh PWA and notification assets and fix narrow-screen sheets, process
  timelines, and persistent error presentation.

### Release and operations

- Align Python, Codex `clientInfo`, Web package metadata, and the public build
  manifest on product version `3.0.0`.
- Upgrade the strict wire gate to protocol v19.
- Validate product and protocol versions together before staging or activating
  a release.
- Use immutable VPS releases, release-local virtual environments, atomic
  activation, readiness checks, and rollback.

### Upgrade notes

- v3.0.0 uses wire protocol v19. Wrapper, relay, and Web must be upgraded
  together; mixed protocol versions are rejected.
- Hard-refresh already-open browser tabs after deployment so they load the v3
  hashed assets and rebuild their local projection against protocol v19.
- Keep runtime secrets and machine state outside release directories. Do not
  replace `.env`, `~/.cc-remote`, Claude transcripts, or Codex rollouts.
- Claude integration remains pinned to `claude-agent-sdk==0.2.119`.
- History browsing remains a local read: it does not resume Claude/Codex or
  create a model turn.

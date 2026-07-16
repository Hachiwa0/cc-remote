# deploy/

Reference files for the production deploy (public VPS relay + wrapper on your
machine). The **full step-by-step guide is in the main [README](../README.md#生产部署公网-vps-中继--你机器上的-wrapper)**
([English](../README_en.md#production-deploy-public-vps-relay--wrapper-on-your-machine)).

- `setup-vps.sh` — atomic VPS release installer. It validates a user-owned
  upload, copies it to a new root-owned
  `/opt/cc-remote/releases/release-*` directory, builds that release's own
  venv, validates the Python/web protocol pair, then switches the
  `/opt/cc-remote/current` symlink in one rename. The running tree is never
  overlaid with `rsync --delete`. If relay restart/readiness fails, `current`,
  Caddyfile, and the relay unit roll back together and the previous release is
  health-checked. The previous full code + web + venv directory is retained.
  Run `sudo bash ~/cc-remote-upload/deploy/setup-vps.sh your-domain.com \
  ~/cc-remote-upload`; the optional second argument defaults to the repository
  containing the invoked script. Shared secrets stay only in
  `/opt/cc-remote/.env`, whose `WEB_STATIC_DIR` must point to
  `/opt/cc-remote/current/web/dist`.
- `Caddyfile` — reverse proxy + auto Let's Encrypt TLS (`wss://domain/ws` →
  `127.0.0.1:8765`) plus an early 4 KiB login-body limit. Replace
  `cc-remote.example.com` with your domain.
- `cc-remote-relay.service` — systemd unit for the relay on the VPS.
- `cc-remote-wrapper.service` — systemd unit for the wrapper on your machine
  (edit `User` + paths first). It reads root-only
  `/etc/cc-remote/wrapper.env`, hides that file and any legacy repository
  `.env` from model descendants, and disables core dumps.
- `../scripts/claude-remote` — explicit same-user Claude launcher. It resolves
  the repository venv even when invoked through a symlink, auto-starts a local
  Unix-socket broker, and attaches the real official Claude TUI. It never
  replaces, aliases, or intercepts the official `claude` command.
- `env.relay.example` / `env.wrapper.example` — environment templates for each
  side. Install the wrapper template as root:root mode 0600 at the path above.

Protocol v15 is a coordinated upgrade: publish the Python package and freshly
built `web/dist` as one release, then restart relay and wrapper. The strict
protocol gate is intentional and mixed protocol versions will not communicate.
`setup-vps.sh` rejects a missing or mismatched web build manifest. Stop the
wrapper first; activate the v15 relay/web release; then start the v15 wrapper.

## Native terminal coordination

- **Claude Code:** run `claude` directly for the untouched official process;
  Remote treats direct CLI, Desktop, and Agent View ownership as read-only.
  Run the separately named `claude-remote` when terminal and Remote must both
  control the same official TUI. Migration is explicit: for a direct CLI it may
  gracefully terminate the exact same-user Claude process with SIGTERM, but it
  never kills the terminal shell, escalates to SIGKILL, or silently adopts a
  process.
- **Codex Code:** `CC_REMOTE_CODEX_DAEMON=auto` prefers Codex's official shared
  app-server daemon. Set it to `off` only to force the legacy private stdio path.
- **Work:** both engines stay on private per-process control planes regardless
  of the Code settings.

The Claude launcher auto-starts its broker on first `new`/`resume`, so no
separate system service is required. The wrapper service and launcher must run
as the same OS user and use the same absolute
`CC_REMOTE_CLAUDE_BROKER_SOCKET`. A typical setup is:

```bash
mkdir -p "$HOME/.local/bin"
ln -sfn /path/to/cc-remote/scripts/claude-remote "$HOME/.local/bin/claude-remote"
export CC_REMOTE_CLAUDE_BROKER_SOCKET="$HOME/.cc-remote/claude-broker.sock"
claude-remote
```

Put the same socket path in `/etc/cc-remote/wrapper.env`. Do not create a
`claude` alias.

## Security (short version)

The relay is exposed publicly; `LOGIN_PASSWORD` or `LOGIN_USERS_JSON`,
`SESSION_SECRET`, and `WRAPPER_TOKEN` or `WRAPPER_TOKENS_JSON` are the
authentication secrets. Claude defaults to
`bypassPermissions`; Codex inherits its local sandbox and defaults to approval
policy `never`. Treat every logged-in client as holding remote agent/shell
authority on the wrapper machine. Use strong secrets, keep relay `.env` out of
git, never store the production wrapper token in a model-readable repository
file, and always terminate TLS at Caddy.
See the [security section](../README.md#安全须知务必读) of the main README.

The relay itself limits unfinished login bodies to 32 concurrent reads and 10
seconds each. The managed Caddy global block additionally sets 10-second header,
15-second body, 30-second write, 2-minute idle, and 64 KiB header limits before
requests reach the relay. Other global options and sites are preserved. If a
shared Caddyfile already contains an unmanaged `servers` block, setup fails
closed and asks the administrator to reconcile it instead of silently creating
ambiguous global behavior.

# deploy/

Reference files for the production deploy (public VPS relay + wrapper on your
machine). The **full step-by-step guide is in the main [README](../README.md#生产部署公网-vps-中继--你机器上的-wrapper)**
([English](../README_en.md#production-deploy-public-vps-relay--wrapper-on-your-machine)).

- `setup-vps.sh` — one-shot VPS setup: installs Caddy + a `ccremote` systemd
  service. Run as `sudo bash deploy/setup-vps.sh your-domain.com` after you've
  rsynced the code + `web/dist` to `/opt/cc-remote` and filled `.env`.
- `Caddyfile` — reverse proxy + auto Let's Encrypt TLS (`wss://domain/ws` →
  `127.0.0.1:8765`). Replace `cc-remote.example.com` with your domain.
- `cc-remote-relay.service` — systemd unit for the relay on the VPS.
- `cc-remote-wrapper.service` — systemd unit for the wrapper on your machine
  (edit `User` + paths first).
- `env.relay.example` / `env.wrapper.example` — `.env` templates for each side.

## Security (short version)

The relay is exposed publicly; `LOGIN_PASSWORD` + `WRAPPER_TOKEN` are the only
gate, and a logged-in client can run **arbitrary commands** on the wrapper
machine (`bypassPermissions`). Use strong secrets, keep `.env` out of git (it's
gitignored), always terminate TLS at Caddy. See the [security section](../README.md#安全须知务必读)
of the main README.

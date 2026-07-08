#!/usr/bin/env bash
# vps setup (Ubuntu/Debian). Run as root/sudo:
#   sudo bash deploy/setup-vps.sh your-domain.com
#
# Assumes /opt/cc-remote already contains: the code, web/dist (from
# `npm --prefix web run build`), requirements.txt, and a filled-in .env (from
# deploy/env.relay.example — LOGIN_PASSWORD, SESSION_SECRET, WRAPPER_TOKEN).
set -euo pipefail

DOMAIN="${1:?usage: sudo bash setup-vps.sh your-domain.com}"
APPDIR=/opt/cc-remote

[ -f "$APPDIR/.env" ] || { echo "ERROR: $APPDIR/.env missing (copy deploy/env.relay.example, fill tokens)"; exit 1; }
[ -d "$APPDIR/web/dist" ] || { echo "ERROR: $APPDIR/web/dist missing (run 'npm --prefix web run build' on your dev machine, then rsync web/dist here)"; exit 1; }
[ -f "$APPDIR/requirements.txt" ] || { echo "ERROR: $APPDIR/requirements.txt missing"; exit 1; }

echo "==> installing system deps (python3-venv) + Caddy (official repo)"
apt-get update -y
apt-get install -y python3-venv debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy

echo "==> creating service user"
id -u ccremote >/dev/null 2>&1 || useradd --system --create-home ccremote
chown -R ccremote:ccremote "$APPDIR"

echo "==> python venv + deps"
sudo -u ccremote python3 -m venv "$APPDIR/.venv"
sudo -u ccremote "$APPDIR/.venv/bin/pip" install --upgrade pip
sudo -u ccremote "$APPDIR/.venv/bin/pip" install -r "$APPDIR/requirements.txt"

echo "==> Caddy config (domain: $DOMAIN)"
sed "s/cc-remote.example.com/$DOMAIN/" "$APPDIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl restart caddy

echo "==> relay systemd service"
cp "$APPDIR/deploy/cc-remote-relay.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cc-remote-relay

echo
echo "Done. Check:"
echo "  https://$DOMAIN/healthz   (should show {\"ok\":true,...})"
echo "  https://$DOMAIN/          (web client)"
echo "  journalctl -u cc-remote-relay -f"
echo "  journalctl -u caddy -f"

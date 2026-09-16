#!/usr/bin/env bash
# Installs the official frpc binary + this sync service on a Raspberry Pi
# (Raspberry Pi OS / any systemd-based Debian derivative). Run as root:
#   sudo ./install.sh
set -euo pipefail

FRP_VERSION="0.70.0"   # pinned to match the frps server / mobile app's frp version — see README.md
INSTALL_DIR="/opt/obex-frpc"
CONFIG_DIR="/etc/obex-frpc"
BIN_PATH="/usr/local/bin/frpc"
SERVICE_USER="obex-frpc"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo ./install.sh)" >&2
  exit 1
fi

case "$(uname -m)" in
  aarch64) FRP_ARCH="arm64" ;;
  armv7l|armv6l) FRP_ARCH="arm" ;;
  x86_64) FRP_ARCH="amd64" ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

echo "==> Installing frpc v${FRP_VERSION} (linux_${FRP_ARCH})"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
ASSET="frp_${FRP_VERSION}_linux_${FRP_ARCH}"
curl -fsSL "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${ASSET}.tar.gz" \
  -o "$TMP_DIR/frp.tar.gz"
tar -xzf "$TMP_DIR/frp.tar.gz" -C "$TMP_DIR"
install -m 755 "$TMP_DIR/${ASSET}/frpc" "$BIN_PATH"

echo "==> Creating service user/directories"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"

echo "==> Installing sync script"
install -m 755 "$(dirname "$0")/obex_frpc_sync.py" "$INSTALL_DIR/obex_frpc_sync.py"

if [ ! -f "$CONFIG_DIR/config.env" ]; then
  echo "==> Installing config.env template (fill this in before starting!)"
  install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$0")/config.env.example" "$CONFIG_DIR/config.env"
else
  echo "==> $CONFIG_DIR/config.env already exists, leaving it as-is"
fi

echo "==> Installing systemd units"
install -m 644 "$(dirname "$0")/systemd/obex-frpc.service" /etc/systemd/system/obex-frpc.service
install -m 644 "$(dirname "$0")/systemd/obex-frpc-sync.service" /etc/systemd/system/obex-frpc-sync.service
install -m 644 "$(dirname "$0")/systemd/obex-frpc-sync.timer" /etc/systemd/system/obex-frpc-sync.timer
systemctl daemon-reload

cat <<EOF

Done. Next steps:
  1. Edit $CONFIG_DIR/config.env with your backend URL, device token, and camera IDs.
  2. Run once by hand to generate the initial frpc.toml:
       sudo -u $SERVICE_USER env \$(cat $CONFIG_DIR/config.env | xargs) /usr/bin/python3 $INSTALL_DIR/obex_frpc_sync.py
  3. Enable + start both units:
       sudo systemctl enable --now obex-frpc.service
       sudo systemctl enable --now obex-frpc-sync.timer
  4. Check status/logs:
       systemctl status obex-frpc.service obex-frpc-sync.timer
       journalctl -u obex-frpc.service -u obex-frpc-sync.service -f
EOF

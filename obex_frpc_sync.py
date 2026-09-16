#!/usr/bin/env python3
"""
Obex edge device: frpc config sync.

Fetches this device's cameras' tunnel configs from the Obex backend, renders
them into a single frpc.toml (frp v0.70 schema — see NOTES.md), and reloads
the already-running frpc process (systemd unit: obex-frpc.service) via its
admin API when the config changes. Runs standalone with no third-party
dependencies (stdlib only) so it needs nothing beyond `python3` on a fresh
Raspberry Pi OS image.

Intended to run periodically via obex-frpc-sync.timer, not continuously.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("obex-frpc-sync")


class ConfigError(Exception):
    """Missing/invalid local configuration — not a transient failure, don't retry blindly."""


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"{name} is required (set it in /etc/obex-frpc/config.env)")
    return value or ""


def fetch_tunnel_config(backend_url: str, device_token: str, camera_id: str) -> dict:
    """
    Mirrors src/features/home-device/api.ts's fetchTunnelConfig() in the
    mobile app repo — same endpoint, same response shape:
    { server_addr, server_port, local_ip, local_port, remote_port, token }.

    NOTE: as of writing, GET /cameras/:id/tunnel-config authenticates the
    caller as a *user* (their access token) and checks camera ownership —
    there is no device/service-account auth yet. OBEX_DEVICE_TOKEN is a
    placeholder for whatever device-scoped credential the backend team lands
    on; confirm the real auth mechanism with them before relying on this in
    production. See README.md "Open questions for the backend team".
    """
    url = f"{backend_url.rstrip('/')}/cameras/{camera_id}/tunnel-config"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {device_token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def render_frpc_toml(tunnels: list[dict], admin_port: int) -> str:
    """
    frp v0.70's actual client config schema (TOML) — no [common] section,
    server/auth fields are top-level camelCase, proxy fields are
    localIP/localPort/remotePort. See native/frpc-android's toml builder and
    server/README.md's "Tunnel / frp architecture" section in the mobile app
    repo for the same schema, verified there against
    github.com/fatedier/frp/conf/frpc_full_example.toml.
    """
    if not tunnels:
        raise ConfigError("no cameras configured (OBEX_CAMERA_IDS is empty) — nothing to tunnel")

    first = tunnels[0]
    lines = [
        f'serverAddr = "{first["server_addr"]}"',
        f'serverPort = {first["server_port"]}',
        'auth.method = "token"',
        f'auth.token = "{first["token"]}"',
        "",
        # Bound to localhost only — lets this script call `frpc reload`
        # without opening the admin API to the LAN.
        'webServer.addr = "127.0.0.1"',
        f"webServer.port = {admin_port}",
        "",
    ]
    for t in tunnels:
        lines += [
            "[[proxies]]",
            f'name = "camera-{t["camera_id"]}"',
            'type = "tcp"',
            f'localIP = "{t["local_ip"]}"',
            f'localPort = {t["local_port"]}',
            f'remotePort = {t["remote_port"]}',
            "",
        ]
    return "\n".join(lines)


def reload_frpc(frpc_bin: str, config_path: Path) -> None:
    """Hot-reload proxies with no dropped connections. Falls back to letting
    systemd's own restart-on-failure pick it up if frpc isn't up yet."""
    result = subprocess.run(
        [frpc_bin, "reload", "-c", str(config_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        log.warning(
            "frpc reload failed (rc=%s): %s — if obex-frpc.service isn't running yet, "
            "it will pick up the new config on its own next start.",
            result.returncode,
            result.stderr.strip(),
        )
    else:
        log.info("frpc reloaded successfully")


def main() -> int:
    try:
        backend_url = env("OBEX_BACKEND_URL", required=True)
        device_token = env("OBEX_DEVICE_TOKEN", required=True)
        camera_ids = [c.strip() for c in env("OBEX_CAMERA_IDS", required=True).split(",") if c.strip()]
        config_path = Path(env("OBEX_FRPC_CONFIG_PATH", "/etc/obex-frpc/frpc.toml"))
        admin_port = int(env("OBEX_FRPC_ADMIN_PORT", "7400"))
        frpc_bin = env("OBEX_FRPC_BIN", "/usr/local/bin/frpc")
    except ConfigError as e:
        log.error(str(e))
        return 2

    tunnels = []
    for camera_id in camera_ids:
        try:
            cfg = fetch_tunnel_config(backend_url, device_token, camera_id)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            log.error("failed to fetch tunnel config for camera %s: %s", camera_id, e)
            return 1
        cfg["camera_id"] = camera_id
        tunnels.append(cfg)

    new_toml = render_frpc_toml(tunnels, admin_port)
    old_toml = config_path.read_text() if config_path.exists() else None

    if new_toml == old_toml:
        log.info("frpc.toml unchanged (%d camera(s)) — nothing to do", len(tunnels))
        return 0

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_toml)
    log.info("wrote %s (%d camera(s))", config_path, len(tunnels))
    reload_frpc(frpc_bin, config_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

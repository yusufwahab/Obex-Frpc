# Obex edge frpc service

Runs `frpc` (the frp reverse-proxy client) as a standalone, always-on systemd
service on the Raspberry Pi, so a phone no longer has to be the one relaying
camera streams. **This directory is meant to be relocated into the edge
device repo** once that repo exists — it lives here in the meantime because
that's the repo this work started from. It has no dependency on the mobile
app's code and doesn't import anything from `src/` or `modules/`.

## Why this exists

Previously, `frpc` ran embedded inside the mobile app itself (see
`modules/frpc-tunnel` and `native/frpc-android` in this repo) — whichever
phone the user tapped "Start Remote Streaming" on became the tunnel for as
long as the app stayed alive in the foreground/background. That's fragile
(kept alive via a wake lock + foreground service, one tunnel per phone at a
time) and means remote viewing only works while someone's phone is actively
relaying.

Moving `frpc` onto a Raspberry Pi that lives permanently on the camera's
network fixes both: the tunnel is always up regardless of any phone's state,
and (per the Sep 4 2026 architecture meeting) it runs as its own systemd
service, independent of whatever ML/inference process also runs on the Pi.

**The mobile app's on-device `frpc` module has deliberately been left in
place for now** — removing it is a separate step once the tunnel lifecycle
(always-on vs. per-session start/stop, and who owns triggering it) is
decided with the backend/edge teams. See "Open questions" below.

## What's here

| File | Purpose |
|---|---|
| `install.sh` | Downloads the official `frpc` v0.70.0 binary (matches the version pinned in `native/frpc-android/go.mod`), creates a dedicated `obex-frpc` system user, installs the systemd units. Run once per Pi. |
| `obex_frpc_sync.py` | Stdlib-only Python script: fetches this device's cameras' tunnel configs from the backend, renders `frpc.toml`, hot-reloads `frpc` if the config changed. No pip dependencies — needs nothing beyond `python3`, which ships on Raspberry Pi OS. |
| `config.env.example` | Template for `/etc/obex-frpc/config.env` — backend URL, device token, camera IDs. |
| `systemd/obex-frpc.service` | Runs `frpc` itself, `Restart=always`. |
| `systemd/obex-frpc-sync.service` + `.timer` | Runs the sync script every 60s to pick up new/changed cameras. |

## How it works

```
obex-frpc-sync.timer (every 60s)
  -> obex_frpc_sync.py
       -> GET {OBEX_BACKEND_URL}/cameras/{id}/tunnel-config   (per camera ID in OBEX_CAMERA_IDS)
       -> renders /etc/obex-frpc/frpc.toml (all cameras as one frpc process, multiple [[proxies]])
       -> if changed: `frpc reload -c frpc.toml`   (zero-downtime, via frpc's local admin API)

obex-frpc.service
  -> frpc -c /etc/obex-frpc/frpc.toml   (Restart=always; picks up frpc.toml on (re)start too)
```

One `frpc` process handles every camera this Pi is responsible for (multiple
`[[proxies]]` blocks in one config) rather than one process per camera —
simpler to supervise, and frp explicitly supports this.

The TOML schema matches what the mobile app already builds in
`src/features/home-device/toml.ts` and what `server/README.md`'s "Tunnel /
frp architecture" section documents: frp v0.70's client config has no
`[common]` section, server/auth fields are top-level camelCase
(`serverAddr`, `auth.token`), proxy fields are `localIP`/`localPort`/`remotePort`.

## Install

On the Raspberry Pi (Raspberry Pi OS, any systemd-based Debian derivative):

```bash
sudo ./install.sh
sudo nano /etc/obex-frpc/config.env   # fill in OBEX_BACKEND_URL, OBEX_DEVICE_TOKEN, OBEX_CAMERA_IDS
sudo systemctl enable --now obex-frpc-sync.service   # generates the first frpc.toml
sudo systemctl enable --now obex-frpc.service
sudo systemctl enable --now obex-frpc-sync.timer
```

Check it's working:

```bash
systemctl status obex-frpc.service obex-frpc-sync.timer
journalctl -u obex-frpc.service -u obex-frpc-sync.service -f
```

## Boot behavior

`enable` (not just `start`) is what makes these survive a reboot — it tells
systemd to launch the unit on every future boot, not just this session. Once
`obex-frpc.service` and `obex-frpc-sync.timer` are both enabled (the install
steps above already do this with `enable --now`), power-cycling the Pi is
enough on its own: `obex-frpc.service` starts automatically, `frpc.toml`
already exists on disk from the last sync, and the tunnel comes back with no
manual command and no login required. If it ever crashes mid-run,
`Restart=always` brings it back within 5 seconds, same guarantee.

The one thing that has to happen *before* any of this works the first time:
`frpc.toml` must exist and be valid, which is why the install steps run
`obex-frpc-sync.service` once (to generate it) before starting
`obex-frpc.service`.

## Manual testing / troubleshooting

Run frpc in the foreground, outside systemd, to see its logs directly and
confirm the tunnel actually connects before trusting the service:

```bash
sudo -u obex-frpc frpc -c /etc/obex-frpc/frpc.toml
```

Force a config re-sync + hot reload by hand, without waiting for the timer:

```bash
sudo systemctl start obex-frpc-sync.service
journalctl -u obex-frpc-sync.service -n 20
```

`frpc reload` (used internally by `obex_frpc_sync.py`, but runnable directly)
pushes new `[[proxies]]` into an *already-running* frpc process without
dropping existing tunnels — different from restarting the service, which
would briefly drop every camera's connection:

```bash
frpc reload -c /etc/obex-frpc/frpc.toml
```

## Firewall / networking requirements

Same as documented in `server/README.md`: the `frps` server needs its
control port (`FRP_SERVER_PORT`, default `7000`) reachable from this Pi, and
each camera's allocated `remotePort` (range `20000`–`59999`) reachable from
wherever viewers connect from.

## Moving this into its own repo

This folder has no dependency on the rest of Obex-Revamp, so splitting it out
is a plain copy — there's no shared history worth preserving with
`git subtree`/`git filter-repo`:

```bash
cp -r edge-frpc-service ../obex-edge-frpc
cd ../obex-edge-frpc
git init
git add .
git commit -m "Initial obex-frpc edge service"
git branch -M main
git remote add origin <new-repo-url>
git push -u origin main
```

Hand the resulting repo's clone URL to whoever owns the edge device codebase.

## Open questions for the backend team

These are placeholders in this scaffold, not implemented decisions — flag
before relying on this beyond a dev box:

1. **Device auth.** `GET /cameras/:id/tunnel-config` currently only accepts
   a *user's* access token and checks camera ownership
   (`server/src/tunnels/tunnels.service.ts`). There's no device/service
   account credential yet. `OBEX_DEVICE_TOKEN` is a stand-in for whatever
   that ends up being (a long-lived device token minted during Pi
   provisioning/pairing is the most likely shape).
2. **Camera discovery.** There's no "list cameras assigned to this device"
   endpoint, so `OBEX_CAMERA_IDS` is a manually maintained comma-separated
   list per Pi. Worth a real endpoint once device auth exists, so a Pi can
   self-discover its cameras instead of being hand-configured.
3. **Tunnel lifecycle.** This service treats the tunnel as always-on once a
   camera ID is listed. The current backend contract
   (`POST /cameras/:id/stream/start` / `/stream/stop`,
   `Tunnel.isActive`, `remoteStreamUrl` only non-null while "LIVE") assumes
   a per-session start/stop model built around the old phone-hosted flow.
   Whether that stays, or `remoteStreamUrl` becomes available whenever the
   Pi's tunnel is simply up, is a decision to make with both the backend and
   frontend before wiring this in for real — see the mobile app's
   `src/features/cameras/remote-streaming.ts` and
   `src/app/camera/[id].tsx` for the current viewer-side logic this would
   affect.

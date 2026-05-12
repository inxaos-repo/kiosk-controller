# kiosk-controller

> Ginaz-hosted HTTP daemon that lets Home Assistant drive kiosks (Chromium DevTools via SSH tunnel) — without exposing DevTools to the LAN.

## Why

Chromium 122+ binds `--remote-debugging-port` to `127.0.0.1` only, even with `--remote-debugging-address=0.0.0.0`. This means HA automations can't reach DevTools directly. `kiosk-controller` bridges the gap: it runs on `ginaz` (192.168.2.20), SSH-tunnels into each kiosk, and proxies DevTools WebSocket commands on behalf of HA.

See: [homelab-infra#163](https://github.com/inxaos-repo/homelab-infra/issues/163)

## Architecture

```
Home Assistant pod
    │  POST /kiosk-reload/wallach
    │  Authorization: Bearer <token>
    ▼
kiosk-controller (ginaz:18080)
    │  SSH tunnel to kiosk IP (password auth)
    │  curl http://127.0.0.1:9222/json/list  → WS debugger URL
    │  WebSocket → Page.reload / Page.navigate
    ▼
Wallach kiosk (192.168.2.114)
    └─ Chromium DevTools 127.0.0.1:9222
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Daemon health (no auth) |
| POST | `/kiosk-reload/<name>` | Reload current page (preserve URL) |
| POST | `/kiosk-reset/<name>` | Navigate to the kiosk's canonical `kiosk_url` |
| POST | `/kiosk-show/<name>?url=<url>` | Navigate to an arbitrary URL (admin) |
| POST | `/kiosk-show-alias/<name>/<alias>` | Navigate to a named dashboard alias |
| POST | `/kiosk-sleep/<name>` | Turn screen **off** via `wlopm --off '*'` |
| POST | `/kiosk-wake/<name>` | Turn screen **on** via `wlopm --on '*'` |
| GET | `/kiosk-status/<name>` | Current tab URL + title |
| GET | `/metrics` | Prometheus counters |

All endpoints except `/healthz` require `Authorization: Bearer <token>`.

### Sleep + wake (HA-driven)

The `/kiosk-sleep` + `/kiosk-wake` pair implements real screen-off / screen-on via `wlopm` (the [wlroots-output-power-management-v1](https://wayland.app/protocols/wlr-output-power-management-unstable-v1) CLI). HA decides when:

1. **Sleep:** HA's `wallach_kiosk_sleep_on_loft_idle_*` automation fires when the loft motion sensor has been off for ≥ 15 min → `POST /kiosk-sleep/<name>` → the daemon SSHes in and runs `wlopm --off '*'` → HDMI output goes to standby → monitor sleeps.
2. **Wake:** HA's `wallach_kiosk_motion_wake_*` automation fires on any loft motion → `POST /kiosk-wake/<name>` → the daemon SSHes in and runs `wlopm --on '*'` → monitor wakes back up.
3. **Dashboard state preserved.** Wake does NOT change the URL — whatever was on screen at sleep time is what shows on wake. If you want "go home," call `/kiosk-reset/<name>` explicitly.

The daemon does not track "is the kiosk awake right now" — that's HA's job. The daemon just exposes the two verbs.

**Requires `wlopm` installed on the kiosk** — handled by the `kiosk-pi` Ansible role in [inxaos-repo/homelab-infra](https://github.com/inxaos-repo/homelab-infra). The `wlopm` package ships in Debian Trixie main for arm64 (v0.1.0).

### Show-Me Dashboard X (alias navigation)

The `/kiosk-show-alias` route lets HA / Bob / future voice intents navigate the kiosk to a named dashboard from a per-kiosk allowlist. Aliases are defined in `kiosks.yaml`. See the [`kiosks.yaml.example`](./kiosks.yaml.example) for shape.

A chosen alias **persists indefinitely** until something explicit changes it (`/kiosk-reset` for canonical home, or another `/kiosk-show-alias` for a different dashboard). Sleep + wake do not change the URL. `/kiosk-reset` is now never auto-fired — it's an explicit "go home" verb.

## Configuration

### `/etc/kiosk-controller/kiosks.yaml`

```yaml
kiosks:
  wallach:
    ip: 192.168.3.91
    ssh_user: damon
    ssh_password_env: WALLACH_SSH_PASSWORD   # name of env var holding the password
    devtools_port: 9222

    # NEW (2026-05): canonical URL for /kiosk-reset (the "home" state).
    # Optional; if absent, /kiosk-reset returns 400.
    kiosk_url: "https://loft-dashboard.k8s.inxaos.com"

    # NEW (2026-05): allowlisted dashboard aliases for /kiosk-show-alias.
    # Optional; if absent, /kiosk-show-alias returns 400.
    dashboards:
      home: "https://loft-dashboard.k8s.inxaos.com"
      wallach: "https://grafana.k8s.inxaos.com/d/wallach-kiosk?kiosk&refresh=30s"
      ginaz: "https://grafana.k8s.inxaos.com/d/ginaz-bare-metal?kiosk&refresh=30s"
```

Both `kiosk_url:` and `dashboards:` are optional and **backward-compatible** — existing kiosks without them keep working; only the new routes return 400 for those kiosks.

See `kiosks.yaml.example` for a full annotated example.

### `/etc/kiosk-controller/env`

```env
KIOSK_CONTROLLER_TOKEN=<bearer-token>
WALLACH_SSH_PASSWORD=<ssh-password>
```

The env file is loaded by systemd (`EnvironmentFile=`) and read by the daemon at request time. Passwords are never stored in the YAML config — only the env var name is there.

## Running

### Docker (production, via Ansible)

The Ansible role `kiosk-controller` in [homelab-infra](https://github.com/inxaos-repo/homelab-infra) deploys this as a systemd-runs-docker unit on ginaz.

### Local dev

```bash
pip install -r requirements.txt
export KIOSK_CONTROLLER_TOKEN=dev
export KIOSK_CONFIG_PATH=kiosks.yaml.example
export WALLACH_SSH_PASSWORD=yourpassword
python -m kiosk_controller.main
```

```bash
# Health
curl http://localhost:18080/healthz

# Reload (requires running kiosk with DevTools accessible)
curl -X POST -H "Authorization: Bearer dev" http://localhost:18080/kiosk-reload/wallach
```

## Home Assistant wiring

```yaml
# configuration.yaml (or rest_command.yaml)
rest_command:
  wallach_kiosk_reload:
    url: http://192.168.2.20:18080/kiosk-reload/wallach
    method: POST
    headers:
      Authorization: !secret kiosk_controller_token

  wallach_kiosk_show:
    url: "http://192.168.2.20:18080/kiosk-show/wallach?url={{ url }}"
    method: POST
    headers:
      Authorization: !secret kiosk_controller_token
```

Add to `secrets.yaml`:
```yaml
kiosk_controller_token: <same token as KIOSK_CONTROLLER_TOKEN>
```

### Existing automation

The automation **"Wallach Kiosk - Wake on Loft Motion"** (created 2026-05-09) currently uses `shell_command.wallach_kiosk_reload`. After deploying this service, switch it to `rest_command.wallach_kiosk_reload` via the HA UI (Settings → Automations → find the automation → edit the action).

## Future work

- **Kitchen kiosk**: The kitchen display uses Fully Kiosk Browser which has its own LAN-accessible REST API — a different code path. See [homelab-infra#163](https://github.com/inxaos-repo/homelab-infra/issues/163).
- **Key-based SSH**: Currently only password auth is implemented. Add `ssh_key_path` support.
- **Screen power control**: `POST /kiosk-screen/<name>?state=on|off` via `vcgencmd`.
- **Multi-tab support**: Allow targeting a specific tab by URL pattern.

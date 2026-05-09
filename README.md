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
| POST | `/kiosk-reload/<name>` | Reload current page |
| POST | `/kiosk-show/<name>?url=<url>` | Navigate to URL |
| GET | `/kiosk-status/<name>` | Current tab URL + title |
| GET | `/metrics` | Prometheus counters |

All endpoints except `/healthz` require `Authorization: Bearer <token>`.

## Configuration

### `/etc/kiosk-controller/kiosks.yaml`

```yaml
kiosks:
  wallach:
    ip: 192.168.2.114        # or 192.168.3.31 (Wi-Fi)
    ssh_user: damon
    ssh_password_env: WALLACH_SSH_PASSWORD   # name of env var holding the password
    devtools_port: 9222
```

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

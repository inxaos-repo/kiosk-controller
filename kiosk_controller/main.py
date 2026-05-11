"""
kiosk_controller/main.py — HTTP daemon for HA-driven kiosk wake/reload.

Listens on 0.0.0.0:18080 by default.
Reads kiosk definitions from /etc/kiosk-controller/kiosks.yaml.

Endpoints:
  POST /kiosk-reload/<name>                  — reload current page (Ctrl+R via DevTools)
  POST /kiosk-reset/<name>                   — navigate to the kiosk's canonical kiosk_url
  POST /kiosk-show/<name>?url=<url>          — navigate to an arbitrary URL (admin)
  POST /kiosk-show-alias/<name>/<alias>      — navigate to a named dashboard alias
  GET  /kiosk-status/<name>                  — current tab URL + last-active timestamp
  GET  /healthz                              — daemon health

Authentication: Bearer token via KIOSK_CONTROLLER_TOKEN env var.

Per-request flow for /kiosk-reload/<name>:
  1. Look up kiosk config (IP, ssh_user, devtools_port)
  2. SSH to kiosk using asyncssh (password from env var)
  3. Via SSH: GET http://127.0.0.1:<devtools_port>/json/list  → WS debugger URL
  4. Open WebSocket, send Page.reload
  5. Close WS + SSH, return result

Dependencies: aiohttp, asyncssh, pyyaml
"""

import asyncio
import json
import logging
import os
import time
from typing import Any

import aiohttp
import asyncssh
import yaml
from aiohttp import web

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("kiosk-controller")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("KIOSK_CONFIG_PATH", "/etc/kiosk-controller/kiosks.yaml")
TOKEN = os.environ.get("KIOSK_CONTROLLER_TOKEN", "")
PORT = int(os.environ.get("KIOSK_CONTROLLER_PORT", "18080"))
HOST = os.environ.get("KIOSK_CONTROLLER_HOST", "0.0.0.0")


def load_kiosks() -> dict[str, Any]:
    """Load kiosks.yaml and return the kiosks dict. Re-read each request so
    the operator can update without restarting the daemon."""
    try:
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f)
        return data.get("kiosks", {}) if data else {}
    except FileNotFoundError:
        log.warning("Config file not found: %s", CONFIG_PATH)
        return {}
    except yaml.YAMLError as e:
        log.error("YAML parse error in %s: %s", CONFIG_PATH, e)
        return {}


def get_kiosk(name: str) -> dict[str, Any] | None:
    kiosks = load_kiosks()
    return kiosks.get(name)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in ("/healthz",):
        return await handler(request)
    if not TOKEN:
        log.warning("No KIOSK_CONTROLLER_TOKEN set — accepting all requests (insecure)")
        return await handler(request)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[len("Bearer "):] != TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


# ---------------------------------------------------------------------------
# SSH + DevTools helpers
# ---------------------------------------------------------------------------
async def _ssh_get(kiosk: dict[str, Any], path: str) -> bytes:
    """Run `curl -s http://127.0.0.1:<port><path>` over SSH and return output."""
    ip = kiosk["ip"]
    user = kiosk.get("ssh_user", "damon")
    port_devtools = kiosk.get("devtools_port", 9222)
    password = _resolve_password(kiosk)

    connect_kwargs: dict[str, Any] = {
        "host": ip,
        "username": user,
        "known_hosts": None,  # Skip host key check — homelab internal trust
    }
    if password:
        connect_kwargs["password"] = password
        connect_kwargs["preferred_auth"] = "password"

    async with asyncssh.connect(**connect_kwargs) as conn:
        result = await conn.run(
            f"curl -s --max-time 5 http://127.0.0.1:{port_devtools}{path}",
            check=False,
        )
        if result.exit_status != 0:
            raise RuntimeError(
                f"Remote curl failed (exit {result.exit_status}): {result.stderr}"
            )
        return result.stdout.encode() if isinstance(result.stdout, str) else result.stdout


async def _ssh_ws_send(kiosk: dict[str, Any], ws_url: str, message: dict) -> None:
    """Open a WebSocket (proxied through an SSH port-forward) and send one message.

    asyncssh supports local port forwarding; we use it to forward a random
    local port to 127.0.0.1:<devtools_port> on the kiosk, then connect to
    the forwarded port directly.
    """
    ip = kiosk["ip"]
    user = kiosk.get("ssh_user", "damon")
    port_devtools = kiosk.get("devtools_port", 9222)
    password = _resolve_password(kiosk)

    connect_kwargs: dict[str, Any] = {
        "host": ip,
        "username": user,
        "known_hosts": None,
    }
    if password:
        connect_kwargs["password"] = password
        connect_kwargs["preferred_auth"] = "password"

    # Extract the WS path from the full WS URL returned by DevTools.
    # ws_url looks like: ws://127.0.0.1:9222/devtools/page/<id>
    ws_path = ws_url.split(f"127.0.0.1:{port_devtools}", 1)[-1]

    async with asyncssh.connect(**connect_kwargs) as conn:
        async with conn.forward_local_port("127.0.0.1", 0, "127.0.0.1", port_devtools) as fwd:
            local_port = fwd.get_port()
            local_ws_url = f"ws://127.0.0.1:{local_port}{ws_path}"
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(local_ws_url) as ws:
                    await ws.send_str(json.dumps(message))
                    # Wait briefly for a response (DevTools usually sends one)
                    try:
                        await asyncio.wait_for(ws.receive(), timeout=3.0)
                    except asyncio.TimeoutError:
                        pass  # Timeout is fine — command was sent


def _resolve_password(kiosk: dict[str, Any]) -> str | None:
    """Resolve SSH password from env var reference in kiosk config."""
    env_var = kiosk.get("ssh_password_env")
    if env_var:
        return os.environ.get(env_var, "")
    # Fallback: direct password field (not recommended)
    return kiosk.get("ssh_password")


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------
async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "status": "live", "ts": int(time.time())})


async def handle_reload(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    kiosk = get_kiosk(name)
    if not kiosk:
        return web.json_response({"error": f"unknown kiosk: {name}"}, status=404)

    log.info("reload request for kiosk=%s ip=%s", name, kiosk.get("ip"))
    try:
        tabs_raw = await _ssh_get(kiosk, "/json/list")
        tabs = json.loads(tabs_raw)
        if not tabs:
            return web.json_response(
                {"error": "no tabs found via DevTools"}, status=503
            )
        # Pick the first page-type tab (skip devtools protocol pages)
        tab = next(
            (t for t in tabs if t.get("type") == "page"),
            tabs[0],
        )
        ws_url = tab.get("webSocketDebuggerUrl", "")
        tab_url = tab.get("url", "unknown")

        if not ws_url:
            return web.json_response(
                {"error": "tab has no webSocketDebuggerUrl (already attached?)"}, status=503
            )

        await _ssh_ws_send(
            kiosk,
            ws_url,
            {"id": 1, "method": "Page.reload", "params": {"ignoreCache": True}},
        )
        log.info("reloaded kiosk=%s tab_url=%s", name, tab_url)
        return web.json_response({"reloaded": True, "tab_url": tab_url, "kiosk": name})

    except asyncssh.DisconnectError as e:
        log.error("SSH disconnect for kiosk=%s: %s", name, e)
        return web.json_response({"error": f"SSH error: {e}"}, status=503)
    except (ConnectionRefusedError, OSError) as e:
        log.error("SSH connection failed for kiosk=%s: %s", name, e)
        return web.json_response({"error": f"SSH connection failed: {e}"}, status=503)
    except Exception as e:  # pylint: disable=broad-except
        log.exception("Unexpected error for kiosk=%s", name)
        return web.json_response({"error": str(e)}, status=500)


def resolve_alias(kiosk: dict[str, Any], alias: str) -> tuple[str | None, list[str]]:
    """Look up an alias in the kiosk's dashboards map.

    Returns (url, available_aliases). url is None if the alias is missing.
    available_aliases is always populated (may be empty list).

    Pure function — no I/O, easy to unit test.
    """
    dashboards = kiosk.get("dashboards") or {}
    available = sorted(dashboards.keys())
    return dashboards.get(alias), available


async def _navigate_kiosk(kiosk: dict[str, Any], name: str, target_url: str) -> web.Response:
    """Shared navigation plumbing: DevTools list -> WS -> Page.navigate.

    Used by /kiosk-show, /kiosk-reset, and /kiosk-show-alias to avoid duplication.
    Returns the final JSON response (success or error). Callers add their own
    response fields by wrapping this in their handler.
    """
    try:
        tabs_raw = await _ssh_get(kiosk, "/json/list")
        tabs = json.loads(tabs_raw)
        if not tabs:
            return web.json_response({"error": "no tabs found via DevTools"}, status=503)

        tab = next((t for t in tabs if t.get("type") == "page"), tabs[0])
        ws_url = tab.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return web.json_response(
                {"error": "tab has no webSocketDebuggerUrl"}, status=503
            )

        await _ssh_ws_send(
            kiosk,
            ws_url,
            {"id": 1, "method": "Page.navigate", "params": {"url": target_url}},
        )
        log.info("navigated kiosk=%s to %s", name, target_url)
        return web.json_response({"navigated": True, "url": target_url, "kiosk": name})

    except asyncssh.DisconnectError as e:
        log.error("SSH disconnect for kiosk=%s: %s", name, e)
        return web.json_response({"error": f"SSH error: {e}"}, status=503)
    except (ConnectionRefusedError, OSError) as e:
        log.error("SSH connection failed for kiosk=%s: %s", name, e)
        return web.json_response({"error": f"SSH connection failed: {e}"}, status=503)
    except Exception as e:  # pylint: disable=broad-except
        log.exception("Unexpected error for kiosk=%s", name)
        return web.json_response({"error": str(e)}, status=500)


async def handle_show(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    target_url = request.rel_url.query.get("url", "")
    if not target_url:
        return web.json_response({"error": "?url= parameter required"}, status=400)

    kiosk = get_kiosk(name)
    if not kiosk:
        return web.json_response({"error": f"unknown kiosk: {name}"}, status=404)

    log.info("show request for kiosk=%s url=%s", name, target_url)
    return await _navigate_kiosk(kiosk, name, target_url)


async def handle_reset(request: web.Request) -> web.Response:
    """Navigate to the kiosk's canonical kiosk_url (the 'home' state).

    Intended to be called by HA's motion-wake automation after a sleep
    period — supports the 'sticky-until-sleep' UX where a user-chosen
    dashboard persists until the kiosk goes idle and wakes.
    """
    name = request.match_info["name"]
    kiosk = get_kiosk(name)
    if not kiosk:
        return web.json_response({"error": f"unknown kiosk: {name}"}, status=404)

    target_url = kiosk.get("kiosk_url")
    if not target_url:
        return web.json_response(
            {"error": f"kiosk has no kiosk_url configured", "kiosk": name},
            status=400,
        )

    log.info("reset request for kiosk=%s canonical_url=%s", name, target_url)
    resp = await _navigate_kiosk(kiosk, name, target_url)
    # If the navigation succeeded, augment the response with reset=True
    if resp.status == 200:
        body = json.loads(resp.body)
        body["reset"] = True
        return web.json_response(body)
    return resp


async def handle_show_alias(request: web.Request) -> web.Response:
    """Navigate to a named dashboard alias.

    Aliases are defined per-kiosk in the dashboards: block of kiosks.yaml.
    Returns 404 (with the list of available aliases) if the alias is missing,
    400 if the kiosk has no dashboards: block at all.
    """
    name = request.match_info["name"]
    alias = request.match_info["alias"]

    kiosk = get_kiosk(name)
    if not kiosk:
        return web.json_response({"error": f"unknown kiosk: {name}"}, status=404)

    dashboards = kiosk.get("dashboards")
    if not dashboards:
        return web.json_response(
            {"error": "kiosk has no dashboards configured", "kiosk": name},
            status=400,
        )

    target_url, available = resolve_alias(kiosk, alias)
    if target_url is None:
        return web.json_response(
            {
                "error": "unknown alias",
                "alias": alias,
                "kiosk": name,
                "available": available,
            },
            status=404,
        )

    log.info("show-alias request for kiosk=%s alias=%s url=%s", name, alias, target_url)
    resp = await _navigate_kiosk(kiosk, name, target_url)
    # If the navigation succeeded, augment the response with the alias name
    if resp.status == 200:
        body = json.loads(resp.body)
        body["alias"] = alias
        return web.json_response(body)
    return resp


async def handle_status(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    kiosk = get_kiosk(name)
    if not kiosk:
        return web.json_response({"error": f"unknown kiosk: {name}"}, status=404)

    try:
        tabs_raw = await _ssh_get(kiosk, "/json/list")
        tabs = json.loads(tabs_raw)
        tab = next((t for t in tabs if t.get("type") == "page"), tabs[0] if tabs else {})
        return web.json_response(
            {
                "kiosk": name,
                "ip": kiosk.get("ip"),
                "tab_url": tab.get("url", "unknown"),
                "tab_title": tab.get("title", ""),
                "tab_id": tab.get("id", ""),
                "ts": int(time.time()),
            }
        )
    except asyncssh.DisconnectError as e:
        return web.json_response({"error": f"SSH error: {e}", "kiosk": name}, status=503)
    except (ConnectionRefusedError, OSError) as e:
        return web.json_response(
            {"error": f"SSH connection failed: {e}", "kiosk": name}, status=503
        )
    except Exception as e:  # pylint: disable=broad-except
        log.exception("Status check failed for kiosk=%s", name)
        return web.json_response({"error": str(e), "kiosk": name}, status=500)


# ---------------------------------------------------------------------------
# Prometheus metrics (minimal)
# ---------------------------------------------------------------------------
_request_counts: dict[str, int] = {}


async def handle_metrics(request: web.Request) -> web.Response:
    lines = ["# HELP kiosk_controller_requests_total Total requests by endpoint"]
    lines.append("# TYPE kiosk_controller_requests_total counter")
    for key, count in _request_counts.items():
        lines.append(f'kiosk_controller_requests_total{{endpoint="{key}"}} {count}')
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_post("/kiosk-reload/{name}", handle_reload)
    app.router.add_post("/kiosk-reset/{name}", handle_reset)
    app.router.add_post("/kiosk-show/{name}", handle_show)
    app.router.add_post("/kiosk-show-alias/{name}/{alias}", handle_show_alias)
    app.router.add_get("/kiosk-status/{name}", handle_status)
    app.router.add_get("/metrics", handle_metrics)
    return app


if __name__ == "__main__":
    app = create_app()
    log.info("kiosk-controller starting on %s:%d", HOST, PORT)
    log.info("Config: %s", CONFIG_PATH)
    web.run_app(app, host=HOST, port=PORT, access_log=log)

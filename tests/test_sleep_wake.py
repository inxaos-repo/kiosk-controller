"""Tests for /kiosk-sleep and /kiosk-wake routes (wlopm-based screen on/off)."""

import pytest


# -----------------------------------------------------------------------------
# /kiosk-sleep
# -----------------------------------------------------------------------------
async def test_sleep_success(client):
    resp = await client.post(
        "/kiosk-sleep/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["slept"] is True
    assert body["kiosk"] == "wallach"


async def test_sleep_unknown_kiosk(client):
    resp = await client.post(
        "/kiosk-sleep/nonexistent",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 404
    body = await resp.json()
    assert "unknown kiosk" in body["error"]


async def test_sleep_requires_auth(client):
    resp = await client.post("/kiosk-sleep/wallach")
    assert resp.status == 401


async def test_sleep_wlr_randr_failure_returns_503(client, monkeypatch):
    """When wlr-randr exits non-zero, the route returns 503 with stderr."""
    from kiosk_controller import main as kc

    async def fake_ssh_exec_fail(kiosk, command):
        return (1, "", "wlr-randr: failed to connect to compositor")

    monkeypatch.setattr(kc, "_ssh_exec", fake_ssh_exec_fail)

    resp = await client.post(
        "/kiosk-sleep/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 503
    body = await resp.json()
    assert "wlr-randr --off failed" in body["error"]
    assert "compositor" in body["stderr"]
    assert body["kiosk"] == "wallach"


# -----------------------------------------------------------------------------
# /kiosk-wake
# -----------------------------------------------------------------------------
async def test_wake_success(client):
    resp = await client.post(
        "/kiosk-wake/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["woken"] is True
    assert body["kiosk"] == "wallach"


async def test_wake_unknown_kiosk(client):
    resp = await client.post(
        "/kiosk-wake/nonexistent",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 404
    body = await resp.json()
    assert "unknown kiosk" in body["error"]


async def test_wake_requires_auth(client):
    resp = await client.post("/kiosk-wake/wallach")
    assert resp.status == 401


async def test_wake_wlr_randr_failure_returns_503(client, monkeypatch):
    from kiosk_controller import main as kc

    async def fake_ssh_exec_fail(kiosk, command):
        return (1, "", "wlr-randr: no outputs found")

    monkeypatch.setattr(kc, "_ssh_exec", fake_ssh_exec_fail)

    resp = await client.post(
        "/kiosk-wake/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 503
    body = await resp.json()
    assert "wlr-randr --on failed" in body["error"]
    assert "no outputs" in body["stderr"]


# -----------------------------------------------------------------------------
# Route registration
# -----------------------------------------------------------------------------
async def test_sleep_wake_routes_registered():
    """Both new routes appear in the app's router."""
    from kiosk_controller import main as kc

    app = kc.create_app()
    paths = {
        r.resource.canonical
        for r in app.router.routes()
        if hasattr(r.resource, "canonical")
    }
    routes_str = " ".join(paths)
    assert "/kiosk-sleep" in routes_str
    assert "/kiosk-wake" in routes_str
    # Sanity: existing routes still there too
    assert "/kiosk-reset" in routes_str
    assert "/kiosk-show-alias" in routes_str
    assert "/kiosk-reload" in routes_str


# -----------------------------------------------------------------------------
# Verify command shape passed to _ssh_exec (catches regressions if someone
# changes the wlopm invocation by mistake)
# -----------------------------------------------------------------------------
async def test_sleep_passes_wlr_randr_off_command(client, monkeypatch):
    from kiosk_controller import main as kc

    captured = {}

    async def fake_ssh_exec_capture(kiosk, command):
        captured["command"] = command
        captured["kiosk_ip"] = kiosk["ip"]
        return (0, "", "")

    monkeypatch.setattr(kc, "_ssh_exec", fake_ssh_exec_capture)

    resp = await client.post(
        "/kiosk-sleep/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 200
    assert "wlr-randr" in captured["command"]
    assert "--off" in captured["command"]
    # No wlopm-style '*' wildcard in wlr-randr commands.
    assert "wlopm" not in captured["command"]
    assert captured["kiosk_ip"] == "192.168.3.91"


async def test_wake_passes_wlr_randr_on_command(client, monkeypatch):
    from kiosk_controller import main as kc

    captured = {}

    async def fake_ssh_exec_capture(kiosk, command):
        captured["command"] = command
        return (0, "", "")

    monkeypatch.setattr(kc, "_ssh_exec", fake_ssh_exec_capture)

    resp = await client.post(
        "/kiosk-wake/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 200
    assert "wlr-randr" in captured["command"]
    assert "--on" in captured["command"]
    assert "wlopm" not in captured["command"]


def test_sleep_wake_command_constants_are_defined():
    """SLEEP_COMMAND / WAKE_COMMAND are module-level so HA / Ansible debugging
    can introspect what's running without parsing handler source."""
    from kiosk_controller import main as kc

    assert hasattr(kc, "SLEEP_COMMAND")
    assert hasattr(kc, "WAKE_COMMAND")
    assert "wlr-randr" in kc.SLEEP_COMMAND
    assert "--off" in kc.SLEEP_COMMAND
    assert "wlr-randr" in kc.WAKE_COMMAND
    assert "--on" in kc.WAKE_COMMAND

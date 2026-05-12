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


async def test_sleep_wlopm_failure_returns_503(client, monkeypatch):
    """When wlopm exits non-zero, the route returns 503 with stderr."""
    from kiosk_controller import main as kc

    async def fake_ssh_exec_fail(kiosk, command):
        return (1, "", "wlopm: failed to connect to compositor")

    monkeypatch.setattr(kc, "_ssh_exec", fake_ssh_exec_fail)

    resp = await client.post(
        "/kiosk-sleep/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 503
    body = await resp.json()
    assert "wlopm --off failed" in body["error"]
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


async def test_wake_wlopm_failure_returns_503(client, monkeypatch):
    from kiosk_controller import main as kc

    async def fake_ssh_exec_fail(kiosk, command):
        return (1, "", "wlopm: no outputs found")

    monkeypatch.setattr(kc, "_ssh_exec", fake_ssh_exec_fail)

    resp = await client.post(
        "/kiosk-wake/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 503
    body = await resp.json()
    assert "wlopm --on failed" in body["error"]
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
async def test_sleep_passes_wlopm_off_command(client, monkeypatch):
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
    assert "wlopm" in captured["command"]
    assert "--off" in captured["command"]
    assert "*" in captured["command"]
    assert captured["kiosk_ip"] == "192.168.3.91"


async def test_wake_passes_wlopm_on_command(client, monkeypatch):
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
    assert "wlopm" in captured["command"]
    assert "--on" in captured["command"]
    assert "*" in captured["command"]

"""Tests for the /kiosk-wake and /kiosk-sleep cooldown guard.

The kiosk-controller daemon serializes HA motion-wake automation traffic so
that a wedged automation (or a hyperactive PIR) can't DDoS the kiosk's sshd.
The guard is shared between /kiosk-wake and /kiosk-sleep because they touch
the same SSH path.
"""

import pytest


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_cooldown_state():
    """Ensure each test starts with a clean cooldown ledger."""
    from kiosk_controller import main as kc

    kc._reset_dpms_cooldown_state()
    yield
    kc._reset_dpms_cooldown_state()


def _auth():
    return {"Authorization": "Bearer test-token"}


# -----------------------------------------------------------------------------
# /kiosk-wake cooldown
# -----------------------------------------------------------------------------
async def test_wake_inside_cooldown_returns_429(client, monkeypatch):
    """Second wake within the cooldown window is throttled with 429."""
    from kiosk_controller import main as kc

    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 30.0)

    first = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert first.status == 200

    second = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert second.status == 429
    body = await second.json()
    assert body["throttled"] is True
    assert body["kiosk"] == "wallach"
    assert 0 < body["remaining_seconds"] <= 30
    assert body["cooldown_seconds"] == 30
    # Retry-After header should be a positive integer string
    retry_after = second.headers.get("Retry-After")
    assert retry_after is not None and int(retry_after) >= 1


async def test_wake_after_cooldown_succeeds(client, monkeypatch):
    """Once the cooldown elapses (simulated via monotonic patch), wake works."""
    from kiosk_controller import main as kc

    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 30.0)

    first = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert first.status == 200

    # Simulate 60s passing by rewinding the recorded attempt timestamp.
    assert "wallach" in kc._last_dpms_attempt
    kc._last_dpms_attempt["wallach"] -= 60.0

    second = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert second.status == 200
    body = await second.json()
    assert body["woken"] is True


async def test_wake_cooldown_disabled_when_zero(client, monkeypatch):
    """Setting the cooldown to 0 disables the guard globally."""
    from kiosk_controller import main as kc

    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 0.0)

    for _ in range(5):
        resp = await client.post("/kiosk-wake/wallach", headers=_auth())
        assert resp.status == 200


async def test_wake_per_kiosk_override(client, monkeypatch, tmp_path):
    """Per-kiosk `wake_cooldown_seconds` in kiosks.yaml overrides the env default."""
    from kiosk_controller import main as kc

    # Global default very low (1s) but per-kiosk override is huge (3600s)
    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 1.0)

    # Patch the loader so the wallach kiosk now has wake_cooldown_seconds=3600.
    original_get = kc.get_kiosk

    def patched_get(name):
        kiosk = original_get(name)
        if kiosk and name == "wallach":
            kiosk = {**kiosk, "wake_cooldown_seconds": 3600}
        return kiosk

    monkeypatch.setattr(kc, "get_kiosk", patched_get)

    first = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert first.status == 200

    # Even after 10s would clear the 1s global default, the per-kiosk override
    # still blocks. Simulate 10s passing.
    kc._last_dpms_attempt["wallach"] -= 10.0

    second = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert second.status == 429
    body = await second.json()
    assert body["cooldown_seconds"] == 3600


async def test_wake_failed_attempt_still_counts(client, monkeypatch):
    """A failed wake (SSH refused) STILL consumes the cooldown window.

    Critical for the runaway-loop scenario: if the kiosk is wedged and sshd
    is refusing connections, we don't want HA's motion-wake automation
    hammering the controller with retries every 30s.
    """
    from kiosk_controller import main as kc

    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 30.0)

    async def fake_ssh_refused(kiosk, command):
        raise ConnectionRefusedError("port 22 refused")

    monkeypatch.setattr(kc, "_ssh_exec", fake_ssh_refused)

    # First call: SSH fails -> 503, but cooldown is recorded
    first = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert first.status == 503

    # Second call: throttled because the failed attempt counted
    second = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert second.status == 429


# -----------------------------------------------------------------------------
# /kiosk-sleep cooldown (shares the same ledger as /kiosk-wake)
# -----------------------------------------------------------------------------
async def test_sleep_inside_cooldown_returns_429(client, monkeypatch):
    from kiosk_controller import main as kc

    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 30.0)

    first = await client.post("/kiosk-sleep/wallach", headers=_auth())
    assert first.status == 200

    second = await client.post("/kiosk-sleep/wallach", headers=_auth())
    assert second.status == 429


async def test_sleep_then_wake_share_cooldown(client, monkeypatch):
    """sleep and wake share the same cooldown ledger \u2014 a sleep blocks a wake.

    Rationale: both routes touch the same SSH path on the kiosk. Allowing a
    rapid sleep-wake-sleep-wake oscillation would defeat the protection.
    """
    from kiosk_controller import main as kc

    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 30.0)

    sleep_resp = await client.post("/kiosk-sleep/wallach", headers=_auth())
    assert sleep_resp.status == 200

    wake_resp = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert wake_resp.status == 429


# -----------------------------------------------------------------------------
# Per-kiosk-name isolation
# -----------------------------------------------------------------------------
async def test_cooldown_isolated_per_kiosk(client, monkeypatch):
    """A wake of kiosk A does not affect the cooldown of kiosk B."""
    from kiosk_controller import main as kc

    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 30.0)

    a = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert a.status == 200

    # `legacy` is a separate kiosk in the test fixture
    b = await client.post("/kiosk-wake/legacy", headers=_auth())
    assert b.status == 200

    # Both are now in cooldown for their own keys
    a2 = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert a2.status == 429
    b2 = await client.post("/kiosk-wake/legacy", headers=_auth())
    assert b2.status == 429


# -----------------------------------------------------------------------------
# Invalid override falls back to default
# -----------------------------------------------------------------------------
async def test_invalid_override_falls_back_to_default(client, monkeypatch):
    from kiosk_controller import main as kc

    monkeypatch.setattr(kc, "WAKE_COOLDOWN_SECONDS", 30.0)

    original_get = kc.get_kiosk

    def patched_get(name):
        kiosk = original_get(name)
        if kiosk and name == "wallach":
            kiosk = {**kiosk, "wake_cooldown_seconds": "not-a-number"}
        return kiosk

    monkeypatch.setattr(kc, "get_kiosk", patched_get)

    first = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert first.status == 200

    second = await client.post("/kiosk-wake/wallach", headers=_auth())
    assert second.status == 429
    body = await second.json()
    assert body["cooldown_seconds"] == 30  # fell back to global default

"""Tests for the alias-lookup logic + /kiosk-show-alias + /kiosk-reset routes."""

import pytest


# -----------------------------------------------------------------------------
# Pure-function tests for resolve_alias
# -----------------------------------------------------------------------------
def test_resolve_alias_hit(kiosks_yaml_path):
    from kiosk_controller import main as kc

    kiosk = kc.get_kiosk("wallach")
    url, available = kc.resolve_alias(kiosk, "ginaz")
    assert url == "https://grafana.k8s.inxaos.com/d/ginaz-bare-metal?kiosk&refresh=30s"
    assert "home" in available
    assert "wallach" in available
    assert "ginaz" in available
    assert available == sorted(available)


def test_resolve_alias_miss(kiosks_yaml_path):
    from kiosk_controller import main as kc

    kiosk = kc.get_kiosk("wallach")
    url, available = kc.resolve_alias(kiosk, "this-alias-does-not-exist")
    assert url is None
    assert "ginaz" in available  # still surfaces the catalog


def test_resolve_alias_no_dashboards_block(kiosks_yaml_path):
    from kiosk_controller import main as kc

    kiosk = kc.get_kiosk("legacy")
    url, available = kc.resolve_alias(kiosk, "home")
    assert url is None
    assert available == []


# -----------------------------------------------------------------------------
# HTTP tests for /kiosk-show-alias
# -----------------------------------------------------------------------------
async def test_show_alias_success(client):
    resp = await client.post(
        "/kiosk-show-alias/wallach/ginaz",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["navigated"] is True
    assert body["kiosk"] == "wallach"
    assert body["alias"] == "ginaz"
    assert "ginaz-bare-metal" in body["url"]


async def test_show_alias_unknown_kiosk(client):
    resp = await client.post(
        "/kiosk-show-alias/nonexistent/home",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 404
    body = await resp.json()
    assert "unknown kiosk" in body["error"]


async def test_show_alias_kiosk_without_dashboards(client):
    resp = await client.post(
        "/kiosk-show-alias/legacy/home",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "no dashboards configured" in body["error"]
    assert body["kiosk"] == "legacy"


async def test_show_alias_unknown_alias_returns_catalog(client):
    resp = await client.post(
        "/kiosk-show-alias/wallach/does-not-exist",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 404
    body = await resp.json()
    assert body["error"] == "unknown alias"
    assert body["alias"] == "does-not-exist"
    assert body["kiosk"] == "wallach"
    assert "ginaz" in body["available"]
    assert "home" in body["available"]


async def test_show_alias_requires_auth(client):
    resp = await client.post("/kiosk-show-alias/wallach/ginaz")
    assert resp.status == 401


# -----------------------------------------------------------------------------
# HTTP tests for /kiosk-reset
# -----------------------------------------------------------------------------
async def test_reset_success(client):
    resp = await client.post(
        "/kiosk-reset/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["navigated"] is True
    assert body["reset"] is True
    assert body["url"] == "https://loft-dashboard.k8s.inxaos.com"
    assert body["kiosk"] == "wallach"


async def test_reset_unknown_kiosk(client):
    resp = await client.post(
        "/kiosk-reset/nonexistent",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 404


async def test_reset_kiosk_without_canonical_url(client):
    resp = await client.post(
        "/kiosk-reset/legacy",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "no kiosk_url configured" in body["error"]
    assert body["kiosk"] == "legacy"


async def test_reset_requires_auth(client):
    resp = await client.post("/kiosk-reset/wallach")
    assert resp.status == 401


# -----------------------------------------------------------------------------
# Sanity: existing endpoints still work
# -----------------------------------------------------------------------------
async def test_healthz_no_auth(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True


async def test_show_arbitrary_url_still_works(client):
    resp = await client.post(
        "/kiosk-show/wallach?url=https%3A%2F%2Fexample.com",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["navigated"] is True
    assert body["url"] == "https://example.com"


async def test_show_requires_url_param(client):
    resp = await client.post(
        "/kiosk-show/wallach",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status == 400


async def test_routes_registered():
    """Quick check that the new routes are in the app's router table."""
    from kiosk_controller import main as kc

    app = kc.create_app()
    paths = {r.resource.canonical for r in app.router.routes() if hasattr(r.resource, "canonical")}
    # Note: aiohttp normalizes /{name}/{alias} differently; check substrings
    routes_str = " ".join(paths)
    assert "/kiosk-reset" in routes_str
    assert "/kiosk-show-alias" in routes_str
    assert "/kiosk-reload" in routes_str  # preserved
    assert "/kiosk-show" in routes_str    # preserved

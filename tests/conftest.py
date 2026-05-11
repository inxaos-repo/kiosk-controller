"""Shared pytest fixtures for kiosk-controller tests."""
import os
import textwrap

import pytest


@pytest.fixture
def kiosks_yaml_path(tmp_path, monkeypatch):
    """Write a kiosks.yaml fixture and point the daemon at it."""
    cfg = tmp_path / "kiosks.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            kiosks:
              wallach:
                ip: 192.168.3.91
                ssh_user: damon
                ssh_password_env: WALLACH_SSH_PASSWORD
                devtools_port: 9222
                kiosk_url: "https://loft-dashboard.k8s.inxaos.com"
                dashboards:
                  home: "https://loft-dashboard.k8s.inxaos.com"
                  wallach: "https://grafana.k8s.inxaos.com/d/wallach-kiosk?kiosk&refresh=30s"
                  ginaz: "https://grafana.k8s.inxaos.com/d/ginaz-bare-metal?kiosk&refresh=30s"
              # Legacy-shape kiosk (no kiosk_url, no dashboards) to test backward compat
              legacy:
                ip: 10.0.0.99
                ssh_user: pi
                ssh_password_env: LEGACY_SSH_PASSWORD
                devtools_port: 9222
            """
        ).strip()
    )
    monkeypatch.setenv("KIOSK_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("KIOSK_CONTROLLER_TOKEN", "test-token")
    return cfg


@pytest.fixture
def app(kiosks_yaml_path, monkeypatch):
    """Build the aiohttp app with a stubbed _navigate_kiosk so tests don't SSH."""
    # Re-import after env vars are set so module-level TOKEN/etc pick up the values.
    import importlib

    from kiosk_controller import main as kc_main

    importlib.reload(kc_main)

    # Stub _navigate_kiosk to avoid actual SSH/DevTools. We just echo the URL back.
    async def fake_navigate(kiosk, name, target_url):
        from aiohttp import web

        return web.json_response(
            {"navigated": True, "url": target_url, "kiosk": name}
        )

    monkeypatch.setattr(kc_main, "_navigate_kiosk", fake_navigate)

    return kc_main.create_app()


@pytest.fixture
async def client(app, aiohttp_client):
    return await aiohttp_client(app)

"""Setup dashboard behavior over the shared declarative registry."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from lup.devtools import setup
from lup.devtools.dashboard.app import DashboardState, create_dashboard
from lup_template.devtools.setup import INTEGRATIONS

BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture
def isolated_dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the dashboard against an isolated env file."""
    monkeypatch.setattr(setup, "ENV_LOCAL", tmp_path / ".env.local")


async def test_dashboard_serves_packaged_wizard(isolated_dashboard: None) -> None:
    del isolated_dashboard
    async with AsyncClient(
        transport=ASGITransport(app=create_dashboard(BASE_URL, INTEGRATIONS)),
        base_url=BASE_URL,
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Project setup" in response.text
    assert "Configured integrations" in response.text


async def test_dashboard_projects_cli_registry_status(
    isolated_dashboard: None,
) -> None:
    del isolated_dashboard
    async with AsyncClient(
        transport=ASGITransport(app=create_dashboard(BASE_URL, INTEGRATIONS)),
        base_url=BASE_URL,
    ) as client:
        response = await client.get("/api/setup")
    state = DashboardState.model_validate(response.json())

    assert response.status_code == 200
    assert state.total == len(INTEGRATIONS)
    assert {item.command for item in state.integrations} == {
        item.command for item in INTEGRATIONS
    }
    assert next(
        item for item in state.integrations if item.command == "google"
    ).mode == ("cli")


async def test_dashboard_writes_only_declared_integration_fields(
    isolated_dashboard: None,
) -> None:
    del isolated_dashboard
    async with AsyncClient(
        transport=ASGITransport(app=create_dashboard(BASE_URL, INTEGRATIONS)),
        base_url=BASE_URL,
    ) as client:
        response = await client.put(
            "/api/setup/api-key",
            json={"values": {"EXAMPLE_API_KEY": "configured-secret"}},
        )
    state = DashboardState.model_validate(response.json())

    assert response.status_code == 200
    assert setup.read_env_local() == {"EXAMPLE_API_KEY": "configured-secret"}
    assert next(
        item for item in state.integrations if item.command == "api-key"
    ).configured


async def test_dashboard_rejects_cross_integration_and_bespoke_writes(
    isolated_dashboard: None,
) -> None:
    del isolated_dashboard
    async with AsyncClient(
        transport=ASGITransport(app=create_dashboard(BASE_URL, INTEGRATIONS)),
        base_url=BASE_URL,
    ) as client:
        cross_integration = await client.put(
            "/api/setup/api-key",
            json={"values": {"NOTION_TOKEN": "wrong-surface"}},
        )
        bespoke = await client.put(
            "/api/setup/google",
            json={"values": {"GMAIL_CREDENTIALS_PATH": "credentials/google.json"}},
        )

    assert cross_integration.status_code == 400
    assert bespoke.status_code == 409
    assert setup.read_env_local() == {}


async def test_a_rebound_host_cannot_write_the_env_file(
    isolated_dashboard: None,
) -> None:
    """A loopback bind does not stop the browser being pointed here.

    DNS rebinding makes a page the user is merely visiting into this origin,
    so the same-origin policy permits it and CORS never runs. The Host header
    is the one thing that still names the attacker, and this surface writes
    the user's credentials — so it is the one that most needs to check.
    """
    del isolated_dashboard
    async with AsyncClient(
        transport=ASGITransport(app=create_dashboard(BASE_URL, INTEGRATIONS)),
        base_url="http://evil.example",
    ) as client:
        page = await client.get("/")
        write = await client.put(
            "/api/setup/api-key", json={"values": {"EXAMPLE_API_KEY": "stolen"}}
        )

    assert page.status_code == 421
    assert write.status_code == 421
    assert setup.read_env_local() == {}

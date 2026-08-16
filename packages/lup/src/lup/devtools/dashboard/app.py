"""Serve the reusable setup registry through a local web dashboard.

The CLI wizard and dashboard deliberately take the same ``Integration``
list and the same env-file helpers. A domain customizes setup once; both
interfaces then expose the same fields, status checks, and bespoke-flow
fallbacks.
"""

from functools import partial
from typing import Annotated, Literal

import typer
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from lup.devtools.setup import Integration, read_env_local, write_env_local
from lup.types import EnvVars
from lup.web.serve import local_page_app, serve_local_page

DASHBOARD_PORT = 8765
"""Where this page listens when nothing says otherwise.

A port is this library's judgement rather than anyone's convention, so it is
the default the ``--port`` flag replaces and the factory parameter an
application overrides — never a value an adopter has to fork to change.
"""


class DashboardField(BaseModel, frozen=True):
    """One declarative setup field safe to expose in the browser."""

    key: str
    prompt: str
    secret: bool


class DashboardIntegration(BaseModel, frozen=True):
    """Browser-facing projection of one setup integration."""

    name: str
    command: str
    help: str
    configured: bool
    detail: str
    mode: Literal["form", "cli"]
    fields: list[DashboardField]


class DashboardState(BaseModel, frozen=True):
    """Complete setup progress shown by the dashboard."""

    configured: int
    total: int
    integrations: list[DashboardIntegration]


class IntegrationUpdate(BaseModel):
    """Allowlisted env values submitted for one declarative integration."""

    values: EnvVars = Field(description="Environment values keyed by setup field")


def dashboard_integration(
    integration: Integration, env: EnvVars
) -> DashboardIntegration:
    """Project one integration into status plus browser-safe field metadata."""
    status = integration.check_status(env)
    fields = [
        DashboardField(key=field.key, prompt=field.prompt, secret=field.secret)
        for field in integration.fields
    ]
    mode: Literal["form", "cli"] = (
        "form" if fields and integration.setup_func is None else "cli"
    )
    return DashboardIntegration(
        name=integration.name,
        command=integration.command,
        help=integration.help,
        configured=status.ok,
        detail=status.detail,
        mode=mode,
        fields=fields,
    )


def dashboard_state(integrations: list[Integration]) -> DashboardState:
    """Read current setup progress from the shared integration registry."""
    env = read_env_local()
    projected = [
        dashboard_integration(integration, env) for integration in integrations
    ]
    return DashboardState(
        configured=sum(item.configured for item in projected),
        total=len(projected),
        integrations=projected,
    )


def find_integration(integrations: list[Integration], command: str) -> Integration:
    """Resolve a setup command or return a typed HTTP not-found response."""
    integration = next((item for item in integrations if item.command == command), None)
    if integration is None:
        raise HTTPException(status_code=404, detail="Unknown setup integration")
    return integration


# lup: ignore[model-free-function] — subject is the registry, not the payload
def save_integration(
    integrations: list[Integration], command: str, update: IntegrationUpdate
) -> DashboardState:
    """Persist only fields declared by one browser-configurable integration."""
    integration = find_integration(integrations, command)
    allowed = {field.key for field in integration.fields}
    if integration.setup_func is not None or not allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Run `uv run lup-devtools setup {command}` for this workflow",
        )
    unknown = set(update.values) - allowed  # lup: ignore[set-shape] — key diff
    if unknown:
        names = ", ".join(sorted(unknown))
        raise HTTPException(status_code=400, detail=f"Fields not allowed: {names}")
    write_env_local(update.values)
    return dashboard_state(integrations)


def create_dashboard(url: str, integrations: list[Integration]) -> FastAPI:
    """Build the local FastAPI app over the canonical setup registry.

    The page writes environment values, including the fields declared secret,
    so it keeps the supervisor's posture rather than a weaker one: what a
    surface is worth attacking is decided by what it writes, and this one
    writes the user's credentials.
    """
    dashboard = local_page_app("Lup setup", "lup.devtools.dashboard", url)

    @dashboard.get("/api/setup")
    async def setup_status() -> DashboardState:
        return dashboard_state(integrations)

    @dashboard.put("/api/setup/{command}")
    async def update_setup(command: str, update: IntegrationUpdate) -> DashboardState:
        return save_integration(integrations, command, update)

    return dashboard


def create_dashboard_app(
    integrations: list[Integration], default_port: int = DASHBOARD_PORT
) -> typer.Typer:
    """Build the dashboard command over a project's declared integrations."""
    app = typer.Typer(
        help="Host the local setup dashboard",
        invoke_without_command=True,
        no_args_is_help=False,
    )

    @app.callback(invoke_without_command=True)
    def serve_dashboard(
        context: typer.Context,
        host: Annotated[str, typer.Option(help="Interface to bind")] = "127.0.0.1",
        port: Annotated[int, typer.Option(help="TCP port to bind")] = default_port,
        open_page: Annotated[
            bool,
            typer.Option("--open/--no-open", help="Open the dashboard in a browser"),
        ] = True,
    ) -> None:
        """Run the setup dashboard from the same registry as the CLI wizard."""
        if context.invoked_subcommand is not None:
            return
        try:
            serve_local_page(
                partial(create_dashboard, integrations=integrations),
                "Lup setup dashboard",
                host,
                port,
                open_page,
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error

    return app

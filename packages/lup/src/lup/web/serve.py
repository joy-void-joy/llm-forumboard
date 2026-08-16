"""Standing a local-only page up, once, for every surface that has one.

:mod:`lup.web.loopback` already argued that binding loopback and checking the
``Host`` header belong together, and that a surface skipping the second had no
way of knowing it had. It then left the half that *invokes* them to each
application — so the supervisor and the setup dashboard each wrote their own
construct-guard-open-run sequence, and a third surface would have written a
third, with the same one line to forget.

What is shared is the whole sequence: refuse a non-loopback bind, build the
URL the guard will answer for, say where the page is, open it, serve it. What
differs is only the routes, which arrive as a factory taking the URL — it has
to, because the ``Host`` values a page answers for are derived from the port
it ends up on.
"""

import webbrowser
from collections.abc import Callable
from importlib import resources

import typer
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from lup.web.loopback import guard_loopback_host, refuse_non_loopback


def local_page_app(title: str, package: str, url: str) -> FastAPI:
    """A Host-guarded app serving one package's ``assets/index.html`` at ``/``.

    The docs routes are off because a local-only page has no audience for
    them and they widen what an attacker reaching this origin can enumerate.
    """
    application = FastAPI(title=title, docs_url=None, redoc_url=None)
    guard_loopback_host(application, url)
    html = resources.files(package).joinpath("assets/index.html").read_text("utf-8")

    @application.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return HTMLResponse(html)

    return application


def serve_local_page(
    build: Callable[[str], FastAPI],
    surface: str,
    host: str,
    port: int,
    open_page: bool = True,
) -> None:
    """Bind loopback and serve one page, announcing where it came up.

    ``surface`` names this page in the refusal a bad ``--host`` earns and in
    the line pointing a reader at it, so the message says which of several
    local surfaces is being talked about.

    Raises ``ValueError`` when ``host`` is not a loopback address — the
    caller is a CLI and turns that into whatever its own framework spells a
    bad parameter as.
    """
    refuse_non_loopback(host, surface)
    url = f"http://{host}:{port}"
    typer.echo(f"{surface}: {url}")
    if open_page:
        webbrowser.open(url)
    uvicorn.run(build(url), host=host, port=port)

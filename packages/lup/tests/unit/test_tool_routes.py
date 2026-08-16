"""Behavior tests for URL → tool routing.

Covers what a route matches, what it builds, the dispatch that calls through
to the tool, and the host matching that decides which redirection speaks for
a URL — including the lookalike host a substring check would have accepted.
"""

import json

from pydantic import BaseModel, Field

from lup.mcp import lup_tool, response_text
from lup.tool_routes import ToolRoutes


class PaperInput(BaseModel):
    paper_id: str = Field(description="Identifier of the paper to fetch")


class PaperOutput(BaseModel):
    paper_id: str


@lup_tool("Fetch a paper by identifier.", name="fetch_paper")
async def fetch_paper(inp: PaperInput) -> PaperOutput:
    return PaperOutput(paper_id=inp.paper_id)


def arxiv_routes() -> ToolRoutes:
    registry = ToolRoutes()
    registry.route(
        r"arxiv\.org/abs/(\d+\.\d+)",
        fetch_paper,
        lambda m: {"paper_id": m.group(1)},
    )
    return registry


def test_route_builds_arguments_from_the_match() -> None:
    entry = arxiv_routes().routes[0]
    assert entry.arguments("https://arxiv.org/abs/2301.12345") == {
        "paper_id": "2301.12345"
    }


def test_route_declines_a_url_it_does_not_match() -> None:
    entry = arxiv_routes().routes[0]
    assert entry.arguments("https://example.com/abs/2301.12345") is None


async def test_dispatch_calls_the_routed_tool() -> None:
    response = await arxiv_routes().dispatch("https://arxiv.org/abs/2301.12345")
    assert response is not None
    assert json.loads(response_text(response)) == {"paper_id": "2301.12345"}


async def test_dispatch_falls_through_on_an_unrouted_url() -> None:
    assert await arxiv_routes().dispatch("https://example.com/page") is None


async def test_first_registration_wins() -> None:
    registry = arxiv_routes()
    registry.route(
        r"arxiv\.org/abs/(\d+)",
        fetch_paper,
        lambda m: {"paper_id": f"second-{m.group(1)}"},
    )
    response = await registry.dispatch("https://arxiv.org/abs/2301.12345")
    assert response is not None
    assert json.loads(response_text(response)) == {"paper_id": "2301.12345"}


def redirected() -> ToolRoutes:
    registry = ToolRoutes()
    registry.redirect("bls.gov", "Use fred_series; FRED mirrors BLS data.")
    return registry


def test_redirection_answers_for_its_own_host() -> None:
    assert redirected().advice("https://bls.gov/data") is not None


def test_redirection_answers_for_a_subdomain() -> None:
    assert redirected().advice("https://www.bls.gov/data") is not None


def test_redirection_ignores_a_lookalike_host() -> None:
    """A substring check would have accepted this registration as bls.gov."""
    assert redirected().advice("https://bls.gov.evil.example/data") is None


def test_redirection_ignores_the_domain_appearing_in_a_path() -> None:
    assert redirected().advice("https://example.com/proxy?to=bls.gov") is None


def test_unredirected_url_has_no_advice() -> None:
    assert redirected().advice("https://example.com/page") is None

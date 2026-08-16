"""Example MCP tools showing the tool pattern and Data Augmentation.

This is a TEMPLATE. Create your own tools following this pattern.

Key patterns:
1. Use @lup_tool(description, InputModel, OutputModel) decorator
2. Define input/output schemas as BaseModel with Field(description=...)
3. Handler receives a validated model instance (not a raw dict)
4. Handler must return a BaseModel instance (auto-serialized to MCP response)
5. Tool name defaults to the function name; override with name="..."
6. Metrics (duration, errors) are tracked automatically
7. tags=["requires:<service>"] marks tools that need an API key —
   ToolPolicy.filter_tools drops them when the key is missing, so the
   agent never sees tools it cannot use (see search_example below)

These tools are also the canonical template for the Data Augmentation
pattern (docs/orchestration.md § Data Augmentation): enrich external data inside
the tool so the agent receives structured, domain-aware results — never
raw HTML or half-empty records. Each of the three forms has a concrete
demonstration here:

- Domain dispatch — ``fetch_example`` routes known hosts to a
  specialized handler (``fetch_wiki_article``) instead of returning raw
  HTML for the agent to parse
- Null-filling — ``search_example`` recovers snippet fields the primary
  source omits from a fallback source (``fill_missing_snippets``)
  inside the tool
- Extraction — ``fetch_example`` distills a fetched page down to a
  focused answer through a nested ``query()`` call (``extract_answer``)
  when the caller passes ``extract`` (see docs/orchestration.md § Nested Agent)

Tool descriptions are the agent's only documentation for each tool.
A terse description forces the agent to guess when/why to use a tool,
which leads to misuse or underuse. A good description answers:
  - WHAT: What does this tool do? (concrete behavior)
  - WHEN: When should the agent use it? (triggers, conditions)
  - WHY: Why does this tool exist? (what problem it solves)
This keeps tool knowledge in the tool itself rather than in the prompt,
so descriptions stay accurate as tools are added or changed.
"""

from pathlib import PurePosixPath
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from lup.mcp import ToolError, lup_tool
from lup.runtime.query import query
from lup_template.agent.config import aux_model


# --- Schemas ---
# Define as BaseModel with Field(description=...) for validation + rich JSON Schema


class SearchInput(BaseModel):
    """Input for the search tool."""

    query: str = Field(description="Search query string")
    limit: int = Field(default=10, description="Maximum number of results to return")


class SearchResult(BaseModel):
    """A single search result."""

    title: str = Field(description="Title of the result")
    url: str = Field(description="URL of the result")
    snippet: str | None = Field(
        default=None, description="Short excerpt of the result content"
    )


class SearchOutput(BaseModel):
    """Output from the search tool."""

    query: str = Field(description="The query that was searched")
    results: list[SearchResult] = Field(description="Matching search results")
    count: int = Field(description="Total number of results")


class FetchInput(BaseModel):
    """Input for the fetch tool."""

    url: str = Field(description="URL to fetch content from")
    extract: str | None = Field(
        default=None,
        description=(
            "Optional question; when set, the tool distills the fetched "
            "content down to a focused answer instead of returning the "
            "full page"
        ),
    )


class FetchOutput(BaseModel):
    """Output from the fetch tool."""

    url: str = Field(description="The URL that was fetched")
    content: str = Field(
        description="Page content, or the focused answer when `extract` was set"
    )
    status: int = Field(description="HTTP status code")
    source: str = Field(
        description="Handler that produced the content (e.g. 'wiki-api', 'generic')"
    )
    extracted: bool = Field(
        default=False, description="Whether content was distilled by extraction"
    )


# --- Data augmentation helpers ---
# Enrichment runs inside the tool, before results reach the agent
# (docs/orchestration.md § Data Augmentation Pattern)


async def fill_missing_snippets(results: list[SearchResult]) -> list[SearchResult]:
    """Fill in snippets the primary source omitted, from a fallback source.

    Null-filling form: the tool recovers missing fields from an
    alternative endpoint before returning, so the agent never re-queries
    to patch gaps in the data.
    """

    async def repaired(result: SearchResult) -> SearchResult:
        if result.snippet is not None:
            return result
        # lup: template: recover the field from a real fallback endpoint
        # Example:
        #
        # summary = await fallback_api.page_summary(result.url)
        # return result.model_copy(update={"snippet": summary})

        # Placeholder fallback
        return result.model_copy(
            update={"snippet": f"Fallback summary for {result.url}"}
        )

    return [await repaired(result) for result in results]


async def fetch_wiki_article(url: str) -> FetchOutput:
    """Fetch a wiki article through its structured API instead of scraping HTML.

    Domain-dispatch form: ``fetch_example`` routes wikipedia.org URLs
    here so the agent gets clean article text, never raw markup.
    """
    title = PurePosixPath(urlparse(url).path).name

    # lup: template: implement the structured API call for this host
    # Example with the Wikipedia REST API:
    #
    # async with httpx.AsyncClient() as client:
    #     response = await client.get(
    #         f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    #     )
    #     response.raise_for_status()
    #     return FetchOutput(
    #         url=url,
    #         content=response.json()["extract"],
    #         status=response.status_code,
    #         source="wiki-api",
    #     )

    # Placeholder response
    return FetchOutput(
        url=url,
        content=f"Structured article text for {title!r}",
        status=200,
        source="wiki-api",
    )


async def fetch_generic(url: str) -> FetchOutput:
    """Plain fetch for hosts without a specialized handler."""

    # lup: template: implement real fetching here, or delete this example tool
    # Example with httpx:
    #
    # try:
    #     async with httpx.AsyncClient() as client:
    #         response = await client.get(url)
    #         response.raise_for_status()
    #         return FetchOutput(
    #             url=url,
    #             content=response.text[:5000],
    #             status=response.status_code,
    #             source="generic",
    #         )
    # except httpx.HTTPError as e:
    #     raise ToolError(f"Fetch failed: {e}") from e

    # Placeholder response
    return FetchOutput(
        url=url,
        content="Example content from the URL",
        status=200,
        source="generic",
    )


async def extract_answer(content: str, question: str) -> str:
    """Distill fetched content down to what answers *question*.

    Extraction form: a nested agent call (docs/orchestration.md § Nested Agent
    Pattern) turns a large text block into a focused answer inside the
    tool, so the raw page never has to occupy the main agent's context.
    The model comes from ``aux_model()`` so the nested call follows the
    session's backend.
    """
    from lup_template.agent.core import build_auxiliary_factory

    factory = build_auxiliary_factory(
        model=aux_model(),
        system_prompt=(
            "Answer the question using only the document provided. "
            "Reply with the answer alone; say so if the document does "
            "not contain one."
        ),
    )
    result = await query(factory, f"Question: {question}\n\nDocument:\n{content}")
    text = "\n\n".join(
        text for block in result.blocks if (text := block.text_payload) is not None
    )
    return text or "(no answer extracted)"


# --- Tool Implementations ---


# A handler is a free function by construction: `lup_tool` reads the tool's
# input schema off this first parameter, so SearchInput is the tool's call
# signature rather than a subject. The behaviour — reaching a search API and
# enriching what comes back — is the tool's, and a schema that only names
# arguments has no operation to carry. Put behaviour on a model when the model
# is what the operation is about, not when it is how the agent calls in.
# lup: ignore[model-free-function] — MCP tool handler; SearchInput is its input schema
@lup_tool(
    "Search for information using keyword queries. "
    "Use this when the agent needs to find data that isn't available in local notes "
    "or when exploring a topic before making decisions. "
    "Exists because the agent has no built-in knowledge beyond its training data. "
    "Results are enriched inside the tool: snippets the primary source omits are "
    "filled from a fallback source before they reach the agent. "
    "Returns a JSON object with {query, results: [{title, url, snippet}], count}. "
    "Replace this with your actual search implementation.",
    tags=["requires:example-api"],
)
async def search_example(params: SearchInput) -> SearchOutput:
    """Search for information, null-filling gaps before returning."""

    if not params.query:
        raise ToolError("Query is required")

    # lup: template: implement real search here, or delete this example tool
    # Example with a real search API:
    #
    # try:
    #     results = await search_api.search(params.query, limit=params.limit)
    # except SearchApiError as e:
    #     raise ToolError(f"Search failed: {e}") from e

    # Placeholder response: the second result arrives without a snippet,
    # the gap fill_missing_snippets recovers below
    results = [
        SearchResult(
            title="Example Result 1",
            url="https://example.com/1",
            snippet="Snippet delivered by the primary source",
        ),
        SearchResult(title="Example Result 2", url="https://example.com/2"),
    ]

    results = await fill_missing_snippets(results)
    return SearchOutput(query=params.query, results=results, count=len(results))


# FetchInput is what the agent fills in to call this tool, not a thing fetching
# is done to: dispatching on the host and distilling the page are the tool's
# work, and the helpers it calls carry the parts that are anyone's to reuse.
# lup: ignore[model-free-function] — MCP tool handler; FetchInput is its input schema
@lup_tool(
    "Fetch the content of a web page by URL, upgraded to structured data before "
    "it is returned: known hosts are routed to specialized API handlers instead "
    "of raw HTML, and an optional `extract` question distills large pages down "
    "to a focused answer via a nested agent call. "
    "Use this when the agent has a specific URL to retrieve — e.g., from search "
    "results, a known reference, or a link found in notes; pass `extract` when "
    "only one fact from the page matters. "
    "Exists because the agent cannot browse the web directly, and raw pages "
    "waste context that enrichment inside the tool saves. "
    "Returns a JSON object with {url, content, status, source, extracted}. "
    "Replace the handlers with your actual fetch implementations."
)
async def fetch_example(params: FetchInput) -> FetchOutput:
    """Fetch content from a URL, enriching it before it reaches the agent."""

    if not params.url:
        raise ToolError("URL is required")

    # Domain dispatch: known hosts get structured data from their API
    # instead of raw HTML the agent would have to parse
    match urlparse(params.url).hostname:
        case str(host) if host == "wikipedia.org" or host.endswith(".wikipedia.org"):
            fetched = await fetch_wiki_article(params.url)
        case _:
            fetched = await fetch_generic(params.url)

    if params.extract is None:
        return fetched

    answer = await extract_answer(fetched.content, params.extract)
    return fetched.model_copy(update={"content": answer, "extracted": True})


# --- Tool Collection ---
# Group tools for your MCP server

# lup: ignore[constant-declaration] — which tools this module defines, which is
# the module's own roster rather than a value a caller supplies
EXAMPLE_TOOLS = [
    search_example,
    fetch_example,
]
"""List of example tools for the example MCP server."""

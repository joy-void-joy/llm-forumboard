"""Reading a project's tracker issues, and answering them where they are read.

The library stays free of any forge: `lup.resolver` knows an issue only as a
number, a URL and some text. Reaching a tracker is workflow tooling's job, so
it lives here beside the pull-request queries that already use `gh`, and a
project on a different tracker supplies its own reader rather than waiting for
the library to learn an API it has no business knowing.
"""

# lup: solved: This module reads and comments, but filing an issue is still a raw
# `gh issue create --body-file` at the agent's own hand. Reporting friction is
# a step the workflow prescribes, so it should be a devtools command like every
# other prescribed step — one that knows the repository, so the report cannot
# be filed against the wrong one, and that carries the friction report's shape
# (command, exact error, state left behind, recovery cost, owning component)
# rather than leaving each session to remember it.

import json
import logging
from collections.abc import Iterator
from html import escape

import sh
from pydantic import BaseModel, Field

from lup.devtools.utils import decode_stderr, gh, repository_slug
from lup.resolver.models import IssueEvidence

logger = logging.getLogger(__name__)

EXCLUDED_LABEL = "resolver-skip"
"""Which label withholds an issue from intake.

Opt-out rather than opt-in: opt-in means remembering to label, and what goes
unlabelled goes unfixed. The name is this project's choice and no more, so a
caller passes its own.
"""

# lup: ignore[constant-declaration] — the fields the models below parse, spelled
# as `gh issue list --json` names them
ISSUE_FIELDS = "number,url,title,body,labels"


class FrictionReport(BaseModel, frozen=True, extra="forbid"):
    """Observed workflow friction in the shape the improvement loop consumes."""

    summary: str = Field(min_length=1, description="Concise issue title")
    component: str = Field(min_length=1, description="Component that owns the fix")
    command: str = Field(min_length=1, description="Exact command that was run")
    error: str = Field(min_length=1, description="Exact error that was observed")
    state: str = Field(
        min_length=1, description="State the failed operation left behind"
    )
    recovery_cost: str = Field(min_length=1, description="Work required to recover")

    def body(self) -> str:
        """Render the evidence without letting its contents alter Markdown."""
        sections: list[str] = [
            f"## Owning component\n\n<pre><code>{escape(self.component)}</code></pre>",
            f"## Exact command\n\n<pre><code>{escape(self.command)}</code></pre>",
            f"## Exact error\n\n<pre><code>{escape(self.error)}</code></pre>",
            f"## State left behind\n\n<pre><code>{escape(self.state)}</code></pre>",
            f"## Recovery cost\n\n<pre><code>{escape(self.recovery_cost)}</code></pre>",
        ]
        return "\n\n".join(sections)

    def file(self, repository: str = "", issue: int | None = None) -> str:
        """File or correct this report against the checkout's explicit repository."""
        slug = repository or repository_slug()
        if not slug:
            raise RuntimeError(
                "cannot file friction: origin names no GitHub repository"
            )
        operation = ["create"] if issue is None else ["edit", str(issue)]
        arguments = ["issue", *operation, "--repo", slug]
        arguments.extend(["--title", self.summary, "--body", self.body()])
        return gh.out(*arguments).strip()


class IssueLabel(BaseModel):
    """One label as `gh issue list --json labels` returns it."""

    name: str = ""


class IssueRow(BaseModel):
    """One issue as `gh issue list --json` returns it (aliases are gh's names)."""

    number: int
    url: str
    title: str
    body: str = ""
    labels: list[IssueLabel] = []

    def excluded_by(self, label: str) -> bool:
        return any(applied.name == label for applied in self.labels)

    def evidence(self) -> IssueEvidence:
        return IssueEvidence(
            number=self.number, url=self.url, title=self.title, body=self.body
        )


def fetch_open_issues(
    excluded: str = EXCLUDED_LABEL, limit: int = 200, repository: str = ""
) -> list[IssueEvidence]:
    """Every open issue a run should weigh, oldest first.

    Oldest first because that is the order they were found in, and a planner
    reading them in that order sees a later issue's context already
    established by the earlier one it followed from.

    A tracker that cannot be reached yields nothing and says so. Intake still
    has the tree's own notes, and a run that plans without the issues is
    better than a run that will not start.
    """
    slug = repository or repository_slug()
    arguments = ["issue", "list", "--state", "open", "--limit", str(limit)]
    if slug:
        arguments.extend(["-R", slug])
    try:
        rows = json.loads(gh.out(*arguments, "--json", ISSUE_FIELDS))
    except (sh.ErrorReturnCode, json.JSONDecodeError) as error:
        logger.warning("could not read open issues: %s", error)
        return []
    issues = [IssueRow.model_validate(row) for row in rows]
    kept = [issue for issue in issues if not issue.excluded_by(excluded)]
    return [issue.evidence() for issue in sorted(kept, key=lambda row: row.number)]


def comment_on_issues(
    issues: list[IssueEvidence], body: str, repository: str = ""
) -> list[int]:
    """Say where an issue was answered, on the issue. Never close it.

    A run's reviewer passing is not a human having read the code, so closing
    on the run's own judgement claims more than it knows. The comment is the
    honest half: it reaches whoever is watching the issue, and leaves the
    decision to close with them.
    """
    slug = repository or repository_slug()

    def commented() -> Iterator[int]:
        for issue in issues:
            arguments = ["issue", "comment", str(issue.number), "--body", body]
            if slug:
                arguments.extend(["-R", slug])
            try:
                gh.out(*arguments)
            except sh.ErrorReturnCode as error:
                logger.warning(
                    "could not comment on %s: %s",
                    issue.reference(),
                    decode_stderr(error),
                )
                continue
            yield issue.number

    return list(commented())

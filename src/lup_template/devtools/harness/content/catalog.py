# lup: ignore[constant-declaration]
# Every constant here is this repository's own composition — which skills and
# agents its plugin ships, and what that plugin is called. A composition root
# is where a judgement is finally made rather than passed on, so there is no
# caller above it to take these from.
"""This repository's harness content: what it inherits, and what only it has.

The skills automating agent work come from the library. Declared here are the
six whose subject is a lup-based project rather than the work inside one —
standing one up, installing the plugin into it, importing from a downstream
repo, and keeping the whole thing in step with upstream — plus the composed
rosters every document and the compiled plugin read.
"""

import lup.harness.models as models
from lup.devtools.harness.content.catalog import LIBRARY_AGENTS, LIBRARY_SKILLS
from lup_template.devtools.harness.content.skills.brainstorm import (
    SKILL as SKILL_BRAINSTORM,
)
from lup_template.devtools.harness.content.skills.import_skill import (
    SKILL as SKILL_IMPORT,
)
from lup_template.devtools.harness.content.skills.init import SKILL as SKILL_INIT
from lup_template.devtools.harness.content.skills.install import SKILL as SKILL_INSTALL
from lup_template.devtools.harness.content.skills.meta import SKILL as SKILL_META
from lup_template.devtools.harness.content.skills.update import SKILL as SKILL_UPDATE

PROJECT_SKILLS = [
    SKILL_BRAINSTORM,
    SKILL_IMPORT,
    SKILL_INIT,
    SKILL_INSTALL,
    SKILL_META,
    SKILL_UPDATE,
]
"""The skills only this repository has, because only it is a template.

Each one's subject is standing up or maintaining a lup-based project — they
have nothing to say inside one, which is why they stay behind when the rest
of the roster becomes the library's.
"""

PROJECT_AGENTS: list[models.Agent] = []
"""No agent here is about being a template; the whole roster is the library's."""

SKILLS = [*LIBRARY_SKILLS, *PROJECT_SKILLS]
"""Every skill this repository's plugin ships, inherited half first."""

AGENTS = [*LIBRARY_AGENTS, *PROJECT_AGENTS]
"""Every agent this repository's plugin ships."""

PLUGIN_NAME: models.NativeName = "lup"
"""The plugin every declared skill is invoked through.

Named here rather than read off the harness because the roster documents are
compiled into that harness: reading it back to render them would close the
loop between what is declared and what describes the declaration.
"""

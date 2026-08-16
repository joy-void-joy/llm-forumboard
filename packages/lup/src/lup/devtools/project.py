"""What an application tells the shared development tooling about itself.

The commands under :mod:`lup.devtools` are workflow rather than domain: they
scan, check, and generate against whichever repository they are pointed at.
A few facts about that repository are not theirs to infer — the import root
the application publishes, which initialization renames, and the roots whose
contents answer to some purpose other than production. Reaching back for
those by name is what kept these commands in an application package; taking
them as a declaration is what lets any adopter supply its own.
"""

from pydantic import BaseModel

from lup.codescan.boundaries import ApplicationRoots
from lup.codescan.common import RuleSelection
from lup.policy.kernel.rows import PathRoleRow


class DevProject(BaseModel, frozen=True):
    """The repository facts shared development tooling cannot work out alone."""

    package: str
    """The import root this application publishes.

    Only the application knows it: initialization renames the package, so a
    value written down in the library would go on naming one that is gone
    and silently resolve nothing.
    """

    path_roles: list[PathRoleRow] = []
    """Roots whose contents are judged by a purpose other than production.

    A test root is judged by whether it exercises production rather than by
    production's own conventions, so a scan that read them all the same way
    would report a fixture as a defect.
    """

    rules: RuleSelection = RuleSelection()
    """Which of the library's scan rules this repository holds itself to.

    Carried here so the sweep reads the selection the edit hook was compiled
    with: an application declares it once on its ``HookSet`` and hands it
    over, the way it already hands over its path roles.
    """

    roots: ApplicationRoots = ApplicationRoots()
    """Where this application is allowed to name a concrete implementation.

    The seam guard reads it to tell a composition root — which may say
    ``ClaudeSpellings`` because choosing is its whole job — from a module
    that reached past the abstraction it was handed.
    """

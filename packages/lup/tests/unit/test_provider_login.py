"""Every runtime answers where it keeps a login, in its own words.

The spellings are pinned here rather than left to the adapters alone because
the point of the value is that both runtimes have one: a runtime that grew a
config home without declaring it would leave an application reaching for a
literal again, which is exactly what the seam rule forbids above the adapter.
"""

from pathlib import Path

import pytest

from lup.adapters.claude.harness import CLAUDE_DISPATCHER
from lup.adapters.claude.login import CLAUDE_LOGIN
from lup.adapters.codex.harness import CODEX_DISPATCHER
from lup.adapters.codex.login import CODEX_LOGIN
from lup.policy.dispatcher import DispatcherDeclaration
from lup.runtime.login import ProviderLogin

RUNTIME_LOGINS = [
    (CLAUDE_LOGIN, "CLAUDE_CONFIG_DIR", ".credentials.json"),
    (CODEX_LOGIN, "CODEX_HOME", "auth.json"),
]


@pytest.mark.parametrize(
    ("login", "config_home_env", "credentials_file"),
    RUNTIME_LOGINS,
    ids=[login.config_home_env for login, _, _ in RUNTIME_LOGINS],
)
def test_each_runtime_spells_its_own_login_location(
    login: ProviderLogin, config_home_env: str, credentials_file: str
) -> None:
    assert login.config_home_env == config_home_env
    assert login.credentials_file == credentials_file


@pytest.mark.parametrize(
    "login", [CLAUDE_LOGIN, CODEX_LOGIN], ids=lambda login: login.config_home_env
)
def test_a_login_routes_a_home_and_finds_the_credential_inside_it(
    login: ProviderLogin, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    assert login.environment(home) == {login.config_home_env: str(home)}
    assert login.credentials_path(home).parent == home
    assert not login.logged_in(home)

    login.credentials_path(home).write_text("{}", encoding="utf-8")

    assert login.logged_in(home)


@pytest.mark.parametrize(
    ("declaration", "login"),
    [(CLAUDE_DISPATCHER, CLAUDE_LOGIN), (CODEX_DISPATCHER, CODEX_LOGIN)],
    ids=[CLAUDE_DISPATCHER.runtime_name, CODEX_DISPATCHER.runtime_name],
)
def test_a_dispatcher_takes_its_managed_root_from_the_login(
    declaration: DispatcherDeclaration, login: ProviderLogin
) -> None:
    """The hook dispatcher and the profile system name one environment.

    Derived rather than compared: the declaration reads the login, so this
    pins that it still does instead of guarding two literals against drift.
    """
    assert declaration.managed_root_env == login.config_home_env

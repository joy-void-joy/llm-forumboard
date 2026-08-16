"""Worktree-scoped Codex home selection and first-use initialization."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
import tomlkit
from tomlkit.items import Table

from lup.types import EnvVars
from lup.adapters.codex.home import (
    CodexWorktreeHomeStore,
    login_state,
    select_codex_home,
)


ACCOUNT_CONFIG = """\
model = "gpt-personal"

[features]
hooks = true

[hooks]
PreToolUse = []

[hooks.state."lup@account:hooks/hooks.json:pre_tool_use:0:0"]
enabled = true

[marketplaces.account]
source_type = "local"
source = "/account"

[plugins."lup@account"]
enabled = true
"""

PROFILE_CONFIG = """\
model_reasoning_effort = "high"

[plugins."lup@account"]
enabled = true
"""

# Nanosecond precision, as the runtime actually writes it.
SEEDED_CREDENTIAL = (
    '{"auth_mode": "chatgpt", "tokens": {"refresh_token": "seeded"},'
    ' "last_refresh": "2026-07-23T22:11:59.592425868Z"}'
)
ROTATED_CREDENTIAL = (
    '{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rotated"},'
    ' "last_refresh": "2026-08-05T19:23:03.507636146Z"}'
)


def credential_expiring(expiry: datetime) -> str:
    """An auth record whose access token states one expiry."""
    # Signed only so the token parses; the adapter never verifies it.
    token = jwt.encode({"exp": expiry}, key="x" * 32, algorithm="HS256")
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": token},
            "last_refresh": "2026-07-23T22:11:59.592425868Z",
        }
    )


def account_home_with(credential: str, root: Path) -> Path:
    """Build an account home holding one credential."""
    account = root / "account"
    account.mkdir()
    (account / "auth.json").write_text(credential, encoding="utf-8")
    return account


def test_worktree_home_is_stable_and_path_scoped(tmp_path: Path) -> None:
    store = CodexWorktreeHomeStore(
        account_home=tmp_path / "account",
        scoped_root=tmp_path / "scoped",
    )
    first = tmp_path / "tree" / "first"
    second = tmp_path / "tree" / "second"
    first.mkdir(parents=True)
    second.mkdir()

    assert store.home_for(first) == store.home_for(first)
    assert store.home_for(first) != store.home_for(second)
    assert store.home_for(first).name.startswith("first-")


def test_prepare_seeds_auth_and_sanitized_personal_settings(tmp_path: Path) -> None:
    account = tmp_path / "account"
    account.mkdir()
    auth = account / "auth.json"
    auth.write_text('{"token": "secret"}\n', encoding="utf-8")
    auth.chmod(0o600)
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    (account / "review.config.toml").write_text(PROFILE_CONFIG, encoding="utf-8")
    worktree = tmp_path / "tree" / "dev"
    worktree.mkdir(parents=True)
    store = CodexWorktreeHomeStore(account, tmp_path / "scoped")

    scoped = store.prepare(worktree, profile="review")

    assert (scoped / "auth.json").read_bytes() == auth.read_bytes()
    assert (scoped / "auth.json").stat().st_mode & 0o777 == 0o600
    assert scoped.stat().st_mode & 0o777 == 0o700
    config = tomlkit.parse((scoped / "config.toml").read_text(encoding="utf-8"))
    assert config["model"] == "gpt-personal"
    assert config["features"]["hooks"] is True
    assert "marketplaces" not in config
    assert "plugins" not in config
    hooks = config.item("hooks")
    assert isinstance(hooks, Table)
    assert "PreToolUse" in hooks
    # Trust is seeded, unlike installed state. The runtime will not run a
    # plugin's hooks until they are reviewed, so a scoped home that dropped
    # this installs the policy plugin and then runs ungoverned — present,
    # never consulted, and silent about it.
    state = hooks.item("state")
    assert isinstance(state, Table)
    assert "lup@account:hooks/hooks.json:pre_tool_use:0:0" in state
    profile = tomlkit.parse((scoped / "review.config.toml").read_text(encoding="utf-8"))
    assert profile["model_reasoning_effort"] == "high"
    assert "plugins" not in profile


def test_prepare_preserves_scoped_state_after_first_use(tmp_path: Path) -> None:
    account = tmp_path / "account"
    account.mkdir()
    (account / "auth.json").write_text("account-auth", encoding="utf-8")
    (account / "config.toml").write_text(ACCOUNT_CONFIG, encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account, tmp_path / "scoped")
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text("scoped-auth", encoding="utf-8")
    (scoped / "config.toml").write_text('model = "scoped"\n', encoding="utf-8")
    session = scoped / "sessions" / "saved.jsonl"
    session.parent.mkdir()
    session.write_text("saved\n", encoding="utf-8")
    (account / "auth.json").write_text("different-account-auth", encoding="utf-8")

    assert store.prepare(worktree) == scoped
    assert (scoped / "auth.json").read_text(encoding="utf-8") == "scoped-auth"
    assert (scoped / "config.toml").read_text(encoding="utf-8") == (
        'model = "scoped"\n'
    )
    assert session.read_text(encoding="utf-8") == "saved\n"


def test_explicit_home_and_environment_bypass_scoped_initialization(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(
        account_home=tmp_path / "account",
        scoped_root=tmp_path / "scoped",
    )
    configured = tmp_path / "configured"
    environment: EnvVars = {"CODEX_HOME": str(configured)}

    from_environment = select_codex_home(None, environment, worktree, store=store)
    explicit = select_codex_home(
        tmp_path / "explicit", environment, worktree, store=store
    )

    assert from_environment.path == configured
    assert from_environment.isolated is False
    assert explicit.path == tmp_path / "explicit"
    assert explicit.isolated is False
    assert not (tmp_path / "scoped").exists()


def test_default_selection_prepares_the_scoped_home(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(
        account_home=tmp_path / "account",
        scoped_root=tmp_path / "scoped",
    )

    selection = select_codex_home(None, {}, worktree, store=store)

    assert selection.path == store.home_for(worktree)
    assert selection.isolated is True
    assert selection.path.is_dir()


def test_a_rotated_account_login_reaches_a_stale_scoped_home(tmp_path: Path) -> None:
    account = account_home_with(ROTATED_CREDENTIAL, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account, tmp_path / "scoped")
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text(SEEDED_CREDENTIAL, encoding="utf-8")

    assert store.prepare(worktree) == scoped
    assert (scoped / "auth.json").read_text(encoding="utf-8") == ROTATED_CREDENTIAL


def test_a_rotation_inside_a_scoped_home_returns_to_the_account(
    tmp_path: Path,
) -> None:
    account = account_home_with(SEEDED_CREDENTIAL, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account, tmp_path / "scoped")
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text(ROTATED_CREDENTIAL, encoding="utf-8")

    assert store.publish(worktree) is True
    assert (account / "auth.json").read_text(encoding="utf-8") == ROTATED_CREDENTIAL


def test_publishing_an_unrotated_login_changes_nothing(tmp_path: Path) -> None:
    account = account_home_with(SEEDED_CREDENTIAL, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account, tmp_path / "scoped")
    store.prepare(worktree)

    assert store.publish(worktree) is False


def test_an_unreadable_credential_is_never_overwritten(tmp_path: Path) -> None:
    account = account_home_with("not-json", tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(account, tmp_path / "scoped")
    scoped = store.prepare(worktree)
    (scoped / "auth.json").write_text(ROTATED_CREDENTIAL, encoding="utf-8")

    assert store.prepare(worktree) == scoped
    assert (scoped / "auth.json").read_text(encoding="utf-8") == ROTATED_CREDENTIAL


def test_a_home_with_no_record_holds_no_login(tmp_path: Path) -> None:
    assert login_state(tmp_path).usable_at(datetime.now(UTC)) is False


def test_a_login_is_usable_until_the_issuer_says_otherwise(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    expiry = now + timedelta(days=5)
    (tmp_path / "auth.json").write_text(credential_expiring(expiry), encoding="utf-8")

    state = login_state(tmp_path)

    assert state.present is True
    assert state.expires_at == expiry.replace(microsecond=0)
    assert state.usable_at(now) is True
    assert state.usable_at(expiry + timedelta(seconds=1)) is False


def test_a_lapsed_login_is_caught_before_a_session_starts(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    lapsed = credential_expiring(now - timedelta(days=7))
    (tmp_path / "auth.json").write_text(lapsed, encoding="utf-8")

    assert login_state(tmp_path).usable_at(now) is False


def test_a_record_stating_no_expiry_is_left_to_the_runtime(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(SEEDED_CREDENTIAL, encoding="utf-8")

    assert login_state(tmp_path).usable_at(datetime.now(UTC)) is True


def test_profile_name_cannot_escape_the_scoped_home(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = CodexWorktreeHomeStore(
        account_home=tmp_path / "account",
        scoped_root=tmp_path / "scoped",
    )

    with pytest.raises(ValueError, match="invalid Codex profile name"):
        store.prepare(worktree, profile="../outside")

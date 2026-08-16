"""This project's setup integrations, over the reusable wizard framework.

The framework — env helpers, the status display, the wizard flow, and the
``Integration`` registry — lives in :mod:`lup.devtools.setup`. This module
holds only what *this* project configures, and composes the two into the
``setup`` command tree.

This is a **TEMPLATE**. Replace the integrations below with your domain's
actual services; the framework is reusable as-is.

Usage::

    $ uv run lup-devtools setup          # Full walkthrough
    $ uv run lup-devtools setup status   # Show what's configured
    $ uv run lup-devtools setup slack    # Just one integration

Customization:
    1. Append an ``Integration`` to ``INTEGRATIONS``. A token-based one is
       pure data: name, command slug, env keys, intro text, and the
       ``PromptField`` list to ask for. Its subcommand is generated for you.
    2. For a bespoke flow (OAuth files, detection, validation), pass a
       ``setup_func`` returning ``EnvVars`` instead of declarative fields.
    3. Override status display with ``status_func`` when env-key presence
       isn't the whole story.
    4. Shell helpers live in ``lup.devtools.utils`` (e.g.
       ``copy_to_clipboard`` for wizard steps that hand the user a value)
"""

import shutil
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import typer
from tzlocal import get_localzone_name

from lup_template.devtools.harness.composition import profile_directory
from lup.devtools.setup import (
    Integration,
    IntegrationStatus,
    PromptField,
    console,
    create_setup_app,
    mask,
    open_browser,
    read_env_local,
)
from lup.types import EnvVars
from lup.workspace.paths import project_root

CREDENTIALS_DIR = project_root() / "credentials"


def detect_system_timezone() -> str:
    """Detect the system's IANA timezone name, or "" if undetermined.

    Delegates to ``tzlocal``, which reads the right source for each
    platform (``/etc/localtime`` on Linux, the registry on Windows,
    system preferences on macOS) instead of string-munging just one.
    """
    try:
        return get_localzone_name()
    except ZoneInfoNotFoundError:
        return ""


# =====================================================================
# lup: template: example integration flows below (Slack, Google, timezone) —
# replace these setup/status functions with your domain's services
# =====================================================================


def slack_status(env: EnvVars) -> IntegrationStatus:
    """Custom status check for Slack."""
    bot = env.get("SLACK_BOT_TOKEN")  # lup: ignore[dict-get] — open env map
    app_token = env.get("SLACK_APP_TOKEN")  # lup: ignore[dict-get] — open env map
    if bot and app_token:
        return IntegrationStatus(ok=True, detail=mask(bot))
    return IntegrationStatus(ok=False, detail="not configured")


# lup: template: Replace with your Google API scopes and services.
def setup_google() -> EnvVars:
    """Walk through Google OAuth setup (Gmail, Calendar, etc)."""
    console.print()
    console.rule("[bold]Google (OAuth)[/]")
    console.print()

    token_path = CREDENTIALS_DIR / "token.json"
    creds_path = CREDENTIALS_DIR / "google.json"

    if token_path.exists():
        console.print("[green]Already authorized.[/]")
        if not typer.confirm("Re-authorize?", default=False):
            return {"GMAIL_CREDENTIALS_PATH": str(creds_path)}

    if not creds_path.exists():
        console.print(
            "  [bold]Step 1:[/] Create a Google Cloud project\n"
            "  [bold]Step 2:[/] Enable the APIs you need (Gmail, Calendar, etc.)\n"
            "  [bold]Step 3:[/] Configure OAuth consent screen\n"
            "  [bold]Step 4:[/] Add yourself as a test user\n"
            "  [bold]Step 5:[/] Create OAuth client ID (Desktop app)\n"
            "  [bold]Step 6:[/] Download the credentials JSON\n"
        )
        open_browser("https://console.cloud.google.com/apis/credentials")
        console.print()

        source = typer.prompt("Path to downloaded credentials JSON").strip()
        source_path = Path(source).expanduser().resolve()

        if not source_path.exists():
            console.print(f"[red]File not found:[/] {source_path}")
            raise typer.Abort()

        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, creds_path)
        console.print(f"  Copied to [bold]{creds_path}[/]")
    else:
        console.print(f"  Using existing credentials at [bold]{creds_path}[/]")

    console.print(
        "\n  [dim]To complete authorization, run your project's OAuth flow\n"
        "  with the credentials file above.[/]\n"
    )

    return {"GMAIL_CREDENTIALS_PATH": str(creds_path)}


def google_status(_env: EnvVars) -> IntegrationStatus:
    """Custom status check for Google OAuth."""
    token_path = CREDENTIALS_DIR / "token.json"
    creds_path = CREDENTIALS_DIR / "google.json"
    if token_path.exists():
        return IntegrationStatus(ok=True, detail="authorized")
    if creds_path.exists():
        return IntegrationStatus(
            ok=False, detail="credentials present, not yet authorized"
        )
    return IntegrationStatus(ok=False, detail="not configured")


def codex_backend_status(env: EnvVars) -> IntegrationStatus:
    """Rates present = budget caps enforceable on codex/openai."""
    rates = [
        key
        for key in ("CODEX_USD_PER_MTOK_INPUT", "CODEX_USD_PER_MTOK_OUTPUT")
        if env.get(key)  # lup: ignore[dict-get] — open env map
    ]
    if len(rates) == 2:
        return IntegrationStatus(ok=True, detail="rates set (budget caps enforceable)")
    return IntegrationStatus(
        ok=False, detail="no rates (budget caps unavailable on codex/openai)"
    )


def setup_timezone() -> EnvVars:
    """Walk through timezone configuration."""
    console.print()
    console.rule("[bold]Timezone[/]")
    console.print()

    env = read_env_local()
    system_tz = detect_system_timezone()
    current = env.get("AGENT_TIMEZONE", "")  # lup: ignore[dict-get] — open env map
    default = current or system_tz

    if system_tz:
        console.print(f"  Detected: [bold]{system_tz}[/]")

    tz = typer.prompt(
        "AGENT_TIMEZONE",
        default=default,
        show_default=bool(default),
    ).strip()

    if tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz)
            console.print(f"  [green]Valid[/] — {tz}")
        except (KeyError, ModuleNotFoundError):
            console.print(
                f"  [yellow]Warning:[/] '{tz}' may not be a valid IANA timezone"
            )
        return {"AGENT_TIMEZONE": tz}

    return {}


def timezone_status(env: EnvVars) -> IntegrationStatus:
    """Custom status check for timezone (not-configured is OK, just shows system default)."""
    tz = env.get("AGENT_TIMEZONE", "")  # lup: ignore[dict-get] — open env map
    if tz:
        return IntegrationStatus(ok=True, detail=tz)
    return IntegrationStatus(ok=False, detail="system default")


# =====================================================================
# Integration registry — order matters (services first, timezone last)
#
# lup: template: Replace these with your domain's actual integrations. A
# token-based one is just data: name, env keys, intro, browser URL, and
# the fields to prompt for. Bespoke flows pass ``setup_func`` instead.
# =====================================================================

# lup: ignore[constant-declaration] — which integrations this application offers
# to set up, decided here because nothing sits above it to be asked
INTEGRATIONS: list[Integration] = [
    Integration(
        name="Slack",
        command="slack",
        help="Set up Slack tokens.",
        env_keys=["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
        intro=(
            "  [bold]Create a Slack app:[/]\n"
            '  1. Go to api.slack.com/apps > "Create New App" > "From a manifest"\n'
            "  2. Pick your workspace, paste your app manifest (JSON tab)\n"
            "  3. Install to workspace\n"
            "  4. Copy the Bot User OAuth Token and App-Level Token\n"
        ),
        browser_url="https://api.slack.com/apps?new_app=1",
        fields=[
            PromptField(key="SLACK_APP_TOKEN", prompt="SLACK_APP_TOKEN (xapp-...)"),
            PromptField(key="SLACK_BOT_TOKEN", prompt="SLACK_BOT_TOKEN (xoxb-...)"),
            PromptField(
                key="SLACK_USER_ID",
                prompt="SLACK_USER_ID (your Slack user ID, or email for lookup)",
            ),
        ],
        status_func=slack_status,
    ),
    Integration(
        name="Google (OAuth)",
        command="google",
        help="Set up Google OAuth.",
        env_keys=["GMAIL_CREDENTIALS_PATH"],
        setup_func=setup_google,
        status_func=google_status,
    ),
    Integration(
        name="Notion",
        command="notion",
        help="Set up Notion integration.",
        env_keys=["NOTION_TOKEN"],
        intro=(
            "  Create an [bold]Internal[/] Notion integration:\n"
            "  1. Go to notion.so/profile/integrations/internal\n"
            "  2. Enter a name, keep type as Internal, submit\n"
            "  3. Copy the Internal Integration Secret\n"
            "  4. Share pages: open page > ... > Connections > add your integration\n"
        ),
        browser_url="https://www.notion.so/profile/integrations/internal",
        fields=[
            PromptField(key="NOTION_TOKEN", prompt="NOTION_TOKEN (secret_...)"),
            PromptField(
                key="NOTION_ALLOWED_PARENT_ID",
                prompt="NOTION_ALLOWED_PARENT_ID (page/DB ID for writes, optional)",
            ),
        ],
    ),
    Integration(
        name="Example API",
        command="api-key",
        help="Set up Example API key.",
        env_keys=["EXAMPLE_API_KEY"],
        intro="  Sign up and get an API key from the service provider.\n",
        fields=[PromptField(key="EXAMPLE_API_KEY", prompt="EXAMPLE_API_KEY")],
    ),
    Integration(
        name="Codex/OpenAI pricing",
        command="codex",
        help="Set Codex/OpenAI per-MTok pricing (enables budget caps).",
        env_keys=["CODEX_USD_PER_MTOK_INPUT", "CODEX_USD_PER_MTOK_OUTPUT"],
        intro=(
            "  Budget caps on AGENT_SDK=codex/openai need per-MTok USD rates\n"
            "  (the Codex SDK reports tokens, not cost). Leave blank to skip —\n"
            "  a budget without rates fails loudly at session start.\n"
        ),
        fields=[
            PromptField(
                key="CODEX_USD_PER_MTOK_INPUT",
                prompt="CODEX_USD_PER_MTOK_INPUT",
                secret=False,
                parse=float,
            ),
            PromptField(
                key="CODEX_USD_PER_MTOK_OUTPUT",
                prompt="CODEX_USD_PER_MTOK_OUTPUT",
                secret=False,
                parse=float,
            ),
            PromptField(
                key="CODEX_USD_PER_MTOK_CACHED_INPUT",
                prompt="CODEX_USD_PER_MTOK_CACHED_INPUT",
                secret=False,
                parse=float,
            ),
        ],
        status_func=codex_backend_status,
    ),
    Integration(
        name="Timezone",
        command="timezone",
        help="Set timezone.",
        env_keys=["AGENT_TIMEZONE"],
        setup_func=setup_timezone,
        status_func=timezone_status,
    ),
]


app = create_setup_app(INTEGRATIONS, profile_directory())

"""The browser surface: set Notion up, then enrol people into the sync.

Two jobs, in the order somebody does them. **Setup** is the Notion
integration and the two databases — the same flow as ``lup-devtools setup
notion``, because the parts a person must do by hand (make an integration,
make a page, grant access) are browser work anyway and it is absurd to send
them back to a terminal in between. **Enrolment** is a claude.ai login
streamed from here to their browser, and the deliberate act of joining the
roster.

Everything else stays a CLI command. An operator has a terminal, and a page
that grew to cover the pipeline would need authentication of its own.

It binds loopback by default. The socket drives a real browser holding a real
session, and the setup form carries an integration secret, so reaching this
from another machine is something to arrange deliberately (an SSH tunnel, or a
reverse proxy that authenticates), never a default.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

from lup.devtools.setup import read_env_local, write_env_local

from forumboard.claudeai.browser import context_dir, has_session
from forumboard.claudeai.login import KeyEvent, MouseEvent, StreamingLogin, WheelEvent
from forumboard.devtools.setup import create_databases, page_id_of
from forumboard.notion.client import NotionError, NotionWorkspace
from forumboard.profiles import ProfileState, read_roster, write_roster

logger = logging.getLogger(__name__)


class ProfileView(BaseModel, frozen=True):
    """One profile as the page shows it."""

    name: str
    enrolled: bool
    signed_in: bool
    summary: str


class ProfileListing(BaseModel, frozen=True):
    """Everything the page needs to render."""

    profiles: list[ProfileView] = []


class EnrolRequest(BaseModel):
    """Anything somebody wants recorded alongside their agreement."""

    note: str = ""


class SetupState(BaseModel, frozen=True):
    """How far Notion has been configured, as the page shows it."""

    token_set: bool = False
    identity: str = ""
    databases_set: bool = False
    error: str = ""

    def ready(self) -> bool:
        """Whether the pipeline could publish as things stand."""
        return self.token_set and self.databases_set and not self.error


class TokenRequest(BaseModel):
    """An integration secret somebody pasted."""

    token: str = ""


class ParentRequest(BaseModel):
    """The page the databases should be created under."""

    parent: str = ""


async def read_setup() -> SetupState:
    """Whether Notion is configured, and who the token belongs to."""
    env = read_env_local()
    token = env.get("NOTION_TOKEN", "")  # lup: ignore[dict-get] — an open env map
    databases = all(
        env.get(key)  # lup: ignore[dict-get] — same open env map
        for key in ("NOTION_DISCUSSIONS_DATABASE_ID", "NOTION_WORLDVIEW_DATABASE_ID")
    )
    if not token:
        return SetupState()
    try:
        identity = await NotionWorkspace(token).whoami()
    except NotionError as error:
        return SetupState(token_set=True, databases_set=databases, error=str(error))
    return SetupState(token_set=True, identity=identity, databases_set=databases)


class ViewerMessage(BaseModel):
    """One message from the page: an input event, in one of three shapes."""

    mouse: MouseEvent | None = None
    wheel: WheelEvent | None = None
    key: KeyEvent | None = None

    def event(self) -> MouseEvent | WheelEvent | KeyEvent | None:
        """Whichever event this carries, or None where it carries none."""
        return self.mouse or self.wheel or self.key


async def push_frames(socket: WebSocket, session: StreamingLogin) -> None:
    """Send each captured frame, and say when a session appears."""
    while True:
        frame = await session.frames.next_frame()
        await socket.send_json({"frame": frame})
        if await session.signed_in():
            await socket.send_json({"signed_in": True})
            return


async def pull_input(socket: WebSocket, session: StreamingLogin) -> None:
    """Replay whatever the viewer does, ignoring anything malformed."""
    while True:
        payload = await socket.receive_json()
        try:
            message = ViewerMessage.model_validate(payload)
        except ValidationError:
            logger.debug("ignoring an unrecognised viewer message")
            continue
        event = message.event()
        if event is not None:
            await session.apply(event)


def create_app(project_root: Path, profiles_root: Path) -> FastAPI:
    """Build the enrolment app for one checkout."""
    app = FastAPI(title="forumboard enrolment", docs_url=None, redoc_url=None)

    def listing() -> ProfileListing:
        roster = read_roster(project_root)
        if not profiles_root.is_dir():
            return ProfileListing()
        names = sorted(
            entry.name for entry in profiles_root.iterdir() if entry.is_dir()
        )
        return ProfileListing(
            profiles=[
                ProfileView(
                    name=state.name,
                    enrolled=state.enrolled,
                    signed_in=state.signed_in,
                    summary=state.describe(),
                )
                for name in names
                if (
                    state := ProfileState(
                        name=name,
                        enrolled=roster.holds(name),
                        signed_in=has_session(profiles_root, name),
                    )
                )
            ]
        )

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return ENROLMENT_PAGE

    @app.get("/api/setup")
    async def setup_state() -> SetupState:
        return await read_setup()

    @app.post("/api/setup/token")
    async def save_token(request: TokenRequest) -> SetupState:
        """Verify an integration secret before recording it.

        Verifying first is the point: a token saved and only exercised by the
        next sync fails hours later, in a log, to nobody.
        """
        if not request.token:
            return SetupState(error="no token was given")
        try:
            identity = await NotionWorkspace(request.token).whoami()
        except NotionError as error:
            return SetupState(error=f"that token was rejected: {error}")
        write_env_local({"NOTION_TOKEN": request.token})
        logger.info("Recorded a Notion token for %s", identity)
        return await read_setup()

    @app.post("/api/setup/databases")
    async def make_databases(request: ParentRequest) -> SetupState:
        """Create both databases under the page somebody named."""
        env = read_env_local()
        token = env.get("NOTION_TOKEN", "")  # lup: ignore[dict-get] — an open env map
        if not token:
            return SetupState(error="set the integration secret first")
        try:
            parent = page_id_of(request.parent)
        except ValueError as error:
            return SetupState(token_set=True, error=str(error))
        try:
            created = await create_databases(token, parent)
        except NotionError as error:
            return SetupState(
                token_set=True,
                error=(
                    f"{error} — the usual cause is that the integration has "
                    "not been added to that page under ⋯ → Connections"
                ),
            )
        write_env_local({"NOTION_PARENT_PAGE_ID": parent, **created})
        logger.info("Created both databases under %s", parent)
        return await read_setup()

    @app.get("/api/profiles")
    async def profiles() -> ProfileListing:
        return listing()

    @app.post("/api/profiles/{name}/enrol")
    async def enrol(name: str, request: EnrolRequest) -> ProfileListing:
        roster = read_roster(project_root)
        write_roster(project_root, roster.with_profile(name, request.note))
        logger.info("Enrolled %s", name)
        return listing()

    @app.post("/api/profiles/{name}/withdraw")
    async def withdraw(name: str) -> ProfileListing:
        roster = read_roster(project_root)
        write_roster(project_root, roster.without_profile(name))
        logger.info("Withdrew %s", name)
        return listing()

    @app.websocket("/ws/login/{name}")
    async def login_socket(socket: WebSocket, name: str) -> None:
        """Stream a login browser to the viewer, and their input back."""
        await socket.accept()
        session = StreamingLogin(context_dir(profiles_root, name))
        try:
            await session.start()
        except Exception as error:
            logger.exception("Could not start a login browser for %s", name)
            await socket.send_json({"error": str(error)})
            await socket.close()
            return
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(push_frames(socket, session))
                group.create_task(pull_input(socket, session))
        except* WebSocketDisconnect:
            logger.info("The viewer closed the login for %s", name)
        finally:
            await session.stop()

    return app


# lup: ignore[constant-declaration] — the enrolment page itself, one file with
# no build step so that serving it needs nothing but this process
ENROLMENT_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>forumboard — enrolment</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
         padding: 0 1rem; color: #1a1a1a; background: #fdfdfc; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.05rem; margin-top: 2rem; color: #333; }
  p.lede { color: #555; }
  .step { border-left: 3px solid #e3e3e0; padding: .1rem 0 .1rem 1rem;
          margin: .8rem 0; }
  .step[hidden] { display: none; }
  input { font: inherit; padding: .25rem .4rem; }
  .ok { color: #16794a; }
  .bad { color: #a3331f; }
  li { margin: .4rem 0; display: flex; gap: .6rem; align-items: center; }
  .name { font-weight: 600; min-width: 10rem; }
  .state { color: #666; flex: 1; }
  button { font: inherit; padding: .2rem .7rem; cursor: pointer; }
  #screen { border: 1px solid #ccc; max-width: 100%; display: none;
            margin-top: 1rem; }
  #status { margin-top: 1rem; color: #444; }
</style>
</head>
<body>
<h1>forumboard</h1>

<section id="setup">
  <h2>1 · Notion</h2>
  <p class="state" id="setup-state">Checking…</p>
  <div id="setup-token" class="step">
    <p>
      Create an <b>Internal</b> integration in Notion, then paste its secret.
      It is checked against Notion before it is saved.
    </p>
    <p><a href="https://www.notion.so/profile/integrations/internal"
          target="_blank" rel="noreferrer">Open Notion integrations →</a></p>
    <input id="token" type="password" placeholder="ntn_… or secret_…" size="42">
    <button id="save-token">Save</button>
  </div>
  <div id="setup-db" class="step">
    <p>
      Make a page for the databases to live under, then add the integration to
      it (<b>⋯ → Connections</b>). Notion grants an integration nothing by
      default, so that step is what makes this one possible. Paste the page URL:
    </p>
    <input id="parent" type="text" placeholder="https://www.notion.so/…" size="52">
    <button id="make-db">Create both databases</button>
  </div>
</section>

<section>
  <h2>2 · People</h2>
  <p class="lede">
    Sign in to claude.ai so your conversations can be reviewed, and say whether
    you agree to be synced. Enrolling is what starts the sync — signing in alone
    does not.
  </p>
  <ul id="profiles"></ul>
</section>

<canvas id="screen" width="1280" height="800" tabindex="0"></canvas>
<div id="status"></div>
<script>
const listEl = document.getElementById('profiles');
const screen = document.getElementById('screen');
const statusEl = document.getElementById('status');
const ctx = screen.getContext('2d');

async function refresh() {
  const res = await fetch('/api/profiles');
  const data = await res.json();
  listEl.innerHTML = '';
  for (const p of data.profiles) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = p.name;
    const state = document.createElement('span');
    state.className = 'state';
    state.textContent = p.summary;
    li.append(name, state);
    const signIn = document.createElement('button');
    signIn.textContent = p.signed_in ? 'Sign in again' : 'Sign in';
    signIn.onclick = () => startLogin(p.name);
    li.append(signIn);
    const toggle = document.createElement('button');
    toggle.textContent = p.enrolled ? 'Withdraw' : 'Enrol';
    toggle.onclick = async () => {
      const path = p.enrolled ? 'withdraw' : 'enrol';
      await fetch(`/api/profiles/${p.name}/${path}`, {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({note: ''}),
      });
      refresh();
    };
    li.append(toggle);
    listEl.append(li);
  }
}

function startLogin(name) {
  statusEl.textContent = 'Starting a browser…';
  screen.style.display = 'block';
  screen.focus();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${proto}://${location.host}/ws/login/${name}`);
  socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.error) { statusEl.textContent = 'Error: ' + msg.error; return; }
    if (msg.signed_in) {
      statusEl.textContent = 'Signed in. You can enrol now.';
      screen.style.display = 'none';
      socket.close();
      refresh();
      return;
    }
    const img = new Image();
    img.onload = () => ctx.drawImage(img, 0, 0, screen.width, screen.height);
    img.src = 'data:image/jpeg;base64,' + msg.frame;
    statusEl.textContent = 'Sign in below.';
  };
  socket.onclose = () => { screen.style.display = 'none'; };

  const at = (e) => {
    const box = screen.getBoundingClientRect();
    return {
      x: (e.clientX - box.left) * (screen.width / box.width),
      y: (e.clientY - box.top) * (screen.height / box.height),
    };
  };
  const send = (payload) => {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
  };
  screen.onmousedown = (e) => {
    const p = at(e);
    send({mouse: {action: 'mousePressed', x: p.x, y: p.y}});
  };
  screen.onmouseup = (e) => {
    const p = at(e);
    send({mouse: {action: 'mouseReleased', x: p.x, y: p.y}});
  };
  screen.onmousemove = (e) => {
    const p = at(e);
    send({mouse: {action: 'mouseMoved', x: p.x, y: p.y, button: 'none'}});
  };
  screen.onwheel = (e) => {
    e.preventDefault();
    const p = at(e);
    send({wheel: {x: p.x, y: p.y, delta_x: e.deltaX, delta_y: e.deltaY}});
  };
  screen.onkeydown = (e) => {
    e.preventDefault();
    const text = e.key.length === 1 ? e.key : '';
    send({key: {action: text ? 'keyDown' : 'rawKeyDown', key: e.key,
                code: e.code, text: text}});
  };
  screen.onkeyup = (e) => {
    e.preventDefault();
    send({key: {action: 'keyUp', key: e.key, code: e.code}});
  };
}

const setupState = document.getElementById('setup-state');
const tokenStep = document.getElementById('setup-token');
const dbStep = document.getElementById('setup-db');

function showSetup(s) {
  if (s.error) {
    setupState.className = 'state bad';
    setupState.textContent = s.error;
  } else if (s.token_set && s.databases_set) {
    setupState.className = 'state ok';
    setupState.textContent = `Ready — connected as ${s.identity}.`;
  } else if (s.token_set) {
    setupState.className = 'state';
    setupState.textContent =
      `Connected as ${s.identity}. The databases have not been made yet.`;
  } else {
    setupState.className = 'state';
    setupState.textContent = 'No integration secret yet.';
  }
  tokenStep.hidden = s.token_set && !s.error;
  dbStep.hidden = !s.token_set || s.databases_set;
}

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(body),
  });
  return res.json();
}

async function loadSetup() {
  showSetup(await (await fetch('/api/setup')).json());
}

document.getElementById('save-token').onclick = async () => {
  const token = document.getElementById('token').value.trim();
  setupState.textContent = 'Checking the secret with Notion…';
  showSetup(await post('/api/setup/token', {token}));
};

document.getElementById('make-db').onclick = async () => {
  const parent = document.getElementById('parent').value.trim();
  setupState.textContent = 'Creating the databases…';
  showSetup(await post('/api/setup/databases', {parent}));
};

loadSetup();
refresh();
</script>
</body>
</html>
"""

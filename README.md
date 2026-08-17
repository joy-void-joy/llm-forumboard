# forumboard

Reads the claude.ai conversations of people who have enrolled, decides what may
be published, removes what may not, and keeps a browsable picture of what is
currently happening.

Built on [lup](https://github.com/joy-void-joy/lup), which arrives as a pinned
dependency rather than as source in this tree.

## What it does

Six steps, and the order of the gates is the design:

1. **Sync** — per enrolled profile, read the conversations that changed since
   that profile's cursor. A profile's session is a stored browser context, so
   an expired cookie is re-read rather than stalling the sync silently.
2. **Review** (Opus, no tools) — publish or skip, what must not appear, and a
   plan. The plan steers the editor and is **never published**.
3. **Edit** (Opus, no tools) — produce the page, including the keypoints that
   *are* published. The editor may abort outright and may redact past what the
   plan asked for; close reading finds what a skim misses, which is why it
   gets the second look.
4. **Publish** — one Notion page per conversation, keyed on the conversation
   id, rewritten in place when the conversation grows.
5. **Worldview** — an agent reads what is unmerged and rewrites the topic pages
   it touches, whole.
6. **Briefings** — periodic recaps, written from what was published.

### Two invariants

**Redaction removes; it never marks.** A `[REDACTED]` beside a name announces
that this person's *something* was sensitive, and often what kind. The contract
is that the text must read as though the removed part was never said. It is
declared once, in `agent/prompts.py`, and quoted into every prompt that touches
a transcript.

**The worldview is rewritten, never appended to.** It answers "what is
happening now". A page that accumulates becomes a log, and a log cannot answer
that. `write_topic` replaces a page; there is deliberately no append.

### Enrolment is an explicit act

A profile directory existing under `.lup/profiles` means somebody has a Claude
account configured here. It does **not** mean their conversations are read.
That is `config/roster.json`, which is committed — so who is being read is
visible in a diff and reviewable by the people it names.

## Getting started

```bash
uv sync
uv run forumboard setup     # the browser, Notion, the timezone, and who is read
uv run forumboard doctor    # says whether all of it took
```

`setup` walks four things. The **login browser** is a Chromium download that
every claude.ai sign-in runs in — without it no profile can be enrolled at all.
**Notion** creates the integration and both databases with the schema this
repository declares, rather than leaving you to assemble columns by hand.
**Timezone** names the windows briefings are labelled with. **Profiles** signs
somebody into claude.ai and records their enrolment, which is the step that
decides whether the pipeline reads anybody at all.

Any of them can be run alone — `uv run forumboard setup browser`,
`setup notion`, `setup timezone`, `setup profiles` — and `setup status` shows
where each stands, including how many profiles are both enrolled and signed in.

`setup profiles` walks somebody at this machine through both halves. To do
them separately, or for somebody who is already signed in:

```bash
uv run forumboard profile login <name>   # opens a browser here
uv run forumboard profile enrol <name>
```

…and for somebody at their own machine:

```bash
uv run forumboard serve                  # http://127.0.0.1:8781
```

The served page carries both jobs: configuring Notion, and signing into
claude.ai over a browser streamed from this machine to theirs. It binds
loopback by default — the socket drives a real browser holding a real session,
so reaching it from elsewhere is something to arrange deliberately (an SSH
tunnel, or a reverse proxy that authenticates).

## Running it

```bash
uv run forumboard run              # the daemon: all three loops
```

Every loop has a one-shot twin, which is how anything here gets debugged:

```bash
uv run forumboard sync-once        # fetch, review, edit, publish
uv run forumboard worldview-once   # fold unmerged discussions in
uv run forumboard briefings-once   # write whichever briefings are owed
uv run forumboard once             # one of each, in order
```

And to see what was decided:

```bash
uv run forumboard profile list
uv run forumboard profile status <name>
```

## Configuration

`.env` holds the defaults and documents every variable; `.env.local` holds
secrets, is gitignored, and overrides them. `src/forumboard/agent/config.py` is
the only module that reads the environment.

Raw transcripts and sync cursors live under `notes/`, which is gitignored. They
are unredacted by definition — they are the input to redaction — so they never
reach a commit.

## Layout

| Path | What lives there |
| --- | --- |
| `agent/` | The models each pass decides in, the prompts, and the composition root over lup's runtime |
| `claudeai/` | The unofficial conversation API, the per-profile browser session, and the streamed remote login |
| `notion/` | The workspace client, the database schema declared once, and the two repositories over it |
| `pipeline/` | The passes and the loops that drive them |
| `profiles.py` | The enrolment roster |

## Things that will catch you out

- **Notion's API moved.** Since 2025-09-03 a database holds *data sources*, and
  queries, schemas, and page parents all name the data source rather than the
  database. Configuration records database ids because that is what a person
  can copy from a URL; `NotionWorkspace.data_source_of` is the one place they
  are bridged.
- **Shrinking a Notion page is opt-in.** `allow_deleting_content` defaults to
  false, so a rewrite that removes redacted material would silently no-op
  without it. Every rewrite here sets it.
- **Page bodies are Markdown.** Notion accepts and returns it directly — there
  is no block-conversion layer.

## Development

```bash
uv run pyright
uv run ruff check .
uv run pytest
uv run lup-devtools dev check          # the repository's own gates
```

`dev check` covers the code. Whether this deployment can actually *run* — the
browser, the token, both database schemas — is `forumboard doctor`, because
those are facts about a machine rather than about the tree.

`.claude/CLAUDE.md` and `AGENTS.md` are generated from
`src/forumboard/devtools/harness/content/guidance.py` — edit the declaration,
then `uv run lup-devtools harness generate all`.

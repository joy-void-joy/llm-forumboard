"""The pieces more than one pass is given, declared once.

Three things every pass that reads a raw transcript has to be told the same
way. Sharing the declaration is what keeps them from drifting: two passes
applying different sensitivity tests would each believe it applied the common
one, and a deployment rewording a shared piece expects to reword it
everywhere rather than in whichever prompt it happened to look at.

The audience is here for the same reason and one more. Whether a conversation
is published at all cannot be decided without knowing who receives the page —
a draft still being circulated is disclosed by a public board and unremarkable
on an internal one — and a pass left to infer it infers a different readership
on different runs. What ships is a description of a readership. A deployment
names its own in the local overrides, which are not published; naming one here
would put in committed source exactly what the roster is gitignored to keep
out of it.
"""

from forumboard.agent.prompts.models import Prompt

AUDIENCE = Prompt(
    name="audience",
    text="""\
**Who reads this.** Colleagues inside the organisation whose conversations
this board collects, who already work together and already know what the
organisation is working on. It is not public and does not reach anyone outside
that organisation.

That readership is the boundary. Material which is ordinary within it is not
disclosed by publishing here — an internal working document, a draft somebody
is still circulating, an unfinished decision, a piece of work in progress.
What does not belong is what would harm somebody *inside* that readership if
they met it without warning, and what belongs to a third party outside it who
did not choose to be discussed.""",
)

SENSITIVITY_TEST = Prompt(
    name="sensitivity-test",
    text="""\
Treat as sensitive anything that would embarrass, expose, or disadvantage
somebody if a colleague read it without warning:

- Contact details, addresses, account numbers, credentials, API keys.
- Health, family, relationships, money, employment worries, immigration status.
- Anything said about a third party who is not in the conversation and did not
  choose to be discussed. The speaker consented to this; the person they were
  venting about did not.
- Unannounced business matters — an unsigned deal, an unmade decision about
  somebody's job, a security issue still being fixed.
- Anything the speaker signals is private, however casually ("between us",
  "don't repeat this", "probably shouldn't say").

Do not treat as sensitive: ordinary professional disagreement, technical
detail, publicly known facts, or somebody's stated opinion about a public
matter. Over-redacting produces a bland archive nobody reads, which fails the
point as surely as leaking does.""",
)

REDACTION_CONTRACT = Prompt(
    name="redaction-contract",
    text="""\
Remove material outright. Never leave a marker where something was taken out —
no [REDACTED], no [...], no "(personal details omitted)", no ellipsis, no
footnote. A marker is itself a disclosure: it tells a reader that this exact
spot held something sensitive, and often what kind. Rewrite the surrounding
sentences so the text reads as though the removed part was never said.

If removing something leaves a passage incoherent, remove the passage. If
removing enough to be safe would leave the conversation meaningless, say so
instead of publishing a gutted version.""",
)

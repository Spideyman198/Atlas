# The chat panel

What a user sees, and why the browser is not allowed to talk to the engine.

## The browser never holds a context token

A context token is a bearer credential: whoever has it can act as that user
against the engine for as long as it lives ([ADR-0006](adr/0006-data-access-and-authorization.md)).
Handing one to a page would give it to every script running in that page.

So the browser asks Odoo, and Odoo does the rest:

```
   browser ──POST /atlas/chat/ask──▶ Odoo ──POST /v1/chat──▶ engine
            session cookie, CSRF        mints the token,
                                        relays the stream
```

The panel sends a session cookie and a CSRF token, which it already has. Nothing
else. The engine's address is not reachable from the browser at all in a normal
deployment, and nothing in the UI needs it to be.

The request goes as form fields rather than a JSON body so Odoo's CSRF check —
which reads form parameters — still applies.

## Streaming costs a second cursor

Odoo commits the request transaction when the handler returns. That happens
*before* werkzeug iterates the response body, so anything the generator writes
needs a cursor of its own, and everything it needs must be captured before the
first yield — `request` is not usable inside it either.

This is why `controllers/chat.py` reads as it does: gather state, return a
generator, open a fresh cursor at the end to store the answer.

**The question is stored before the stream opens; the answer when it closes.** A
connection dropped in between loses the answer and keeps the question, which is
the right way round — the user can see what they asked and ask it again.

## Events

The panel reads the response body with a stream reader rather than `EventSource`,
because `EventSource` cannot issue a POST and the question, conversation and CSRF
token all have to travel in the body.

| Event | Carries |
| --- | --- |
| `open` | the conversation id, which a new conversation does not have until now |
| `delta` | text as it is generated |
| `done` | the whole answer, its citations, intent, tools called, usage |
| `error` | one sentence to show the user |

Errors arrive as events, not status codes. Once the first byte is out the status
line is gone.

## Citations are chips, and chips open records

An answer marks its sources inline — `[1]`, `[2]` — and lists them underneath as
chips carrying the record's name. Clicking one opens that record in Odoo.

A citation with no resolvable target is dropped at write time rather than
rendered as a dead chip. A chip that opens nothing is worse than one that is not
there: it looks like evidence until you click it.

Markers are only rendered as markers when they match a citation that came back.
The engine already removes the ones pointing at nothing
([orchestration.md](orchestration.md)); this is the second half of the same rule,
applied where it is visible.

Answer text is escaped before any markup is applied. An answer quotes customer
records, and a customer whose name contains a tag is a customer, not an attack —
but it renders as one if the escaping is skipped.

## Suggestions

An empty panel is hard to start using, and "ask me anything" is not true: what
Atlas can answer depends on which modules are installed and what the person
asking may read.

So the suggestions are derived. Each names the model it needs, and one whose
model is missing from the database — or that this user cannot read — is not
shown. Nobody is offered a question that would come back as "you do not have
access to that". Populated models sort first, because a suggestion that ends in
"there are none" is a poor first impression.

## Tests

`test_chat_controller.py` covers the relay and what it writes down, with the
engine scripted. `test_chat_tour.py` drives a real browser through the
acceptance criterion:

    a first-time user completes a question-to-cited-answer round trip
    without instructions

The tour is written as that user. It starts on an empty panel, clicks only what
is visible, and finishes on the record the answer cited. A step that cannot be
performed by clicking is a step a first-time user cannot take, so there are none.

**Browser tests skip rather than fail when the browser is missing.** The tours
here first "passed" that way, having run nothing at all: `websocket-client` was
absent, Odoo logged a warning and skipped, and the suite reported green.
`TestTheBrowserTestsCanRun` now asserts that a browser and the websocket client
are both present, so the same situation is a red build. The image installs
Google Chrome — Ubuntu's `chromium` on Noble is a stub that installs a snap and
produces a binary that cannot start in a container.

## Keyboard and theme

Enter sends, Shift+Enter breaks the line. The other way round makes a chat panel
feel like a form.

Colours come from Odoo's own CSS variables, so the panel follows the backend's
light and dark themes rather than carrying a second set of rules that has to be
kept in step. The typing indicator stops animating for a reader who has asked
the system to stop animating things.

On a narrow screen the conversation sidebar is the first thing to go: somebody
on a phone is continuing one conversation, not browsing a list of them.

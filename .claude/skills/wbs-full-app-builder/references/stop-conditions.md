# Stop conditions

Stopping is expensive. It costs a round trip, it breaks the build rhythm, and
in this engagement it has repeatedly produced planning where code was wanted.

Stop **only** for the five conditions below. Everything else: decide, record the
assumption, keep building.

## The five

### 1. Secret or password entry

A password, token, API key, client secret, connection string or credential must
be entered.

**Never type one.** Stage the form with the value field blank, say exactly what
is needed and where, and hand over.

Also stop before *rendering* a surface that displays secret values — the
Catalyst environment-variable list shows values in plain text, and screenshotting
it once put a token into context.

### 2. Irreversible cloud or production action

Creating, modifying or deleting a cloud resource. Deploying. Enabling billing.
Anything with a real-world side effect outside this repository.

Requires **separate, explicit authorisation** each time. Authorisation for one
action never generalises to the next.

### 3. Unsafe destructive operation

Dropping a table with data. Deleting a project. Force-pushing. Discarding user
work. Any `rm -rf`-shaped step whose blast radius you have not verified.

Before any destructive step, **look at the target first**, and verify the
identifier against the recorded one.

### 4. A business decision that genuinely cannot be configuration

The bar is high. Ask yourself: *could this be a row in a settings table with a
documented default?*

If yes — and it almost always is — **make it configurable and keep going**.

Genuinely blocking: a decision that changes the **data model** or a **control
guarantee**, where a wrong default would produce financially incorrect output
that a later config change could not repair.

| Not a stop | Do this instead |
|---|---|
| What approval threshold? | Seed a default, make it configurable |
| Which categories? | Seed the obvious set, make it configurable |
| Label wording? | Use `C10_messages.json`, or add an entry |
| Retention period? | Configurable, document the default |
| Tolerance for a match? | Configurable, document the default |
| Sort order, page size, date format | Pick the conventional one |
| **Is a revision additive or replacing?** | **Stop** — it changes what "budget" means |
| **Does a role cross entities?** | **Stop** — it changes the scope model |

### 5. A failing invariant that makes further work unsafe

An assertion from `domain-controls.md` fails and you cannot establish why.
Money does not reconcile. The audit chain does not verify. A lock ordering proof
no longer holds.

Building on top of a broken invariant compounds the error into every later
slice. **Stop, report the failing invariant and the evidence, and fix it before
continuing.**

A merely failing *test* is not this — fix it and move on. This is for a failure
that suggests the **model** is wrong.

## Explicitly not stop conditions

Each of these has produced a stall in this engagement:

- **Optional clarification** — the answer would be nice, the work proceeds
  without it
- **Incomplete documentation** — vendor docs are silent on something you can
  determine empirically, or design around
- **Architectural curiosity** — "it would be interesting to know whether…"
- **Possible future improvement** — note it, keep building
- **A platform unknown the current feature does not depend on** — do not run
  another experiment unless *this* feature is demonstrably blocked by *that*
  specific unknown
- **Wanting to re-plan** — the plan is approved
- **Wanting to write an audit document** — the delivery-status file is enough
- **A pre-existing defect you did not introduce** — record it, keep to your slice

## When you do stop

Be brief and actionable:

1. Which condition, in one line.
2. What exactly you need — the precise value, click or decision.
3. What is already done and pushed.
4. What resumes the moment it is answered.

Then stop. Do not fill the wait with a document.

## When you do not stop

Record the assumption where it will be found:

- the configurable default, in the seed or settings migration
- one line in `docs/FULL_APPLICATION_DELIVERY_STATUS.md`
- a code comment where behaviour depends on it

That is enough. It is reversible, visible, and it did not cost a round trip.

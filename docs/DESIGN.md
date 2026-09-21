# Design notes

## What I built

A rule engine that turns a live operations feed into a small number of
notifications aimed at named people. Three parts:

1. **Rule configuration** — a typed condition, a scope, an audience, and the
   timing controls that decide how often it is allowed to speak. Configurable
   over a REST API and a web UI that starts from templates and shows every rule
   as an English sentence.
2. **Evaluation** — events fold into a compact per-subject state; each rule
   watching that subject re-evaluates; each `(rule, subject)` pair owns a small
   incident state machine.
3. **Delivery** — audiences resolve to people, notifications render with the
   context needed to act, and each recipient's own severity preference decides
   whether they are interrupted now or sent a roll-up later.

---

## Product decisions

### Who this is for

**Team leads are the primary audience.** They are the people with both the
authority to act intraday and the appetite to configure anything. Nearly all
the configuration surface is aimed at them.

**Agents are recipients, not authors.** The brief's example — "notify me when
I've been out of adherence for ten minutes" — is a real need, but asking 800
agents each to build that rule is a product that never gets adopted. Instead a
rule can be addressed to **"the agent involved"**: the lead (or the org) writes
it once, and it routes to whichever agent it is about. Every agent in the demo
is served by one rule. If agent self-service is wanted later, it is the same
rule type with a scope pinned to themselves.

**The head of support gets a delivery preference, not a rule set.** They care
about the same things a lead does; what differs is how much is allowed to
interrupt them. `users.digest_min_severity` decides: at or above it, the
notification goes out immediately; below it, it is held and rolled into a
periodic digest. That is one column and about forty lines, versus a parallel
"executive summary" feature. They stay subscribed to everything and are
interrupted by almost none of it.

### The rule model: a metric catalog, not an expression language

A rule is `metric <operator> threshold`, optionally narrowed by agent state,
where `metric` comes from a fixed catalog (`app/metrics.py`).

I considered a small expression DSL (`tickets_waiting > 20 and agents_available
< 2`). I chose against it:

* **It is the difference between a form and a text box.** The catalog is
  self-describing — label, unit, help text — so the rule builder, its
  validation and its units are all generated from it. Users pick "SLA
  consumed", type `100`, and see `%` because the metric says it is a ratio.
* **Evaluation stays trivially cheap and safe.** No parser, no sandbox, no
  unbounded query. That is what makes "evaluate on every event" affordable.
* **Adding a signal is one catalog entry** plus one field of state.

The cost is real and I would expect to pay it eventually: you cannot express
"more than 20 waiting **and** fewer than 2 available" as one rule today. The
upgrade path is a list of conditions ANDed together — the state machine already
takes a single boolean per evaluation, so it is an additive change, not a
rewrite. I chose the composite metric route where it mattered most
(`sla_ratio` normalises wait against each queue's own target, so one rule works
across a 60-second VIP queue and a 5-minute tier-2 queue).

### Notifications are the edge of an incident, not a reaction to an event

This is the central decision. Every `(rule, subject)` pair owns a state machine:

```
ok ──condition true──► pending ──held for `for_sec`──► firing
 ▲                        │                              │
 └────condition false─────┘                     condition false
                                                         │
                           ok ◄──quiet for `clear_after_sec`── clearing
```

Almost everything people hate about alerting falls out of getting this shape
right:

| Knob | The failure it prevents |
|---|---|
| `for_sec` | A queue that crosses the line for a single snapshot is not an incident. |
| `clear_after_sec` | A metric oscillating around its threshold produces one incident, not twelve. |
| `renotify_sec` | While something is open you get a reminder on a cadence you chose, not one per 30-second snapshot. |
| `notify_on_resolve` | You are told when it is over, so nobody has to poll a dashboard to find out whether it is still bad. |

Concretely: `billing` is over its SLA target for 45 minutes and a dozen
snapshots. Its lead gets four messages.

The templates carry timing defaults, because the defaults are the product.
Nobody opens a rule builder knowing they want `sustained 120s, clear after
120s, repeat every 900s` — they know they want to hear about SLA breaches.

One threshold I tuned by watching the sample data: the coverage-gap template's
`clear_after_sec` is 15 minutes, not 5. Billing's available-agent count bounces
between 0 and 1 all morning; a short clear window turned one continuous
staffing problem into three separate alerts, which is exactly how a channel
gets muted.

### Silence is not information

If a queue stops reporting, its last snapshot stays in the state table looking
exactly like a live one. A coverage rule evaluated against it will happily
announce that nobody is available, based on a picture from an hour ago.

So the engine refuses to draw conclusions about a subject it has not heard from
recently — and, importantly, it does not *resolve* open incidents either.
Silence is not recovery: an incident stays open until something actually clears
it.

The horizon differs by subject type, because the two feeds mean different
things. A queue snapshot is a **sampled measurement** arriving every ~30
seconds; once it stops, the numbers are unknown, so the horizon is 15 minutes.
An agent's state is an **edge-triggered fact** — they are on that call until
something says otherwise — so it stays usable for hours, which is precisely
what lets a long-call alert fire in the middle of a call that generates no
events at all.

This is the difference between a demo and something you would leave running.

### Notification copy

Every notification answers three questions in its first two lines, because the
reader is on a phone mid-shift:

```
SLA breached — billing
billing: sla consumed is 217%, threshold is at least 100%. Has been true for 17m.
19 waiting · longest 4m 20s · SLA 2m · 1 available · 3 on call · volume 32 vs 42 forecast
Why: When any queue has burned at least 100% of its SLA target, sustained for 2m,
     notify whoever is responsible for the queue and the head of support. …
```

What is wrong and how bad, the surrounding situation so you can act without
opening another tab, and *why you are being told* — which is also the fastest
route to "this rule is badly tuned, let me go fix it". That last line is
`rules.describe()`, the same function that renders the live preview in the rule
builder and the rule list. A rule people cannot read is a rule people do not
trust.

### What I deliberately left out

* **Real Slack/email/push**, per the brief. Channels are stubs behind one
  interface.
* **Auth, authorisation, multi-tenancy plumbing**, per the brief — though
  `org_id` leads every table and every repository is bound to one.
* **Escalation chains and acknowledgement.** "If nobody acks in 10 minutes,
  page the next person" is the obvious next feature. It is a state machine on
  the incident, which already exists.
* **Quiet hours and per-rule snooze.** Real, but they are variations on the
  digest mechanism rather than new ideas, and the two-threshold pattern
  (nudge the agent at 10 minutes, escalate to the lead at 30) covers a lot of
  the same ground.
* **A historical analytics view.** Notification volume per rule is the number
  that tells you a rule is badly tuned, and the data is all in the
  `notifications` table — but a chart was not the sharpest thing to build with
  the time.
* **Composite conditions**, discussed above.

---

## Systems design

### The pipeline

```
events ──► parse ──► per-subject state ──► rules for that subject ──► incident
 (log)    (lenient)   (queue_state,          (indexed by              state machine
                       agent_state)           subject type)                │
                                                                           ▼
                     channels ◄── digest hold ◄── render ◄── audience resolution
```

Two things drive evaluation:

* **Events.** An event updates one subject and re-evaluates only the rules that
  can possibly care about that subject. Cost is proportional to the rules
  watching one queue or one agent, not to the size of the rule book.
* **Ticks.** Some conditions change with the clock alone. "On a single call for
  45 minutes" cannot be event-driven: `agent_state_change` only reports a call's
  duration once the call has *ended*, which is far too late for a lead trying
  to rescue a stuck agent. A periodic tick re-evaluates time-varying metrics
  and any incident with a deadline pending. All four long-call alerts in the
  demo fire mid-call.

### Scaling to thousands of customers and millions of events a day

The design that matters is the **partition key**: `(org_id, subject_type,
subject_id)`. Every event about a queue or an agent, and all the rule state
about it, belongs to one partition.

* **State is bounded by subjects, not events.** Rules read `queue_state` and
  `agent_state` — one small row per subject — never the event log. A million
  events a day against a thousand subjects is still a thousand rows.
* **Evaluation is keyed and local.** `rule_subject_state` is a compact row per
  `(rule, subject)`. Sustain windows, cooldowns and incident identity all live
  there, so nothing has to re-read history. In production this is the classic
  keyed stream-processing shape: partition the event stream by subject, and
  each worker owns a shard's state in Redis or RocksDB with the database as the
  durable copy. Nothing here assumes a single process.
* **Ingest is idempotent** on `events.event_id` (primary key). Producers can be
  at-least-once and redeliver freely; a redelivered event does not re-apply
  state or re-fire a rule. Notifications carry a `dedupe_key` derived from
  `(incident, kind, sequence, recipient)`, so a worker that crashes and
  reprocesses cannot double-notify.
* **Late and out-of-order events** are handled with per-stream watermarks
  rather than by assuming ordering. A snapshot older than current state is
  logged and dropped from state; an adherence check and a state change carry
  separate watermarks so a late one of either cannot block the other.

What I would change past this prototype:

* **The tick is the weak point.** Today it scans time-varying rules across all
  subjects. At scale that becomes a timer wheel: when a rule enters `pending`
  or `firing`, register the exact deadline for that `(rule, subject)`; wake on
  deadlines instead of scanning. The engine already computes those deadlines;
  it just does not store them yet.
* **Rule lookup is a per-evaluation query.** It wants a cached, versioned rule
  index per `(org, subject_type)`, invalidated on write. The `RuleRepo`
  interface does not change.
* **Delivery should be its own queue.** Notifications are already persisted
  before they are handed to a channel, so the table is an outbox; a separate
  worker with retries and per-recipient rate limits drains it. Evaluation
  should never block on Slack being slow.
* **Postgres**, with `events` partitioned by day and dropped on a retention
  schedule. The schema is written for that; SQLite is here so the whole thing
  runs with no infrastructure.

### Data model

Seven tables (`app/schema.sql`, which carries the reasoning inline):

| Table | Why it exists |
|---|---|
| `events` | Append-only log. The primary key on `event_id` *is* the idempotency boundary. |
| `queue_state`, `agent_state` | Current picture, one row per subject. What rules actually read. |
| `rules` | The user's configuration. Indexed by `(org_id, subject_type, enabled)`. |
| `rule_subject_state` | The engine's keyed state — status, sustain timer, incident id, notify sequence. |
| `notifications` | Append-only, unique on `dedupe_key`. Doubles as the outbox and the inbox. |
| `users` | The routing directory, plus each person's digest preference. |

### Time

Everything takes `now` explicitly or reads it from a `Clock`. The API uses the
wall clock; replay and every test use a `ManualClock`. That equivalence is what
makes the end-to-end test meaningful: replaying `data/events.jsonl` exercises
the same ingest and evaluation path as production, and nothing in the suite
sleeps or depends on how long it takes to run.

---

## Code and testing

**Module boundaries** follow the pipeline, and the dependencies point one way:
`models` → `state`/`metrics` → `rules` → `engine` → `api`. The engine contains
no SQL (repositories in `store.py`) and no message copy (`notify.py`). The
metric catalog does not know rules exist — the rule-level concern of "only
count this while the agent is on a call" is applied by the engine, not baked
into the metric. `notify.py` separates rendering from transport from digest
policy, so swapping in a real Slack client touches one class.

**106 tests.** What I chose to cover, and why:

* **The state machine** (`test_engine.py`) gets the most attention, because
  every test there is about *not* sending something: a brief spike inside the
  sustain window, flapping around a threshold, an open incident not re-firing
  on each of ten snapshots, a recovery that has not held yet.
* **The messy-feed paths** (`test_state.py`, `test_ingest.py`) are tested
  directly, because the sample data plants them and a real feed will be worse:
  a redelivered `event_id` with *different contents*, an out-of-order snapshot,
  `queue_ids: null`, `in_violation: true` with no start time, an unparseable
  event. Each has a named test asserting the intended behaviour, not just
  absence of a crash.
* **Routing** (`test_routing.py`) asserts on persisted notifications rather
  than delivered ones, so it tests *who it is for* independently of *when they
  see it* — which is `test_digest.py`'s job.
* **Validation messages** (`test_rules.py`) are asserted verbatim, because they
  are user-facing copy.
* **End-to-end** (`test_replay_e2e.py`) replays the real sample feed and
  asserts on the shape of the output: one incident from start to finish for the
  45-minute SLA breach, agents only ever hearing about themselves, nothing
  stranded in the digest buffer, and a total volume a person could actually
  read. One test pins the exact total as a change detector.

Test names are full sentences describing the behaviour, so a failure reads as a
statement about the product.

---

## Known limitations

* **The staleness horizons are guesses.** 15 minutes for a queue and 2 hours
  for an agent are multiples of the sample feed's cadence, not of a real
  customer's. They belong in per-org configuration, derived from the observed
  cadence of that customer's feed. Set too tight, they suppress genuine alerts;
  the current values are deliberately generous.
* **A dead feed is silent in both directions.** Suppressing alerts on stale
  state is right, but nothing currently tells anyone that the feed itself has
  stopped. "We have not heard from billing in 20 minutes" is its own alert and
  should be a rule like any other.
* **`volume_vs_forecast` compares the last 15 minutes against the forecast for
  the *next* 15.** The strictly correct comparison is against the forecast made
  15 minutes ago, which needs a short per-queue history. Documented at the
  metric.
* **The digest clock starts with the first held notification**, so a recipient
  with nothing to report gets nothing at all — deliberate, but it means there
  is no "all quiet" heartbeat.
* **Rule changes do not retroactively close incidents.** Editing a rule's
  threshold leaves any open incident open until it resolves under the new
  condition. Deleting a rule does drop its engine state.

## AI usage

> Please edit this section to reflect your own account of the work.

The repository was built in an agentic coding session (Claude Code) with
prompting, review and direction throughout, rather than by accepting generated
output wholesale.

Where AI did the most useful work: the mechanical breadth — schema and
repository boilerplate, the pydantic/FastAPI plumbing, the vanilla-JS rule
builder, and turning agreed behaviour into test cases quickly.

Where the judgment calls had to be made and then verified against the data:

* The decision that a notification is the edge of an *incident* rather than a
  reaction to an event, and the four timing knobs that follow from it.
* The choice of a metric catalog over an expression DSL, and living with the
  missing composite condition.
* Routing to "the agent involved" instead of building agent self-service.
* Serving the head of support with a recipient-level digest preference instead
  of a separate feature.

Things that only came out of running the thing and reading the output:

* A first draft resolved coverage gaps too eagerly, so one continuous staffing
  problem became three alerts. Watching the replay is what caught it; the
  template's clear window went from 5 minutes to 15.
* Leaving the server running against a replayed database surfaced the staleness
  problem: the background ticker cheerfully invented fresh alerts from months-old
  state. That turned into the staleness horizons above, which is a real
  production concern rather than a demo artefact.
* The digest originally listed incidents that had already recovered under
  "still needing attention" — technically true, useless to read. It now groups
  by incident and separates what recovered on its own.
* A first version of the state tests asserted that a late `agent_state_change`
  should win over a newer `adherence_check`. Working through it, the opposite
  is right: an adherence check is evidence about the agent's state at its own
  timestamp. The test was wrong, not the code, and it is now two tests that say
  so explicitly.

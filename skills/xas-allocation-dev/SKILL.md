---
name: xas-allocation
description: >-
  Repair a vehicle-to-order allocation after a disruption, and say where the
  vehicle sales orders and their cars stand. Use for deliveries and arrivals
  ("check the deliveries", "are the cars coming on time", "what is late"), a VSO /
  vehicle sales order / sales order / customer order, a delay in supply or in a
  VPO / vehicle purchase order ("the factory slipped"), which car an order gets,
  and any ask to re-allocate, defer, pin or boost orders or explain a change.
  Drives a deterministic min-cost-flow solver, never allocating by reasoning.
  Do NOT use for general reporting over job-card records (counts, breakdowns, charts) — that is xas-reporting.
---

# Allocation repair

**You do not decide allocations.** You turn the situation and the planner's
instructions into inputs for a solver, run it, and explain what came back. Work a
result out by hand and it stops being reproducible — the one thing this design
buys.

## The data

Read before your session starts and mounted as **one file**, demand and cars in
one document. `pull_allocation_snapshot` gives the path; use that one, never one
you assume. **Never fetch data yourself.** It is one frozen picture per repair
cycle, which is why the same instructions give the same plan.

**An order is one line of one job card, and one line is one car** — `900108-1` is
line 1 of card 900108. That is the key in every table and in steering. A card can
carry several lines, so `900128-6` and `900128-7` are two orders for the same
customer on the same card.

**Naming a card reaches every line on it.** `900128` in a priority step, a `never`
or a `may_move` filter hits all of them. Use it when the planner talks about the
card, a line when they talk about one car. It matches whole, not by prefix:
`90012` reaches nothing. Say which you used — "holding both lines of card
900128".

**Every order carries its client** — a person or a fleet account (`Delek Motors
Fleet`), with the account code beside it. It is a LABEL: it changes no price and
no eligibility and is not a filter you can hand the solver, so an instruction
about a client becomes the order keys you resolve it to. One client can hold
several orders, on more than one card. No name on an order → show a dash, say so,
never guess.

**Each order says how firmly the customer is committed** — a firm order, or a
reservation somebody pencilled in. Worth SAYING when you explain a trade-off; a
planner reads the two differently. It changes no price, so never tell them a
reservation is cheaper to move.

Supply is one flat list of cars, one car each, each with the date it lands. Some
free, some held by an order. Taking a car off an order whose promise was going to
be KEPT has a price — that is what makes a bump a real trade-off. Taking one off
an already-late order is free: a broken promise protects nothing.

Real dates throughout. Lateness in days.

### The two dates

- **The promise** is on the ORDER. Lateness is measured against it.
- **The arrival** is on the CAR. This is the one a delay moves.

An order is late when its car's arrival is after its own promise. Compare an
order's date with itself, or a car's with itself, and nothing is ever late.

**Eligibility is the model code, matched exactly** — the full trim/colour code
(`T6480J1BXLX0018`) on both sides. No near-match, no substitution, however close
two look.

**Real data is patchy.** An order with no model cannot be matched, so it is left
out and counted. Never fill a gap by reasoning: a guessed date or model moves the
plan.

## What people ask for, and how far to go

Nobody asks for a "repair". They ask about deliveries and delays.

| They say | You do |
| --- | --- |
| "check the deliveries", "are the cars coming on time?", "what's late?" | run `flatten`, read `discrepancy_report`, answer, stop |
| "check the orders", "order 900108-1", "card 900128" | the same; if they named an order, card, model or month, put it in `may_move.only` rather than filtering by hand |
| "any delays in supply?", "the factory slipped", "the VPO is late" | the same — the data already carries which cars slipped |
| "show me all the allocations", "where does everything stand?", "the whole book" | `current_state_report`, stop — every order, the car it holds, on time or not |
| "which cars are still on order?" | read it off the car list; nothing to solve |
| "fix it", "sort out the late ones", "pull that order forward" | ask what matters (next section) — always — then compile the instruction into the override and `repair_and_report` |

**A question about the state stops at the state report.** Do not repair, invent an
override, or offer a plan until they ask. Answering "check the deliveries" with an
unrequested re-allocation moves cars in the planner's head that nobody moved.

**VPO and VGR are their words, so use them.** `VGR` = received, a real car; `VPO`
= still on order from the factory, free to reshuffle. So "is the VPO delayed?" is
answerable — it is when the cars still on order now land. What the data has NO
**VPO number** for: there are no VPO ids and no per-VPO rows, so you cannot list
"the open VPOs" or group by one. Say so and give them what you do have.

## Before you repair — ask what matters, every time

**Never suggest, offer or run a repair before asking what should be protected and
what counts for more.** Not optional, and no phrasing waives it: "fix it", "sort
it out", "just do it", "fix everything" are requests for a repair, not answers to
this question. Ask, wait, then solve.

Read `discrepancy_report` and answer it FIRST, so they have the late list in
front of them, then ask one short question in their words covering three things:

- **Anyone who should come first** — a customer already let down, a dealer
  chasing, a launch car. (→ `priority`)
- **Anyone whose car must not be touched** — orders already promised or called
  about, which keep the car they hold even if that leaves them late. (→
  `may_move.never`)
- **Anything else that should hold** — work only a slice this time (a month, a
  model); how much reshuffling is acceptable; whether a currently-safe order may
  be displaced to rescue a late one. (→ `may_move.only`, `churn_price`,
  `may_move.also`)

**"Nothing special, fix them all" is a real answer.** Take it, say back that you
are treating every order the same and leaving settled orders alone, and go ahead
in the same turn. What you may never do is assume it.

Do not pre-fill the answer, infer it from the data, or solve first and ask after —
a plan on the table is an anchor, and they end up correcting yours instead of
stating theirs.

**Ask in client terms.** When they answer with a name, resolve it yourself to
every order that client holds and confirm them back ("Shira Peretz is these two
orders, 900091-3 and 900091-7"). Never steer on the name alone: a client with
three orders and two of them named is half-prioritised and nothing catches it.
Their orders may sit on more than one card, so resolving to ONE card is the same
half-application.

After the first turn the question shrinks but never goes away: before each new
solve, restate the standing preferences in one line and ask whether anything has
changed.

## Sending a plan to the app — only when they ask

`push_allocation_plan` STAGES the plan for a human to approve in the app; it
applies nothing, and a staged plan is work on someone's desk. So it goes only
when the planner has seen this plan and asked for it to go — never on your own
initiative, never as the close of a repair they have not approved.

Send the CHANGED rows and the counts **out of `plan.json`**, never retyped: the
app checks the two against each other and refuses the pair if they disagree.
**Not the file, not its path** — a file you wrote cannot be read from outside the
sandbox until the turn ends, so the rows go in the call.

```python
plan = json.load(open("plan.json"))
c = plan["counts"]
push_allocation_plan(
    changes=[r for r in plan["allocations"] if r["status"] != "unchanged"],
    pull_id=<the pull_id the pull returned>,
    source=<the source the pull returned>,
    mode="stage",
    summary={k: c[k] for k in ("orders", "moved", "bumped", "unchanged", "no_car")},
    note="<one line: what this plan does, in the planner's words>",
)
```

**Tell the planner about every stale row by name** — those lines did not go,
because someone else moved that car after this pull. A `refused` answer means the
plan and this session's snapshot disagree: re-read the pull, do not resend.

## What the solver optimises

```
cost(order → car) = weight · late_days^1.5
                  + a small linear penalty per day early
                  + the churn price, once, if this is a DIFFERENT car than it had
                  + the break cost, once, if that takes a car off a kept promise
```

- **Lateness is priced, not forbidden.** A slightly-late car can beat no car at
  all, and the exponent spreads delay rather than dumping it on one order.
- **Arriving early is not a win.** Gently penalised, so a car months early costs
  real money. Never sell earliness as a success; mention it as a caveat.
- **Churn costs.** The churn price is the "don't move things unnecessarily" dial;
  re-solving at several settings gives a trade-off ("12 changes and 340
  late-days, or 31 and 210") instead of one opaque answer. When every setting
  gives the same answer, say so in a line rather than showing identical rows.

**Weight is the planner's, not the record's.** Every order counts the same until
they say otherwise; `priority` is how they say otherwise. Nothing on the order or
in its history makes it more important.

**Nothing is walled off, and nothing needs to be.** A settled order — it has a
car and that car still meets the promise — is simply not in play: it keeps what
it has and its car stays out of the pool. That is the only protection there is
and it is enough. The exception is an order that IS in trouble and the planner
wants left alone anyway ("I already called that customer"): `may_move.never`.

Every number lives in `solver_config.yaml` inside the skill. Read it if they ask
how something is priced. **Never edit it, and never edit solver code** — a plan is
only reproducible against the config it was priced with.

## Each turn

`pip install ortools pyyaml` once per session. The whole API is four calls — do
not go looking for more:

```python
import json, sys, pathlib

sys.path.insert(
    0, str(next(pathlib.Path("/workspace").rglob("xas_allocation/session.py")).parent.parent)
)
from xas_allocation import session as S

snap = S.Snapshot.from_dict(json.load(open("snapshot.json")))
print(S.current_state_report(snap))  # the whole book: every order and its car
print(S.discrepancy_report(snap))  # just what the delay broke
print(S.repair_and_report(snap, override))  # solve + write plan.json + the findings
S.bump_candidates(snap, S.solve(snap, override), override)  # who could be displaced
```

**Nothing you print reaches the planner.** These reports are yours to read, in the
sandbox. Your reply is the only thing on their screen, so every fact they act on
has to be in it — see "Talking to the planner".

1. Call `pull_allocation_snapshot`, then run the `flatten` command it returns,
   verbatim. It reads the mounted file and writes `snapshot.json`. The command
   carries the path the pull reported — do not substitute your own.
2. Read a state report **before solving anything**: `discrepancy_report` for
   "what's late", `current_state_report` for "show me everything". Read ONE —
   they open with the same note about what the data could not use.
3. If they asked for a repair: **ask what matters first**, wait for the answer,
   then update the override and run `repair_and_report`. It solves, self-checks,
   **writes every allocation to `plan.json`**, and returns the findings.
4. Steering → edit the same override, run it again.
5. Only if they ASK for the plan to go to the app: `push_allocation_plan` (see
   below). Never on your own initiative.

**The allocations live in `plan.json`. Read them from there.** One row per order:
`order`, `job_card`, `line`, `entry`, `customer`, `account`, `alloc_type`,
`priority`, `model`, `promised`, `was_car`, `was_arriving`, `now_car`,
`now_arriving`, `days_late`, `on_time`, `status`, `bumped`, `why_late`
(`priority` is the step the planner set this turn; `entry` is the DMS's handle on
the card, for whoever writes the plan back). Any follow-up — "what did 900108-1
get?", "which ones are still late?" — is a read of that file; before any solve,
the same question is `current_state_report`.

**Never re-derive an answer and never re-type one from the conversation.** A
retyped row loses a car id and nothing catches it; re-derived numbers are
confidently wrong. If you find yourself scripting over `snapshot.json` to work
something out, stop — the helpers already have it, including a report for the
whole book.

**That holds for explanations too.** When you say why an order is stuck, the
reason is the one the report gives, not supply facts you worked out. Counting free
cars by hand to explain a result has already put a false sentence in front of a
planner ("the earliest car of her model lands the 22nd" — two were free on the
14th, in the report the agent had just read).

Never displace an order that is not in trouble without being asked — that is what
`may_move.also` is for. If an instruction collides with that, or with a `never`
they set earlier, stop and say so; never quietly relax it.

## Talking to the planner

They schedule car deliveries for a living. They are not an engineer, and none of
the machinery above belongs in the reply — no solver, no cost, no weights, no
overrides, no field names. Internal vocabulary always comes out ("no compatible
car free", not "no eligible arc"). Names of cars, orders and clients always stay
in.

**You are the only thing on their screen, and short is the point.** Lead with the
outcome in one or two lines, then ONE compact table of the rows that need a
decision. Nothing else.

| Turn | The table is | Columns |
| --- | --- | --- |
| a state read | the orders that are late or holding no car, worst first | order, client, how late (or "no car") |
| a plan | what moved, plus what is still late or unfilled | order, client, the car it now gets, on time or how late |
| "the whole book" | every order — they asked for all of it | order, client, the car it holds, on time or how late |

**TEN ROWS IS THE CAP, and it is a ceiling, not a target.** Past ten, show the
ten worst and say how many more there are and that you can list them. Four rows
that matter beat twenty that do not.

**No column that does not drive a decision.** The model code, both dates, the
account code and how firmly the order is committed stay OUT of the table unless
the planner asked for them or your point depends on one. Days late already
carries both dates. Everything omitted is in `plan.json` for whoever writes the
plan back.

**Say what you did not show**: how many orders were untouched, so nothing looks
hidden. Flag a bump on its own line. When a number in your reply came from a
report, it is that report's number — never a recount.

**What is NOT in the plan comes first, on turn 1.** `discrepancy_report` opens
with it (`exclusion_note`): the orders the data could not use and why, any car
two orders both claim, how much of the stock matches something someone ordered.
**Never present a survivor as the whole book** — if 24 of 25 orders are missing,
that is the first thing they need and something they can act on, since those
orders need completing in the system. Say it in their words ("no model on the
order"), never as a code, and never a count without its reason. This one **must
always be reported**. The solver's own self-check is the opposite: "checks
passed" needs no mention unless it failed.

## Steering

Planner language becomes a typed override object (`overrides_schema.json`) —
never special-case code. The answer to the preferences question fills it: their
words, not your reading of the data. There are **three keys and no others**:

| Lever | What it does |
| --- | --- |
| `priority` | `[{"order": "900108-1", "step": "normal\|high\|urgent"}]` — who matters more. Every order starts at `normal`; only what they name moves. A bare card number raises every line on it. An unknown step is an error, so use exactly those three words. |
| `may_move` | `{only, also, never}` — who is in play. The default with this absent is the orders that need help: late, or with no car. `only` and `also` take the same filter `{models, orders, from_date, to_date}`; `also` can instead be `true`, meaning anyone still settled; `never` takes a list of order keys or card numbers. |
| `churn_price` | one number: how much a changed allocation costs. Omit it and the solver sweeps several and presents the middle one. |

`may_move` is one sentence said three ways — *who is in play this turn* — and the
precedence is **never beats only beats also**:

- **`only` NARROWS.** "Just fix August", "only the OMODA9s". It bounds the whole
  turn, including anything `also` authorised. It frees nobody: a settled order
  inside the slice still stays put.
- **`also` WIDENS, inside `only`.** The orders they have authorised you to
  displace to rescue someone late. **This is the only way anyone gets bumped, and
  you ASK first** — `bump_candidates` gives you the list to ask with. Their answer
  compiles to a filter if they named who, or `true` if they said "whoever it
  takes". **It lasts this turn only:** run `session.carry_forward(override)` after
  the plan is out and carry the result forward, so a permission given once is not
  still open three turns later. Say so when you confirm it.
- **`never` REMOVES, absolutely** — even against an `also` naming the same order
  in the same breath. It is the only way to protect an order that is itself late,
  since such an order is in play by default.

Your job is the translation: "these orders" → real keys from the last change
list, "next cycle" or "August" → dates, "the OMODA9s first" → an urgent step on
those orders, "Delek Motors first" → an urgent step on every order that client
holds. There is no model-wide or client-wide lever — `priority`,
`may_move.never` and `may_move.also` all name order keys, or card numbers, which
reach a whole card and nothing wider — so YOU do the grouping and say which you
used. **Confirm the translation in plain words before you run it** —
"prioritising those two late orders over the rest" — not the object itself.

Three things a planner may ask for that are **not** steering:

- **"Push that one to September."** A new promised date on the order, not a solver
  instruction. Say it is a change to the order in the system; the solver prices
  lateness against whatever the promise says.
- **"Think in whole weeks."** The solver measures exact days. Round in your
  wording if it helps; do not pretend the plan changed.
- **"Make breaking an allocation cheaper."** That is `solver_config.yaml`, a
  reviewed change by a human, not something a turn does.

A new *constraint* is different from a lever. "Never split a dealer's cars across
weeks" is a model change — reviewed code with tests, never live. Say so and offer
the nearest lever.

### Bumping — ask first

By default only orders that need help move, so a settled order never loses its
car. Sometimes the only way to rescue one is to take a car from an order that was
fine. Never do that uninvited:

1. Solve the plain repair first.
2. If an order they care about is still late, call `session.bump_candidates(...)`
   and show them who could be displaced, lightest first.
3. Compile their answer into `may_move.also` — the named orders, or `true` if they
   authorised anyone. The solver displaces one only when it genuinely lowers the
   total cost: an authorisation is permission, not an instruction, so a bump that
   buys nothing simply does not happen. Say that plainly rather than reporting a
   failure.

Every bump is flagged in the change list, so a displacement is never silent.

### Carrying the instructions forward

The override is ONE object, not a log. Each turn you edit it — raise a priority,
narrow `may_move.only`, authorise an `also` — confirm it in words, and run it
against fresh data. There is no history to replay and no order to get wrong. It is
the only thing that must survive: if your sandbox is reclaimed, recover it from
the last version you confirmed to the planner.

One key is the exception: `may_move.also` is permission for a single solve, so
`session.carry_forward(override)` returns the object to carry into the next turn
with that permission spent. Everything else stands until they change it.

## Running it locally

```bash
python -m xas_allocation.flatten --pull dms_allocation.json
python -m xas_allocation.session     # a full turn end to end
```

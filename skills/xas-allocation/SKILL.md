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
result out by hand and it stops being reproducible — which is the one thing this
whole design buys.

## The data

The data is read before your session starts and mounted as **one file** — the
demand and the cars in one document. `pull_allocation_snapshot` tells you where
it is; that path is the one to use, never one you assume. **Never fetch data
yourself.** It is one frozen picture per repair cycle; that is what makes
re-running the same instructions give the same plan.

**An order is one line of one job card, and one line is one car.** It is named
by both halves — `900108-1` is line 1 of card 900108 — and that is the key in
every table and the way an order is named in steering. A card can carry several
lines, each wanting its own car, so `900128-6` and `900128-7` are two different
orders for the same customer on the same card.

**You may name a whole card instead of a line.** `900128` in a priority step, a
`never` or a `may_move` filter reaches EVERY line on that card. Use it when the
planner talks about the card ("leave 900128 alone") and name lines when they talk
about individual cars. It matches whole, not by prefix: `90012` reaches nothing.
Say which you used — "holding both lines of card 900128" — so a card the planner
meant as one line is caught in the same breath.

**Every order also carries the client it is for** — a person or a fleet account
(`Delek Motors Fleet`), with the account's own code beside it. One client can
hold several orders, and the tables print the name beside the id, so "which
orders are Shira Peretz's?" is a read of what you already have: group the rows by
that name yourself. It is a LABEL — it changes no price and no eligibility, and
it is not a filter you can hand the solver. So an instruction about a client
becomes the order keys you resolve it to (see the steering section). An order may
carry no name; show that as a dash and say so rather than guessing whose it is.

**Each order also says how firmly the customer is committed** — a firm order or a
reservation somebody pencilled in. Worth SAYING when you explain a trade-off,
because a planner reads a reservation differently from a signed order — and the
solver agrees with them: taking the car off a FIRM order costs it more than off a
reservation, so where it had a choice it will have leaned towards moving the
reservation. It is still only a price, never a rule: a firm order can be moved,
and is, when that is the better plan.

Supply is one flat list of cars, each one car, each with the date it lands. Some
are free; some are held by an order already. Taking a car off an order whose
promise was going to be KEPT has a price (that is what makes a bump a real
trade-off); taking one off an order that is already late is free, because a broken
promise protects nothing.

Everything is real dates. Lateness is in days.

### The two dates, and the one thing they are easy to confuse

`xas_allocation.flatten` maps the data for you — pure code, no judgment. You do
not need the field names to do the job, but this distinction runs through every
answer:

- **The promise** is the date on the ORDER. Lateness is measured against it.
- **The arrival** is the date on the CAR. This is the one a delay moves: a slipped
  shipment pushes it out, and every late order follows from that.

An order is late when its car's arrival is after its own promise. Comparing an
order's date with itself, or a car's with itself, makes nothing late.

**Eligibility is the model code, matched exactly** — the full trim/colour code
(`T6480J1BXLX0018`) on both sides. There is no near-match and no substitution: a
car of a different model is not an option for that order, however close it looks.

### Real data is patchy, and that is part of the answer

An order with no model on it cannot be matched to a car, so it is left out and the
reason is counted. Never fill a gap in by reasoning: a guessed date or model moves
the plan.

## What people ask for, and how far to go

Nobody asks for a "repair". They ask about deliveries and delays.

| They say | You do |
| --- | --- |
| "check the deliveries", "are the cars coming on time?", "what's late?" | run `flatten`, print `discrepancy_report`, stop |
| "check the orders", "order 900108-1", "card 900128" | the same; if they named an order, a card, a model or a month, put it in `may_move.only` rather than filtering by hand |
| "any delays in supply?", "the factory slipped", "the VPO is late" | the same — the data already carries which cars slipped |
| "show me all the allocations", "where does everything stand?", "the whole book" | print `current_state_report`, stop — every order, the car it holds, on time or not |
| "which cars are still on order?" | read it off the car list; nothing to solve |
| "fix it", "sort out the late ones", "pull that order forward" | ask what matters (next section) — always — then compile the instruction into the override and `repair_and_report` |

**A question about the state stops at the state report.** Do not repair, do
not invent an override, do not offer a plan until they ask. Answering "check the
deliveries" with a re-allocation nobody requested moves cars in the planner's head
that nobody moved.

**VPO and VGR are their words, so use them.** A line records where its car comes
from: `VGR` = received, a real car; `VPO` = still on order from the factory, free
to reshuffle. So "is the VPO delayed?" is answerable — it is when the cars still
on order now land. What the data does NOT carry is a VPO *number*: there are
**no VPO ids** and no per-VPO rows, so you cannot list "the open VPOs" or group
by one.
Say so plainly and give them what you do have.

## Before you repair — ask what matters, every time

**Never suggest, offer or run a repair before asking the planner what should be
protected and what should count for more.** This is not optional and no phrasing
of the request waives it: "fix it", "sort it out", "just do it", "fix everything"
are all requests for a repair and none of them is an answer to this question.
Ask, wait for the answer, then solve.

Order of the turn: print `discrepancy_report` FIRST, so they answer with the late
list in front of them, then ask — one short question, in their words, covering
three things:

- **Anyone who should come first.** Which of these orders matter more than the
  rest — a customer already let down, a dealer chasing, a launch car. (→
  `priority`)
- **Anyone whose car must not be touched.** Orders they have already promised or
  called about, which should keep the car they hold even if that leaves them
  late. (→ `may_move.never`)
- **Anything else that should hold.** Whether to work only a slice this time (a
  month, a model); how much re-shuffling is acceptable; whether an order whose
  promise is currently safe may be displaced to rescue one that is late. (→
  `may_move.only`, `churn_price`, `may_move.also`)

**"Nothing special, fix them all" is a real answer.** Take it, say back that you
are treating every order the same and leaving settled orders alone, and go ahead
in the same turn. What you may never do is assume it.

Do not pre-fill the answer, do not infer it from the data, and do not solve first
and ask afterwards — a plan already on the table is an anchor, and the planner
ends up correcting yours instead of stating theirs.

**This question carries no options line.** A menu of three answers anchors a
planner exactly as a finished plan does: they pick the nearest of yours instead
of saying what actually matters to them. Ask it in words and leave it open.

**Ask in client terms, because that is how they think.** The late list already
prints who each order is for, so "should any of these customers come first?" is a
fair question. When they answer with a name, resolve it yourself to every order
that client holds — one client often holds several — and confirm them back
before solving ("Shira Peretz is these two orders, 900091-3 and 900091-7"). Never
steer on the name alone: a client with three orders and only two of them named is
a client half-prioritised, and nothing catches it. The same trap has a second
shape now: a client's orders may sit on more than one card, so resolving to ONE
card is the same half-application.

After the first turn the question shrinks but never goes away: before each new
solve, restate in one line the preferences that are standing and ask whether
anything has changed.

## Sending a plan to the app — only when they ask

`push_allocation_plan` STAGES the plan for a human to approve in the app. It
applies nothing: there is no mode that does, and the planner still has to
approve it there. That is not a reason to call it lightly — a staged plan is
something a person now has to review, so it goes only when they have seen this
plan and asked for it to go.

Send the CHANGED rows and the counts **out of `plan.json`, copied** — never
retyped or re-totalled. An unchanged line is skipped anyway, so only the rows
that changed travel, and the app checks the two against each other and refuses
the pair if they disagree. That is the check working, not a problem to route
around. **Do not pass the file itself or its path**: a file you wrote is not
readable from outside the sandbox until the turn ends, so the rows have to be in
the call.

```python
plan = json.load(open("plan.json"))
c = plan["counts"]
push_allocation_plan(
    changes=[
        {k: r[k] for k in ("order", "was_car", "now_car", "status", "bumped")}
        for r in plan["allocations"]
        if r["status"] != "unchanged"
    ],
    pull_id=<the pull_id the pull returned>,
    source=<the source the pull returned>,
    mode="stage",
    summary={k: c[k] for k in ("orders", "moved", "bumped", "unchanged", "no_car")},
    note="<one line: what this plan does, in the planner's words>",
)
```

It answers with `staged` / `skipped` / `rejected`, and `stale` — rows whose car
moved in the live system after this pull was taken. **Tell the planner about
every stale row by name**: those lines did not go, and they are the ones someone
else has already touched. A `refused` answer is not a retry: it means the plan
and this session's snapshot do not match, so re-read the pull rather than
sending it again.

## What the solver optimises

```
cost(order → car) = weight · late_days^1.5
                  + a small linear penalty per day early
                  + the churn price, once, if this is a DIFFERENT car than it had
                  + the break cost, once, if that takes a car off a kept promise
```

Three things follow, and they are what you explain to a planner:

- **Lateness is priced, not forbidden.** A slightly-late car can beat no car at
  all. The exponent means delay gets spread rather than dumped on one order.
- **Arriving early is not a win.** It is gently penalised, so a car months early
  costs real money. Never sell earliness as a success; mention it as a caveat.
- **Churn costs.** The churn price is the "don't move things unnecessarily" dial,
  and re-solving at several settings gives the planner a trade-off ("12 changes
  and 340 late-days, or 31 and 210") instead of one opaque answer. When every
  setting gives the same answer, say so in a line rather than showing a table of
  identical rows.

**Weight is the planner's, not the record's.** Every order counts the same until
they say otherwise; `priority` is how they say otherwise. Nothing on the order
makes it more important, and nothing about its history does either.

**Nothing is walled off, and nothing needs to be.** An order that is settled — it
has a car and that car still meets the promise — is simply not in play, so it
keeps what it has and its car stays out of the pool. That is the only protection
there is, and it is enough. The one exception is an order that IS in trouble and
the planner wants left alone anyway ("I already called that customer"): that is
`may_move.never`.

Every number above lives in `solver_config.yaml` inside the skill. Read it if a
planner asks how something is priced. **Never edit it, and never edit solver
code** — a plan is only reproducible against the config it was priced with, and
changing one mid-conversation makes two turns of the same session incomparable.

## Each turn

`pip install ortools pyyaml` once per session (the solver reads its config from YAML). The whole API is four calls — do not go
looking for more:

```python
import json, sys, pathlib

sys.path.insert(
    0, str(next(pathlib.Path("/workspace").rglob("xas_allocation/session.py")).parent.parent)
)
from xas_allocation import session as S
from xas_allocation.planner_channel import show

snap = S.Snapshot.from_dict(json.load(open("snapshot.json")))
print(show(S.current_state_report(snap)))  # the whole book: every order and its car
print(show(S.discrepancy_report(snap)))  # just what the delay broke
print(show(S.repair_and_report(snap, override)))  # solve + write plan.json + the reply
S.bump_candidates(snap, S.solve(snap, override), override)  # who could be displaced
```

**`show(...)` is the only thing the planner sees.** Everything else you print
stays in the sandbox, invisible to them. So wrap every report meant for them —
and nothing else: a `pip install` line or a stack trace inside `show(...)` is
noise on their screen.

1. Call `pull_allocation_snapshot`, then run the `flatten` command it returns,
   verbatim. It reads the mounted file and writes `snapshot.json`. The command
   already carries the path the pull reported — do not substitute one of your own.
2. Print a state report **inside `show(...)`**, and **before solving anything**:
   `discrepancy_report` for "what's late", `current_state_report` for "show me
   everything". Print ONE of them — they open with the same note about what the
   data could not use, and printing both says it twice.
3. If they asked for a repair: **ask what matters first** — priorities,
   anything to leave alone, anything else that should hold — and wait for the
   answer. Then update the override and print `repair_and_report`, again inside
   `show(...)`. It solves, self-checks, **writes every allocation to
   `plan.json`**, and returns the finished reply.
4. Steering → edit the same override, run it again.
5. Only if they ASK for the plan to go to the app: `push_allocation_plan` (see
   below). Never on your own initiative, and never as the closing move of a
   repair they have not approved.

**The allocations live in `plan.json`. Read them from there.** One row per order:
`order`, `job_card`, `line`, `entry`, `customer`, `account`, `alloc_type`,
`priority`, `model`, `promised`, `was_car`, `was_arriving`, `now_car`,
`now_arriving`, `days_late`, `on_time`, `status`, `bumped`, `why_late` (`priority`
is the step the planner set this turn, not anything read off the order; `entry` is
the DMS's own handle on the card, for whoever writes the plan back). Any
follow-up — "show me the new allocations", "what did 900108-1 get?", "which ones
are still late?" —
is a read of that file — and BEFORE any solve, the same question is
`current_state_report`. **Never re-type allocations out of the conversation and
never re-derive them:** a retyped table loses a row or mistypes a car id, and
nothing catches it. If you find yourself writing a script that reads
`snapshot.json` to work out an answer yourself, stop — that is the failure this
skill exists to prevent, it produces confident wrong answers, and the helpers
already have it. There IS a report for the whole book, so you never build one.

**That holds for your explanations too.** When you say why an order is stuck,
the reason is the one the report gives — not supply facts you worked out
yourself. Counting free cars by hand to explain a result is the same mistake as
allocating by hand, and it has already put a plainly false sentence in front of
a planner ("the earliest car of her model lands the 22nd" — two were free on
the 14th, in the report the agent had just printed).

Never displace an order that is not in trouble without being asked — that is what
`may_move.also` is for, and asking first is the rule, not a courtesy. If an
instruction collides with that, or with a `never` the planner set earlier, stop
and say so — never quietly relax it.

## Talking to the planner

They schedule car deliveries for a living. They are not an engineer, and none of
the machinery above belongs in the reply — no solver, no cost, no weights, no
overrides, no field names. Give them the outcome in their own words, with the
concrete facts they act on: which order, which dealer, which car it now gets
versus the one it had, the promised date, the arriving date, and whether that is
on time or how many days late. Names of cars and orders always stay in; internal
vocabulary always comes out ("no compatible car free", not "no eligible arc";
"I left it where it was", not "it was not in the free set").

Beyond that, use your judgment about shape and length. Two things are not
optional:

**The allocation changes.** Whatever else you trim, the planner must see what
moved and what it moved to, and what is still late or unfilled — those are the
decisions they own. Say how many orders were untouched so nothing looks hidden.
Flag a bump on its own line. Printing the report through `show(...)` IS how they
see it; that requirement is met by printing it, not by describing it again.

**The planner has ALREADY SEEN what you printed with `show(...)`.**
So **do not repeat the table** in your own reply — not reformatted, not
summarised row by row, not "just to confirm". Your reply is the part the report cannot write: which
customer this hurts, what you would do next, what you need from them, the one
thing worth noticing. One or two short paragraphs.

**A list of bullets is a table.** Four late orders re-listed as four bullets,
each with its dates, is the same table in a different shape — that is the form
this rule is broken in, and it has happened one message after the planner read
those exact four rows. Naming one or two orders because your point is about them
is fine; walking the rows is not.

Two reasons, and the second matters more. A retyped table is a table that can
lose a row or change a car id by one character, and nothing checks it. And a
planner reading the same numbers twice, in two shapes, cannot tell which is
authoritative — the printed one always is.

**What is NOT in the plan, first, on turn 1.** `discrepancy_report` opens with it
(`exclusion_note`): the orders the data could not use and why, any car two orders
both claim, how much of the stock matches something someone ordered.
**Never present it as the whole book.** If 24 of 25 orders are missing, that is
the first
thing they need and it is something they can act on — those orders need
completing in the system. Say it in their words ("no model on the order", "no
promised date"), never as a code, and never a count without its reason. This one
**must always be reported**; the solver's own self-check is the opposite — "checks
passed" is enough, unless it failed.

## Steering

Planner language becomes a typed override object (`overrides_schema.json`) — never
special-case code. The answer to the preferences question is what fills it; the
planner's words, not your reading of the data. Everything they can ask for
compiles into one of:

There are **three keys and no others**:

| Lever | What it does |
| --- | --- |
| `priority` | `[{"order": "900108-1", "step": "normal\|high\|urgent"}]` — who matters more. Every order starts at `normal`; only what they name moves. A bare card number raises every line on it. An unknown step is an error, so use exactly those three words. |
| `may_move` | `{only, also, never}` — who is in play. The default with this absent is the orders that need help: late, or with no car. `only` and `also` take the same filter `{models, orders, from_date, to_date}`; `also` can instead be `true`, meaning anyone still settled; `never` takes a list of order keys or card numbers. |
| `churn_price` | one number: how much a changed allocation costs. Omit it and the solver sweeps several and presents the middle one. |

`may_move` is one sentence said three ways — *who is in play this turn* — and the
precedence is **never beats only beats also**:

- **`only` NARROWS.** "Just fix August", "only the OMODA9s". It bounds the whole
  turn, including anything `also` authorised. It does not free anybody: a settled
  order inside the slice still stays put.
- **`also` WIDENS, inside `only`.** The orders the planner has authorised you to
  displace to rescue someone late. **This is the only way anyone gets bumped, and
  you ASK first** — `bump_candidates` gives you the concrete list to ask with.
  Their answer compiles to a filter if they named who, or to `true` if they said
  "whoever it takes". **It lasts this turn only:** run
  `session.carry_forward(override)` after the plan is out and carry the result
  into the next turn, so a permission given once is not still open three turns
  later. Say so when you confirm it.
- **`never` REMOVES, absolutely.** "Leave that one alone", even against an `also`
  naming the same order in the same breath. It is the only way to protect an
  order that is itself late, since such an order is in play by default.

Your job is the translation: "these orders" → real keys from the last change
list, "next cycle" or "August" → dates, "the OMODA9s first" → an urgent step on
those orders, "Delek Motors first" → an urgent step on every order that client
holds (there is no model-wide or client-wide lever: `priority`, `may_move.never`
and `may_move.also` all name order keys — or card numbers, which reach a whole
card and nothing wider — so YOU do the grouping and say which you used).
**Confirm the translation in plain words
before you run it** — "prioritising those two late orders over the rest" — not the
object itself.

Three things a planner may ask for that are **not** steering:

- **"Push that one to September."** That is a new promised date on the order, not
  a solver instruction. Say that it is a change to the order in the system; the
  solver prices lateness against whatever the promise says.
- **"Think in whole weeks."** The solver measures exact days. Round in your own
  wording if it helps them; do not pretend the plan changed.
- **"Make breaking an allocation cheaper."** That is `solver_config.yaml`, a
  reviewed change by a human, not something a turn does.

A new *constraint* is different from a lever. "Never split a dealer's cars across
weeks" is a model change: a reviewed code change with tests, never something you
do live. Say so and offer the nearest lever.

### Bumping — ask first

By default only orders that need help move, so a settled order never loses its
car. Sometimes the only way to rescue one is to take a car from an order that was
fine. Never do that uninvited:

1. Solve the plain repair first.
2. If an order they care about is still late, call `session.bump_candidates(...)`
   and show the planner who could be displaced, lightest first.
3. Compile their answer into `may_move.also` — the named orders, or `true` if
   they authorised anyone. The solver then displaces one only
   when it genuinely lowers the total cost — an authorisation is permission, not
   an instruction, so a bump that buys nothing simply does not happen. Say that
   plainly rather than reporting a failure.

Every bump is flagged in the change list, so a displacement is never silent.

### Carrying the instructions forward

The override is ONE object, not a log. Each turn you edit it — raise a priority,
narrow `may_move.only`, authorise an `also` — confirm it in words, and run it
against fresh data.
There is no history to replay and no order to get wrong. It is the only thing that
must survive: if your sandbox is reclaimed, recover it from the last version you
showed the planner.

One key is the exception: `may_move.also` is permission for a single solve, so
`session.carry_forward(override)` returns the object to carry into the next turn
with that permission spent. Everything else stands until they change it.

## Running it locally

```bash
python -m xas_allocation.flatten --pull dms_allocation.json
python -m xas_allocation.session     # a full turn end to end
```

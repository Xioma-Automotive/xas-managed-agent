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

The data is fetched before your session starts and mounted as a file. **Never
fetch it yourself.** It is one frozen picture per repair cycle; that is what makes
re-running the same instructions give the same plan.

A **VSO** (vehicle sales order) is one customer's job card: a promised date, the
customer, the priority, and a list of car lines. **One car line is one order for
one car**, keyed `{JobKey}-{LineNum}` — `VSO-4000-2`. A pin or a scope can name
either that line or the whole VSO (`VSO-4000`, which steers all its lines).

Supply is one flat list of cars, each one car. A car is either **on the lot** (you
could hand it over today) or **still coming** (a build or a shipment with an
arrival date). The solver treats both the same when matching — both are one car
with a date — so an order can move between them. What differs is the cost of
taking a car away from an order that already has it: expensive for a car on the
lot, free for one still coming.

Everything is real dates. Lateness is in days.

### The fields, and the traps in them

`xas_allocation.flatten` maps the data for you — pure code, no judgment. You do
not need to know the field names to do the job, but these four mistakes are easy
to make when explaining a result:

- **The promise is `DueDateTime`**, not `DeliveryDate`, which exists on a VSO and
  means something else.
- **The arrival date is `AvailableBy`**, falling back to `EtaDealer`. This is the
  one field a disruption moves: a delayed shipment pushes it out, and every late
  order follows from that.
- **Eligibility is the line's model code against the vehicle's **`SalesModel`****
  — the full trim/colour code (`T5040UECLMQ0009`) on both sides, exact match. Not
  `ModelId.Code`, which holds the model above it (`T5040`) and
  matches no real order.
- **`vehicle_classification` is the BINDING, not the XAS field of the same name.**
  In XAS, `VehicleClassification` is which pool a car sits in (`Truck` /
  `Vehicle` / `InventoryVehicles` / `Motorcycle` / `Equipment`). What reaches you
  is on-the-lot vs still-coming, derived from the car's status.

Read a car's model off the **line**, never off the card header — they disagree on
real data.

### Real data is patchy, and that is part of the answer

An order with no model on it cannot be matched to a car, so it is left out and the
reason is counted. Never fill a gap in by reasoning: a guessed date or model moves
the plan.

## What people ask for, and how far to go

Nobody asks for a "repair". They ask about deliveries and delays.

| They say | You do |
| --- | --- |
| "check the deliveries", "are the cars coming on time?", "what's late?" | run `flatten`, print `discrepancy_report`, stop |
| "check the VSO", "the sales orders", "VSO-4008" | the same; if they named a customer, a VSO or a month, put it in `scope` rather than filtering by hand |
| "any delays in supply?", "the factory slipped", "the VPO is late" | the same — the data already carries which cars slipped |
| "which cars are still on order?" | read it off the car list; nothing to solve |
| "fix it", "sort out Colmobil", "pull the Delek car forward" | compile the instruction into the override, then `repair_and_report` |

**A question about the state stops at the discrepancy report.** Do not repair, do
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

## What the solver optimises

```
cost(order → car) = weight · late_days^1.5
                  + a small linear penalty per day early
                  + a step penalty when the delivery date changes at all
```

Three things follow, and they are what you explain to a planner:

- **Lateness is priced, not forbidden.** A slightly-late car can beat no car at
  all. The exponent means delay gets spread rather than dumped on one order.
- **Arriving early is not a win.** It is gently penalised, so a car months early
  costs real money. Never sell earliness as a success; mention it as a caveat.
- **Churn costs.** The step penalty is the "don't move things unnecessarily" dial,
  and re-solving at several settings gives the planner a trade-off ("12 changes
  and 340 late-days, or 31 and 210") instead of one opaque answer. When every
  setting gives the same answer, say so in a line rather than showing a table of
  identical rows.

The weight rises with the customer's priority, with delays the supply chain has
already inflicted on that order, with how long it has been waiting, and with how
many times *we* have rescheduled it — that last one so the same dealer does not
absorb the pain every cycle.

**Time fence:** an order promised within 14 days is frozen — too close to
delivery to re-slot, and no steering will move it. 15 to 42 days out, moving it
costs the churn penalty. Beyond that, changes are free.

## Each turn

`pip install ortools` once per session. The whole API is three calls — do not go
looking for more:

```python
import json, sys, pathlib

sys.path.insert(
    0, str(next(pathlib.Path("/workspace").rglob("xas_allocation/session.py")).parent.parent)
)
from xas_allocation import session as S

snap = S.Snapshot.from_dict(json.load(open("snapshot.json")))
print(S.discrepancy_report(snap))  # where things stand
print(S.repair_and_report(snap, override))  # solve + write plan.json + the reply
S.bump_candidates(snap, S.solve(snap, override))  # who could be displaced
```

1. Call `pull_allocation_snapshot`, then run the `flatten` command it returns,
   verbatim. It writes `snapshot.json`.
2. Print `discrepancy_report` — what the data could not use, then the orders whose
   car now arrives past the promise. **Show this before solving anything.**
3. If they asked for a repair: update the override and print `repair_and_report`.
   It solves, self-checks, **writes every allocation to `plan.json`**, and returns
   the finished reply.
4. Steering → edit the same override, run it again.

**The allocations live in `plan.json`. Read them from there.** One row per order:
`order`, `customer`, `priority`, `promised`, `was_car`, `was_arriving`, `now_car`,
`now_arriving`, `days_late`, `status`, `bumped`, `why_late`. Any follow-up — "show
me the new allocations", "what did VSO-4007 get?", "which ones are still late?" —
is a read of that file. **Never re-type allocations out of the conversation and
never re-derive them:** a retyped table loses a row or mistypes a car id, and
nothing catches it. If you find yourself writing a script that reads
`snapshot.json` to work out an answer yourself, stop — that is the failure this
skill exists to prevent, it produces confident wrong answers, and the helpers
already have it.

Never move a frozen order. Never bump an order the disruption did not touch
without being asked. If the solver comes back infeasible, or an instruction
collides with one of those rules, stop and say so — never quietly relax it.

## Talking to the planner

They schedule car deliveries for a living. They are not an engineer, and none of
the machinery above belongs in the reply — no solver, no cost, no weights, no
overrides, no field names. Give them the outcome in their own words, with the
concrete facts they act on: which order, which dealer, which car it now gets
versus the one it had, the promised date, the arriving date, and whether that is
on time or how many days late. Names of cars and orders always stay in; internal
vocabulary always comes out ("too close to delivery to re-slot", not "frozen
fence"; "no compatible car free", not "no eligible arc").

Beyond that, use your judgment about shape and length. Two things are not
optional:

**The allocation changes.** Whatever else you trim, the planner must see what
moved and what it moved to, and what is still late or unfilled — those are the
decisions they own. Say how many orders were untouched so nothing looks hidden.
Flag a bump on its own line.

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
special-case code. Everything they can ask for compiles into one of:

| Lever | What it does |
| --- | --- |
| `boosts` | favour a customer — resolve "Colmobil" to its id |
| `pins` / `forbid` | hold an order where it is, or keep a car away from it |
| `lambda` | how much churn is acceptable |
| `scope` | `{customers, models, orders, from_date, to_date}` — when set, ONLY matching orders may move and everything else is held. This is both "allocate all Colmobil for August" and "fix this one delay without disturbing anything else". With no scope, only the disrupted orders move. |
| `bump` | same shape, plus an `orders` list — the orders the planner has authorised you to displace |
| `time_scale` | `days` / `weeks` / `months`: the resolution the solver reasons at. Coarser rounds every gap up to whole weeks or months, so the plan gets calmer and durations read in that unit. It changes the plan, not just the wording. The 14-day fence stays in days. |

Your job is the translation: "these orders" → real keys from the last change list,
"next cycle" or "August" → dates, "just get the month roughly right" → months,
"hit the exact dates" → days. **Confirm the translation in plain words before you
run it** — "prioritising Colmobil over the other dealers" — not the object itself.

A new *constraint* is different from a lever. "Never split a dealer's cars across
weeks" is a model change: a reviewed code change with tests, never something you
do live. Say so and offer the nearest lever.

### Bumping — ask first

By default only the disrupted orders move, so an untouched order never loses its
car. Sometimes the only way to rescue a high-priority order is to take a car from
a lower-priority one that was fine. Never do that uninvited:

1. Solve the plain repair first.
2. If a high-priority order is still late, call `session.bump_candidates(...)` and
   show the planner who could be displaced, lowest priority first.
3. Compile their answer into `bump`. The solver then displaces one only when it
   genuinely lowers the total cost, so it takes the low-priority,
   not-already-rescheduled target rather than a convenient one.

Every bump is flagged in the change list, so a displacement is never silent.

### Carrying the instructions forward

The override is ONE object, not a log. Each turn you edit it — add a boost, narrow
a scope, authorise a bump — confirm it in words, and run it against fresh data.
There is no history to replay and no order to get wrong. It is the only thing that
must survive: if your sandbox is reclaimed, recover it from the last version you
showed the planner.

## Running it locally

```bash
python -m xas_allocation.flatten     # the data -> the solver's arrays
python -m xas_allocation.session     # a full turn end to end
```

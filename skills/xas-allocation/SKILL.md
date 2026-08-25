---
name: xas-allocation
description: >-
  Repair a vehicle-to-order allocation after a disruption (delayed shipment,
  changed inbound, manual steering), and say where the vehicle sales orders and
  their cars stand. Use whenever someone asks about deliveries or arrivals
  ("check the deliveries", "are the cars coming on time", "what is late"), about
  a VSO / vehicle sales order / sales order / customer order, about a delay in
  supply or in a VPO / vehicle purchase order ("the factory slipped", "check for
  delays in supply"), which car an order gets, or asks to re-allocate,
  defer/pin/boost orders, or explain an allocation change. Translate the
  situation + planner instructions into inputs for a deterministic min-cost-flow
  solver, run the λ sweep, self-check hard constraints, and emit a reason-coded
  change list. Does NOT allocate by reasoning — it drives the reference solver.
  Do NOT use for general reporting over job-card records (counts, breakdowns,
  charts) — that is xas-reporting.
---

# XAS allocation repair skill

**Core invariant — the whole design in one line:**

> `plan = pure_function(data_snapshot, skill, override)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is lost. That is the
bug to guard against everywhere. You do **not** decide allocations: you build the
network, call the solver, and explain the result. `override` is a **single
combined object** you carry forward (§7); same snapshot + same override →
byte-identical plan.

Reference solver version pinned by this skill: **0.2.0-prototype**
(`xas_allocation/` package; DECIDE-9 — canonical version moves to a tested repo
before real dealer data).

## The data model (real XAS vocabulary — DECIDE-7)

The pull is `{meta, vsos, vehicles, disruption}`. It is fetched **host-side**
from a callable source (`datasource.py` — the `scenario_engine/` fake by default,
the live system read through the app MCP by config) and mounted into your sandbox
as a file by `web.py`. **You never fetch it yourself**: it is one frozen snapshot
per repair cycle, which is what makes re-applying the same override reproducible.
The skill itself carries no dataset — only this file and the solver package — so
regenerating the data needs no re-deploy.

A **VSO** (Vehicle Sales Order, one customer) is a job card carrying a promise —
`DueDateTime`, the customer, the priority — and a list of **job items**. **The
grain is the CAR: one `ModelItem` job item wants `Quantity` of them**, so a
single VSO can be many orders. `JobItemType` is the discriminator and the only
thing separating a car from a `Configuration` or parts row; the host-side mapper
applies it, so every `JobItems` entry that reaches you is already a car. Read the
car's model off the **line**, never off the header — the card's own
`SalesModelCode` disagrees with its lines on real data, and the detail shape does
not carry it at all.

`Quantity` is **expanded**: a line wanting 3 cars is 3 orders, each one car of
demand. So the order key has three levels — `{JobKey}-{LineNum}-{n}`, e.g.
`VSO-4000-2-3` — and every key carries all three, a one-car line included.

**Steering is usually per LINE; results are per CAR.** Going in, a line's cars are
interchangeable — same model, same promise, same customer — so a pin, forbid or
scope normally names the line, and the solver matches any of the three levels (one
car / the line / the whole VSO): `VSO-4000-2` steers all its cars at once. Coming
out they are NOT interchangeable: each car has its own vehicle, from its own
shipment, with its own arrival date. Report it that way — "cars 1-3 of 5" on one
date, "car 4 of 5" on another — and never average a line into one date it does
not have.

**A multi-car line is usually only PARTLY allocated.** A line resolves to at most
one vehicle, and one car cannot satisfy two orders, so a qty-3 line has at most
one incumbent and its other two cars are genuine unfilled demand. When a line
claims `AllocQty` above that, the extra committed cars exist but this pull cannot
identify them — they are counted in `snapshot.meta.excluded.flatten_skips` as
`allocation_qty_not_resolvable_to_cars`, never invented. Say so if it is material:
those cars may already have vehicles you cannot see.

Each order is allocated to one **vehicle** from a single flat pool — no PO/PDN/slot
layer, and a vehicle is always exactly one car.

**`vehicle_classification` is the BINDING, not the XAS field of the same name.**
`"Vehicle"` = a car you can hand over now (a **hard** binding); `"Future"` = a
car still coming (a **soft** binding). In real XAS, `VehicleClassification` is
something else entirely — `Truck` / `Vehicle` / `InventoryVehicles` /
`Motorcycle` / `Equipment`, which pool the car sits in — and the binding is
derived from the vehicle's **`Status`**: `Ordered` / `On The Way` are future,
`In Stock` / `Available For Sale` are here now. The names collide; the host-side
mapper does the deriving, so what reaches you is already the binding.

`xas_allocation.flatten` maps the pull — **pure code, no judgment** — into the
three arrays the solver reads:

- **`orders[]`** (the wanted cars): `so_id · line · qty_index · customer ·
  customer_id · sales_model · priority · delivery_date · price · n_prior_delays ·
  days_backordered · times_rescheduled` (key = `{so_id}-{line}-{qty_index}`;
  `price` is the line total split across its cars)
- **`units[]`** (the vehicle pool): `vehicle_id · vehicle_classification ·
  sales_model · eta_dealer`
- **`incumbent[]`**: `order_key → vehicle_id` (the current allocation)

Everything is **real dates** (`YYYY-MM-DD`); tardiness is in **days**.
`eta_dealer` (from a vehicle's `EtaDealer`) is the one field a disruption moves
(a delayed shipment slips it on the affected vehicles). `delivery_date` (from the
VSO's `DueDateTime`) is the commitment tardiness is measured against; a
discrepancy is when the allocated vehicle's `eta_dealer` now runs past it.

Field mapping (real XAS → solver): the LINE's `JobItemCode` → `sales_model`;
`DueDateTime` (card) → `delivery_date` (the promise — **not** `DeliveryDate`,
which exists on a VSO and means something else); the line's `VehicleId.Code`, else
the card's `VehicleDMSCode` when it holds exactly one car line, ↔ `VehicleCode` is
the incumbent link; vehicle `SalesModel` → the unit's `sales_model`;
`EtaDealer` (or `AvailableBy`) → `eta_dealer`; `JobPriority.Code` → `priority`.

**Eligibility (the sparse arc rule) — computed, never stored:** an arc
`order → vehicle` exists iff `order.sales_model == vehicle.sales_model` — the
line's `JobItemCode` against the vehicle's **`SalesModel`**, a full
trim/colour code (`T5040UECLMQ0009`) on both sides. Not `ModelId.Code`, which
holds the model above it (`T5040`) and matches no real order. Hard equality, not
a fuzzy match — no LLM spec-residual. The solver treats a real and a future
vehicle identically for matching (both are capacity-1 supply with a date), so an
order can be re-linked between them. Lateness is **priced**, not forbidden, so a
slightly-late vehicle can still be placed instead of backordering.

**Real data is patchy, and that is part of the answer.** A sales order with no
model on it has nothing to match a car against, so the host-side pull leaves it
out and counts why — report it as "Turn 1" below says. Never fill a gap in by
reasoning: a guessed date or model moves the plan and breaks the invariant.

## The words people actually use

Nobody asks for a "snapshot". They ask about deliveries, VSOs and supply delays.
Every row below starts the same way — pull → `flatten` → `discrepancy_report`;
what differs is how much comes after.

| They say | They mean | You do |
| --- | --- | --- |
| "check the deliveries", "are the cars coming on time?", "what's late?" | promised dates vs the dates the cars now arrive | pull → `flatten` → `discrepancy_report`, then **stop** |
| "check the VSO", "the sales orders", "customer orders", "VSO-4008" | the VSO car lines — one wanted car is one order | same; if they named a customer, a VSO or a month, put it in `scope` instead of filtering by hand |
| "any delays in supply?", "delay in the VPOs", "the factory slipped", "the shipment is late" | some cars' arrival dates moved out — the pull already carries it | same; the report names the affected orders and cars |
| "which cars are still on order?" | supply still on a factory purchase order — a **Future** vehicle, soft binding | read it off the snapshot's units; nothing to solve |
| "fix it", "sort out Colmobil", "pull the Delek car forward" | a repair | compile the override, then `repair_and_report` |

**A question about the state stops at the discrepancy report.** Do not repair, do
not invent an override, do not offer a plan until they ask for one. Answering
"check the deliveries" with a re-allocation nobody requested moves cars in the
planner's head that nobody moved.

**VPO and VGR, precisely.** Each order line records where its car comes from:
`AllocSourceClassification` `"VGR"` = received (a real VIN, a **hard** binding),
`"VPO"` = still on order from the factory (a **Future** vehicle, a **soft**
binding, free to reshuffle). So "is the VPO delayed?" is answerable — it is the
arrival date of the cars still on order. What this pull does NOT carry is a VPO
*number*: it holds **no VPO ids** and no per-VPO rows, so you cannot list "the
open VPOs" or group by one. (A choice about what the pull fetches, not a gap in
XAS: the plan is over cars and orders, and a VPO adds a layer the solver has no
use for.) Say so plainly and offer what you do have — the cars on order and when
they now land.

## The cost model (verbatim — §2)

```
cost(o → u) = W(o) · late_units^1.5  +  EARLY_WEIGHT · W(o) · early_units
              +  λ(fence) · [delivery differs from promise by ≥ 1 unit]

late_units / early_units = the day-gap rounded UP to whole time_scale units
W(o) = priority · (1 + α·n_prior_delays + γ·times_rescheduled)   [+ β·days_backordered]
```

Every business factor maps to exactly one lever:

| Factor            | Encoding                                            |
|-------------------|-----------------------------------------------------|
| customer priority | multiplicative weight on W                           |
| delayed before (supply) | `α·n_prior_delays` escalation on W            |
| bumped by us before | `γ·times_rescheduled` escalation on W (DECIDE-11) — fairness: protects an already-rescheduled order from being delayed *again* |
| back-order aging   | `β · days_backordered` (DECIDE-1: additive default) |
| don't recall      | HARD pin — vehicle removed from choice, no cost     |
| minimal changes   | `λ` step penalty on changed-date arcs               |
| convex lateness   | exponent `1.5` so delay never dumps on one order    |
| **arriving too early** | **`EARLY_WEIGHT·W·early_units` (DECIDE-15) — linear + small, so lateness always dominates; a little early is cheap, a lot early is costly. Lateness is NOT softened.** |
| time fence        | frozen / slushy / liquid → `λ` varies by horizon    |
| **time resolution** | **`time_scale` (DECIDE-14) rounds every gap UP to days/weeks/months — a planner knob, see §6. Default days = exact.** |

Term placement matters: priority / prior-delays / aging build the **weight**
`W(o)`; the convex exponent shapes the **lateness term**; the earliness term is a
separate, gentle, *linear* add-on; `λ` is a **separate additive term**, untouched
by weight escalation. `λ` is per-arc linear — the problem stays a pure linear
min-cost flow.

**The λ sweep is the highest-value output.** Re-solve for λ ∈ {0,5,10,25,50,100}
(same network, only some arc costs change) and hand the planner a Pareto frontier
("12 changes → 340 weighted late-days" vs "31 → 210"), not one opaque answer.

**Time fence (DECIDE-2):** promised ≤14 days out = frozen (hard pin); 15–42 days
= slushy (`λ` applies); beyond = liquid (date changes free). A frozen order is
too close to delivery to re-slot at all — no steering will move it, which is the
turn-1 fact the planner most needs.

## Solver (§3)

OR-Tools `SimpleMinCostFlow` — provably optimal, deterministic, ms-scale. NEVER
implement the algorithm; build the network and call the library. When orders
become **coupled** (fleet all-or-nothing, transport batching), flow structure
breaks — switch to CP-SAT + LNS as a **reviewed PR against the solver repo with
tests**, never as live-session code.

## Procedure (§8) — each turn

1. **Pull** with `pull_allocation_snapshot`, then run the returned **`flatten`
   command** verbatim to write `snapshot.json`. The host has already fetched the
   pull and mounted it as a file; the command reads that file (never a bundled
   copy). This is the data-prep step;
   `xas_allocation.session.data_prep_flowchart` draws it as a mermaid flow chart.
2. **Map discrepancies** — print `session.discrepancy_report(snapshot)`. It opens
   with what the pull could not use and why (`exclusion_note`), then lists the
   orders whose allocated vehicle now delivers past its `delivery_date` and
   classifies each fixable vs locked-in. Show this **before** solving.
3. Apply the current combined override (weights / pins / forbid / lambda / scope /
   bump), build the graph (§4), run the **λ sweep**.
4. **Self-check hard constraints:** no frozen-fence order moved, no
   sales_model violation, every order has exactly one vehicle (or a surfaced
   backorder), no vehicle double-booked. `repair_and_report` runs it; trust it.
5. **Print `session.repair_and_report(snapshot, override)`** — it does steps 3–4
   and returns the finished, jargon-free reply.
6. Human approves → write back via MCP (approval-gated). Steering → edit the one
   combined override and re-run step 3.

## Planner-facing output — the reply the planner reads

**`session.discrepancy_report` and `session.repair_and_report` already produce
this reply — print them, don't rebuild them.** The contract below is what those
helpers emit and why: read it to trust the output and answer follow-ups.
Hand-assembling the report from raw solver fields, or writing ad-hoc analysis
scripts, is the exact leak the invariant guards against — and it is slow, leaks
jargon, and re-computes what the helper already did.

The planner is a dealer-allocation scheduler, not an engineer: the reply must
answer "did my instruction land, what moved, what still needs me" at a glance.
Write **outcomes in business terms**; keep the machinery in the sandbox.

**Concrete, and short** — those pull in the same direction. Concrete = the order
key, the dealer, the vehicle it now gets vs. the one it had, the promised date,
the arriving date, on time or N days late; never trim those. Short = everything
else: no preamble, no restating the instruction, no narrating your steps, no
summary after the summary. Machinery words are banned outright — translate or
drop, per **The words** below.

**Turn 1 opens with what is NOT in the plan — before anything else.**
`discrepancy_report` prints it (`session.exclusion_note`): the sales orders the
pull could not use and why, any car two orders both claim, and how much of the
stock matches something someone ordered. **Never present it as the whole book.**
If 24 of 25 orders are missing, that is the first thing the planner needs, and it
is actionable — those orders need completing in the system. Say it in their words
("no model on the order", "no promised date"), never a reason code, and never a
count without its reason. (On the fabricated dataset nothing is excluded and the
note is empty.)

**Turn 1 also splits the broken orders into can-be-repaired vs locked-in**, so a
frozen order gets its expedite call on turn 1 — not after three rounds of
steering that can't move it.

**Lead with the outcome, in one or two lines:** what the instruction did (or
didn't do), how many orders moved, how many are still late. Answer the question
they actually asked *first* — if they said "prefer Colmobil", the first fact is
what happened to Colmobil.

**Then a change summary — mandatory, never dropped.** The reason-coded change
list is the deliverable; trimming jargon must never mean omitting the allocation
changes. Every changed order as a table row:

| Order      | Dealer (priority) | Was arriving | Now arrives    | Promised   | Allocation (actual change) | Result |
|------------|-------------------|--------------|----------------|------------|----------------------------|--------|
| VSO-4001-1 | Colmobil (A)      | 2026-10-05   | **2026-08-24** | 2026-08-24 | now `VEH-9044` (was `VEH-9001`) | ✅ on time |

**Always name the actual allocation change** — the vehicle the order now gets vs.
what it had (`now VEH-9044 [future] (was VEH-9001)`). The planner allocates by
these references; "moved to an earlier car" without the id is not actionable.
Flag any **bump** (an untouched order displaced) on its own line.

Then a second table, **still late / unfilled**, under a "needs your call" heading
— the decisions the planner owns, split into locked-in vs no-car, with `↑moved`
marking an order that appears in both. Close with the count of unchanged orders ("The
other 112 orders are unchanged") so nothing is silently hidden. Sort changed
orders most-improved first, with the priority-A dealers and the order named in the
instruction easy to find.

**Surface the one thing they'd miss** in one plain sentence: the steering barely
mattered (the boosted dealer had one order in play), a high-priority order is
still late, a pin cost extra changes. Don't bury it under the table.

**Dates:** `2026-08-24`, consistently. **Late:** "21 days late", not a raw
tardiness number. **End** with the natural next steering options in the planner's
words ("defer the late Delek order", "protect Colmobil next cycle").

### Stays internal — never in the planner reply

Compute it, rely on it, but do **not** print it unless the planner asks or it
carries a decision:

- **The override JSON**, `customer_id`s (`CUST-001`), `weight_mult`, "override
  object", "seed", "reproducible", "turn N". Confirm the *translation* in plain
  words ("prioritizing Colmobil over the other dealers") before running — not the
  raw object. Print the object only on request, or when you must hand it over so
  a session can be resumed after the sandbox is reclaimed (DECIDE-5).
- **The λ sweep table when it's flat.** A frontier where every row is identical
  is noise — collapse it to one sentence ("churn/lateness didn't trade off this
  cycle"). Show the table only when the rows genuinely differ and the planner has
  a point to pick.
- **Self-check field dumps** (`every_order_placed`, `unfilled_count`,
  double-booked). Report it as one word — "checks passed" — or, on a violation,
  the plain-English violation only. (This is about the SOLVER's self-check. The
  pull's exclusion census is the opposite — it must always be reported.)
- **Node indices, objective-in-micros, solver status, λ internals.** (NOT the
  supply ids — the VehicleCode of the actual swap DOES belong in the change
  table. Keep them out of the one-line *headline*, but always in the table.)

### The words — translate or omit

Their words are not all jargon: **VSO, sales order and VPO are theirs, use
them.** Everything below is yours, and never reaches the reply as written.

| Internal | Say instead |
| --- | --- |
| `delivery_date` | the promised date / what the customer was promised |
| `eta_dealer` | when the car lands / arriving |
| `Vehicle` classification, a VIN | a car in stock — name the VIN, they allocate by it |
| `Future` classification | a car on order from the factory |
| `sales_model` | the model |
| a VSO car line, "jobitem" | the order, or "the second car on VSO-4008" |
| frozen fence, slushy | too close to delivery to re-slot |
| no eligible arc | no compatible car free |
| a boost / weight on `CUST-001` | "prioritizing Colmobil" — never the raw id |
| `time_scale: weeks` | planning in whole weeks |
| λ, Pareto frontier, weighted late-days, mid-frontier default | the trade-off in plain numbers: "12 changes vs 31" |
| solver, min-cost-flow, network, arc, cost, incumbent | omit |
| snapshot, flatten, the pull, override, scope, pin, break cost, seed, "turn N", DECIDE-n | omit — or "the current position" |

## Steering contract (§6) — prompt compiles to parameters, NEVER code

Planner natural language → a typed override object (see
`xas_allocation/overrides_schema.json`). This object is the **flexibility
surface**: you handle *any* request by compiling it into `boosts` / `pins` /
`forbid` / `lambda` / **`scope`** / **`bump`** / **`time_scale`** — never by
special-casing in prose. Your job is the **translation**: resolve "Colmobil" →
its customer_id, "these orders" → real keys from the previous turn's change list,
"next cycle"/"August" → a date or date range, "just get the month roughly right"
→ `time_scale: months`, "hit the exact dates" → `time_scale: days`. **Confirm that translation back in plain words before
running** — the object itself stays internal.

`time_scale` (DECIDE-14) sets the resolution the solver reasons at: coarser
scales round every gap up to whole weeks/months and stop distinguishing smaller
differences, so the plan itself gets calmer (and durations read in that unit). It
changes the plan, not just the wording; the hard time fence stays in days. There
is no separate "how early is OK" knob — the earliness term is always on and
gentle, and a coarse `time_scale` already absorbs sub-unit earliness.

Review gate:

| Request                                                  | Handling                          |
|----------------------------------------------------------|-----------------------------------|
| "delay VSO-4471", "prefer Colmobil", "more churn", "allocate all Colmobil for August", "just fix this delay" | Runtime override (weights / pins / **scope**). Instant, safe. |
| "never split a dealer's vehicles across weeks"           | New **constraint** → **PR with tests**, not a live mutation. |

The prompt moves weights, pins, and scope; a human moves the model.

### Scope — work a slice, keep fixes local

`scope` is a general filter carried in the override:
`{customers?, models?, orders?, from_date?, to_date?}`. **When a scope is present
it DEFINES the free set** — only orders matching *all* given dimensions may move;
everything else stays pinned. With no scope, the free set is the disrupted rows
(the default repair). One mechanism, two everyday jobs:

- **Monthly / per-customer allocation:** "allocate all Colmobil orders for
  August" → `scope {customers:["CUST-001"], from_date:"2026-08-01",
  to_date:"2026-08-31"}`, then solve the slice.
- **Localized fix:** to fix one delay "without disrupting everything else", scope
  narrowly (a customer, a model, a short window). Repair-not-rebuild already pins
  the rest; scope makes the bound explicit and auditable.

### Bumping an untouched order — ASK first (DECIDE-13)

By default the repair frees only disrupted rows, so an **untouched** order is
never displaced. Sometimes the only way to get a high-priority disrupted row on
time is to **bump** an untouched lower-priority row off its on-time vehicle.
Never do this uninvited:

1. Solve the plain repair first. If a high-priority row is still late, call
   `session.bump_candidates(snapshot, result)` — the untouched, movable rows whose
   vehicle would rescue it, lowest priority first.
2. **Ask the planner explicitly** who may be bumped (show the candidates).
3. Compile their answer into the `bump` filter (same shape as scope, plus an
   `orders` list). The solver frees exactly those rows and displaces one **only
   when it lowers total cost** — so it takes the low-priority,
   not-already-rescheduled target, never gratuitously.

Every bump is flagged in the change list (`— BUMPED …`), so a displacement is
never silent.

## Steering state — one combined override (§7)

Each turn you *edit that one object* (add a boost, tighten a scope, authorize a
bump), confirm it in words, and feed it + a fresh pull into the solver. No
ledger, no append-only history, no replay, no TTL. The sandbox (loaded data,
solver in memory) is a performance convenience only: discard it, re-pull,
re-apply the **same** override → the same plan. If not, state has leaked.

The override is the only state that must survive a sandbox reclaim; recover it
from the last one you showed the planner. It is order-independent — a set of
accumulated instructions, not a sequence — so there is no replay order to get
wrong.

DECIDE-5: durable, cross-session persistence stays **deferred** — the real fix is
a host-side store (`web.py` keyed by session id, shipped in via the pull). Here
the override lives only in the conversation, and no audit trail of *who* steered
*when* is kept.

## Infeasibility (§9)

Never silently relax a pin. Prototype default (DECIDE-8): instruction pins run as
**high-cost soft constraints** so the solver always returns something; a violated
pin surfaces as a large cost line ("honouring this pin costs 3 extra changes —
proceed?"). CP-SAT assumption-literal minimal conflict sets are the honest
upgrade, deferred.

## Running the reference solver

The bundle is this file plus the `xas_allocation` package, and the sandbox runs
plain `python`:

```bash
python -m xas_allocation.flatten     # rich pull -> snapshot (the pull's own flatten command does this)
python -m xas_allocation.session     # the §8 loop end to end
python -m xas_allocation.decisions   # every open DECIDE + its default
```

The generator, the source census and the determinism proof (`scenario_engine`,
`datasource`, `tests/`) are host-side and are not in this sandbox.

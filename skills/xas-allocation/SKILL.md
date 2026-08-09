---
name: xas-allocation
description: >-
  Repair a vehicle-to-order allocation after a disruption (delayed PDN, changed
  inbound, manual steering). Translate the situation + planner instructions into
  inputs for a deterministic min-cost-flow solver, run the λ sweep, self-check
  hard constraints, and emit a reason-coded change list. Use whenever a planner
  asks to re-allocate, defer/pin/boost orders, or explain an allocation change.
  Does NOT allocate by reasoning — it drives the reference solver.
---

# XAS allocation repair skill

**Core invariant — the whole design in one line:**

> `plan = pure_function(data_snapshot, skill, override)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is lost. That is the
bug to guard against everywhere. `override` is a **single combined object** you
carry forward and show each turn (no ledger, no replay); same snapshot + same
override → byte-identical plan. You do **not** decide allocations; you build the
network and call the solver, then explain the result.

Reference solver version pinned by this skill: **0.2.0-prototype**
(`xas_allocation/` package; DECIDE-9 — canonical version moves to a tested repo
before real dealer data).

## The data model (dates, XAS-shaped — DECIDE-7, `docs/xasdatamodel.md`)

Supply-first: `PO → PDN → Vehicle (pool)`, `Customer → SO`. A **Sales Order**
(one customer) groups **vehicle order rows**; the **row** is the allocatable
unit. Each row is allocated to a piece of **supply** that is one of two kinds: a
concrete **Vehicle** (a VIN) or a **PO-line slot** (a future car, keyed
`PO-model-row` e.g. `PO-150-1-5`). The pull comes from a callable data source
resolved host-side (`datasource.py` — the `scenario_engine/` fake by default, the
real XAS endpoint by config; DECIDE-7), which `web.py` fetches and mounts into
your sandbox as a file. `xas_allocation.flatten` maps it — **pure code, no
judgment** — into the three arrays the solver reads:

- **`orders[]`** (vehicle order rows): `order_id · so_id · customer ·
  customer_id · sales_model · priority · promised_date · eta_date · price ·
  n_prior_delays · days_backordered · times_rescheduled`
- **`units[]`** (supply = vehicles ∪ PO-line slots): `vehicle_id · kind ·
  sales_model · planned_delivery_date · location_state · po_ref · pdn · committed`
- **`incumbent[]`**: `row_id → supply_id` (the current allocation)

Everything is **real dates** (`YYYY-MM-DD`); tardiness is in **days**.
`planned_delivery_date` is the one field a disruption moves (a delayed PO slips
every supply item under it — slots and vehicles). `promised_date` is the
commitment tardiness is measured against; `eta_date` is the originally-expected
delivery (a discrepancy = the allocated supply now delivers past it).

**Eligibility (the sparse arc rule) — computed, never stored:** an arc
`row → supply` exists iff `row.sales_model == supply.sales_model`. Hard equality,
not a fuzzy match — no LLM spec-residual. The solver treats a Vehicle and a
PO-line slot identically (both are capacity-1 supply with a date), so a row can
be re-linked between them. Lateness is **priced**, not forbidden, so a slightly-
late supply item can still be placed instead of backordering.

## The cost model (verbatim — §2)

```
cost(o → u) = W(o) · late_units^1.5  +  EARLY_WEIGHT · W(o) · early_units
              +  λ(fence) · [delivery differs from promise by ≥ 1 unit]

late_units / early_units = the day-gap rounded UP to whole time_scale units
W(o) = priority · (1 + α·n_prior_delays + γ·times_rescheduled)   [+ β·days_backordered]
```

Encodings — every business factor maps to exactly one lever:

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
| **time resolution** | **`time_scale` (DECIDE-14) rounds every gap UP to days/weeks/months, so the solver ignores sub-unit differences. Planner knob; default days = exact.** |

Term placement matters: priority / prior-delays / aging build the **weight**
`W(o)`; the convex exponent shapes the **lateness term**; the earliness term is a
separate, gentle, *linear* add-on; `λ` is a **separate additive term**, untouched
by weight escalation. `λ` is per-arc linear — the problem stays a pure linear
min-cost flow.

**The λ sweep is the highest-value output.** Re-solve for λ ∈ {0,5,10,25,50,100}
(same network, only some arc costs change) and hand the planner a Pareto frontier
("12 changes → 340 weighted late-days" vs "31 → 210"), not one opaque answer.
When the frontier is flat (all rows identical), don't table it — say so in a line
(see Planner-facing output).

**Time fence (DECIDE-2):** promised ≤14 days out = frozen (hard pin); 15–42 days
= slushy (`λ` applies); beyond = liquid (date changes free).

## Solver (§3)

OR-Tools `SimpleMinCostFlow` — provably optimal, deterministic, ms-scale. NEVER
implement the algorithm; build the network and call the library. When orders
become **coupled** (fleet all-or-nothing, transport batching), flow structure
breaks — switch to CP-SAT + LNS as a **reviewed PR against the solver repo with
tests**, never as live-session code.

## Procedure (§8) — each turn

1. Confirm MCP liveness (DECIDE-6). *Prototype: synthetic data, skipped.*
2. **Pull** with `pull_allocation_snapshot`, then run the returned **`flatten`
   command** verbatim to write `snapshot.json`. The host has already fetched the
   pull and mounted it in your sandbox as a file; the command reads that file
   (never a bundled copy). `flatten` (in `xas_allocation.flatten`) maps the rich
   pull into `orders/units/incumbent` — pure code; never re-shape the data by
   reasoning. This is the data-prep step;
   `xas_allocation.session.data_prep_flowchart` draws it as a mermaid flow chart.
3. **Map discrepancies** — print `session.discrepancy_report(snapshot)`. It lists
   the SO rows whose allocated supply now delivers past its `promised_date` **and
   classifies each fixable vs locked-in** (frozen fence / committed vehicle). Show
   this **before** solving — it is the turn-1 truth of what the disruption broke
   *and* which of it re-allocation can't touch.
4. Apply the current combined override (weights / pins / forbid / lambda / scope /
   bump), build the graph (§4), run the **λ sweep**. `session.repair_and_report`
   does all of this and returns the finished reply.
5. **Self-check hard constraints:** no frozen/committed vehicle moved, no
   sales_model violation, every order has exactly one vehicle (or a surfaced
   backorder), no vehicle double-booked. (`repair_and_report` runs the self-check;
   trust it.)
6. **Print `session.repair_and_report(snapshot, override)`** — the finished,
   jargon-free reply (headline · what-changed table with the actual swap ·
   still-late split into locked-in vs no-car · unchanged count · one caveat). It
   already builds the reason-coded change list; **do not re-derive the solver's
   output by hand or write ad-hoc analysis scripts** — that is the exact leak the
   invariant guards against, and it burns time and tokens.
7. Human approves → write back via MCP (approval-gated). Steering → edit the one
   combined override and re-run step 4.

## Planner-facing output — the reply the planner reads

**`session.discrepancy_report` and `session.repair_and_report` already produce
this reply — print them, don't rebuild them.** The contract below is what those
helpers emit and why; read it so you can trust the output and answer follow-ups,
not so you re-derive it. Hand-assembling the report from raw solver fields (the
old habit) is slow, leaks jargon, and re-computes what the helper already did.

The planner is a dealer-allocation scheduler, not an engineer. The reply's job
is to let them answer "did my instruction land, what moved, and what still needs
my attention" at a glance. Write **outcomes in business terms**; keep the
machinery in the sandbox.

**Turn 1 is the discrepancy map, and it must say what's stuck.**
`discrepancy_report` splits the broken orders into **can be repaired** vs
**locked in** (too close to delivery to re-slot, or riding a committed vehicle).
Say this up front — an order that's frozen won't be helped by any re-allocation,
and the planner needs to know that on turn 1 (a call to expedite the delivery),
not after three rounds of steering that can't move it.

**Lead with the outcome, in one or two lines.** What the instruction did (or
didn't do) and the headline: how many orders moved, how many are still late.
Answer the question they actually asked *first* — if they said "prefer Colmobil",
the first fact is what happened to Colmobil.

**Then a change summary — this is mandatory, never dropped.** The reason-coded
change list is the deliverable. Trimming jargon must never mean omitting the
allocation changes. Render every changed order as a table, in plain columns:

| Order   | Dealer (priority) | Was arriving | Now arrives    | Promised   | Allocation (actual change) | Result |
|---------|-------------------|--------------|----------------|------------|----------------------------|--------|
| SO-4001 | Colmobil (A)      | 2026-10-05   | **2026-08-24** | 2026-08-24 | now `VEH-9044` (was `VEH-9001`) | ✅ on time |

**Always name the actual allocation change** — the concrete supply the row now
gets vs. what it had (`now PO-152-1-3 [slot] (was VEH-9001)`). The planner
allocates by these references; "moved to an earlier car" without the id is not
actionable. Flag any **bump** (an untouched order displaced) in its own line.

Then a second table for orders **still late / unfilled** under a "needs your
call" heading — these are the decisions the planner owns. Close with the count
of unchanged orders (e.g. "The other 112 orders are unchanged") so nothing is
silently hidden. Sort changed orders most-improved first; put the priority-A
dealers and the order named in the instruction where they're easy to find.

**Surface the one thing they'd miss.** If the steering barely mattered (the
boosted dealer had one order in play), if a high-priority order is still late, if
a pin cost extra changes — say so in a single plain sentence. Don't bury it under
the table.

**Dates:** show as `2026-08-24`, consistently. **Late:** say "21 days late", not
a raw tardiness number. **End** with the natural next steering options in the
planner's words ("defer the late Delek order", "protect Colmobil next cycle") —
not internal levers.

### Stays internal — never in the planner reply

Compute it, rely on it, but do **not** print it unless the planner asks or it
carries a decision:

- **The override JSON**, `customer_id`s (`CUST-001`), `weight_mult`, "override
  object", "seed", "reproducible", "turn N". Confirm the *translation* in plain
  words ("prioritizing Colmobil over the other dealers") before running — not the
  raw object.
- **The λ sweep table when it's flat.** §2 keeps the sweep as the high-value
  computation, but a frontier where every row is identical is noise — collapse it
  to one sentence ("churn/lateness didn't trade off this cycle"). Show the table
  only when the rows genuinely differ and the planner has a point to pick.
- **Self-check field dumps** (`every_order_placed`, `unfilled_count`,
  double-booked). Report it as one word — "checks passed" — or, on a violation,
  the plain-English violation only.
- **Node indices, objective-in-micros, solver status, λ internals.** (NOT the
  supply ids — the VIN / PO-line ref of the actual swap DOES belong in the change
  table; the planner allocates by them. Keep them out of the one-line *headline*,
  but always in the table.)
- **Internal vocabulary:** λ, Pareto frontier, weighted late-days, slushy/frozen
  fence, incumbent, mid-frontier default, arc, min-cost flow. Translate or omit.

## Steering contract (§6) — prompt compiles to parameters, NEVER code

Planner natural language → a typed override object (see
`xas_allocation/overrides_schema.json`). Same inputs + same override →
byte-identical plan. This object is the **flexibility surface**: you handle *any*
request by compiling it into `boosts` / `pins` / `forbid` / `lambda` / **`scope`**
/ **`time_scale`** — never by special-casing in prose. Your job is the
**translation**: resolve "Colmobil" → its customer_id, "these orders" → real keys
from the previous turn's change list, "next cycle"/"August" → a date or date
range, "just get the month roughly right" → `time_scale: months`, "hit the exact
dates" → `time_scale: days`. **Show the override object back to the planner before
running.**

`time_scale` (DECIDE-14) sets the resolution the solver reasons at — coarser
scales round every gap up to whole weeks/months and stop distinguishing smaller
differences, so the plan itself gets calmer (and durations read in that unit). It
changes the plan, not just the wording; the hard time fence stays in days. There
is no separate "how early is OK" knob — the earliness term (DECIDE-15) is always
on and gentle, and a coarse `time_scale` already absorbs sub-unit earliness.

Review gate:

| Request                                                  | Handling                          |
|----------------------------------------------------------|-----------------------------------|
| "delay SO-4471", "prefer Colmobil", "more churn", "allocate all Colmobil for August", "just fix this delay" | Runtime override (weights / pins / **scope**). Instant, safe. |
| "never split a dealer's vehicles across weeks"           | New **constraint** → **PR with tests**, not a live mutation. |

The prompt moves weights, pins, and scope; a human moves the model.

### Scope — work a slice, keep fixes local (§6)

`scope` is a general filter carried in the override:
`{customers?, models?, po?, from_date?, to_date?}`. **When a scope is present it
DEFINES the free set** — only rows matching *all* given dimensions may move;
everything else stays pinned. One mechanism, two everyday jobs:

- **Monthly / per-customer allocation:** "allocate all Colmobil orders for
  August" → `scope {customers:["CUST-001"], from_date:"2026-08-01",
  to_date:"2026-08-31"}`, then solve the slice.
- **Localized fix:** to fix one delay "without disrupting everything else",
  scope narrowly (a customer, a PO, a short window). Repair-not-rebuild already
  pins the rest; scope makes the bound explicit and auditable.

With no scope, the free set is the disrupted rows (the default repair).

### Bumping an untouched order — ASK first (§6, DECIDE-13)

By default the repair frees only disrupted rows, so an **untouched** order is
never displaced — a good vehicle it holds stays its. Sometimes the only way to
get a high-priority disrupted row on time is to **bump** an untouched
lower-priority row off its on-time vehicle. You must **never do this uninvited**:

1. Solve the plain repair first. If a high-priority row is still late, call
   `session.bump_candidates(snapshot, result)` — it lists the untouched,
   movable rows whose vehicle would rescue it, lowest priority first.
2. **Ask the planner explicitly** who may be bumped (show the candidates).
3. Compile their answer into the `bump` filter (same shape as scope, plus an
   `orders` list). The solver then frees exactly those rows and displaces one
   **only when it lowers total cost** — so it takes the low-priority,
   not-already-rescheduled target, never gratuitously.

Every bump is flagged in the change list (`— BUMPED …`), so a displacement is
never silent.

## Steering state — one combined override (§7)

Steering is a **single combined override object** (weights / pins / forbid /
lambda / scope / bump) — not a log. Each turn you *edit that one object* (add a
boost, tighten a scope, authorize a bump), show it back, and feed it + a fresh
pull into the solver. There is no ledger, no append-only history, no replay, no
TTL. The sandbox (loaded data, solver in memory) is a performance convenience
only: discard it, re-pull, re-apply the **same** override → the same plan. If
not, state has leaked — the bug.

The override is the only state that must survive a sandbox reclaim; recover it
from the last one you showed the planner. Because it is order-independent (a set
of accumulated instructions, not a sequence), there is no replay order to get
wrong.

DECIDE-5: durable, cross-session persistence of that override is a Managed Agents
platform concern and stays **deferred** — the real fix is a host-side store
(`web.py` keyed by session id, shipped in via the pull). In this prototype the
override lives only in the conversation. No audit trail of *who* steered *when*
is kept (the ledger carried that; deferred with persistence).

## Infeasibility (§9)

Never silently relax a pin. Prototype default (DECIDE-8): instruction pins run as
**high-cost soft constraints** so the solver always returns something; a violated
pin surfaces as a large cost line ("honouring this pin costs 3 extra changes —
proceed?"). CP-SAT assumption-literal minimal conflict sets are the honest
upgrade, deferred.

## Running the reference solver

```bash
uv run python -m scenario_engine.generate        # (re)fabricate data/pull.json + baseline
uv run python -m xas_allocation.flatten          # rich pull -> snapshot (sanity check)
uv run python -m xas_allocation.session          # full §8 loop over the repo dataset
uv run python -m xas_allocation.decisions        # dump every open DECIDE + default
PYTHONPATH=. uv run python tests/test_invariant.py   # determinism proof
```

The dataset is **no longer bundled in the skill**: `web.py` fetches the pull from
a callable source (`datasource.py`) at session start and mounts it into the
sandbox as a file. The skill carries only this SKILL.md + the solver package, so
regenerating `data/pull.json` no longer requires a re-deploy — only a change to
the solver package or this file does. `scenario_engine/`'s *code* still stays
OUTSIDE the sandbox; only its *output* travels in, now as a mounted file.

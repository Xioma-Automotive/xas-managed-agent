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

> `plan = pure_function(data_snapshot, skill, ledger)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is lost. That is the
bug to guard against everywhere. You do **not** decide allocations; you build the
network and call the solver, then explain the result.

Reference solver version pinned by this skill: **0.2.0-prototype**
(`xas_allocation/` package; DECIDE-9 — canonical version moves to a tested repo
before real dealer data).

## The data model (dates, XAS-shaped — DECIDE-7, `docs/xasdatamodel.md`)

Supply-first: `PO → PDN → Vehicle (pool)`, `Customer → SO`. A **Sales Order**
(one customer) groups **vehicle order rows**; the **row** is the allocatable
unit. Each row is allocated to a piece of **supply** that is one of two kinds: a
concrete **Vehicle** (a VIN) or a **PO-line slot** (a future car, keyed
`PO-model-row` e.g. `PO-150-1-5`). The pull is fabricated by `scenario_engine/`
(outside the agent) and `xas_allocation.flatten` maps it — **pure code, no
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
cost(o → u) = W(o) · tardiness_days(o,u)^1.5  +  λ(fence) · [date(u) ≠ promised_date(o)]

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
| time fence        | frozen / slushy / liquid → `λ` varies by horizon    |

Term placement matters: priority / prior-delays / aging build the **weight**
`W(o)`; the convex exponent shapes the **lateness term**; `λ` is a **separate
additive term**, untouched by weight escalation. `λ` is per-arc linear — the
problem stays a pure linear min-cost flow.

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
   command** verbatim to write `snapshot.json`. `flatten` (in
   `xas_allocation.flatten`) maps the rich pull into `orders/units/incumbent` —
   pure code; never re-shape the data by reasoning. This is the data-prep step;
   `xas_allocation.session.data_prep_flowchart` draws it as a mermaid flow chart.
3. **Detect discrepancies** (`session.find_discrepancies`): SO lines whose
   allocated vehicle now delivers past its `promised_date`. Map them for the
   planner **before** solving — this is what the disruption actually broke.
4. Apply the combined override from the ledger replay (§7), build the graph
   (§4), run the **λ sweep**.
5. **Self-check hard constraints:** no frozen/committed vehicle moved, no
   sales_model violation, every order has exactly one vehicle (or a surfaced
   backorder), no vehicle double-booked.
6. **Emit a reason-coded change list** — not a bare new plan. This is the hard
   part; spend the effort here. The internal line carries everything
   (`order SO-4471: 2026-09-01 → 2026-09-14 (promised 2026-09-07, 7d late);
   vehicle VEH-9a→VEH-9b; priority A, delayed 1× before`) — but that is the
   *source*, not the reply. Render it for the planner per **Planner-facing
   output** below.
7. Human approves → write back via MCP (approval-gated). Steering → append a new
   ledger entry → back to step 4.

## Planner-facing output — the reply the planner reads

The planner is a dealer-allocation scheduler, not an engineer. The reply's job
is to let them answer "did my instruction land, what moved, and what still needs
my attention" at a glance. Write **outcomes in business terms**; keep the
machinery in the sandbox.

**Lead with the outcome, in one or two lines.** What the instruction did (or
didn't do) and the headline: how many orders moved, how many are still late.
Answer the question they actually asked *first* — if they said "prefer Colmobil",
the first fact is what happened to Colmobil.

**Then a change summary — this is mandatory, never dropped.** The reason-coded
change list is the deliverable. Trimming jargon must never mean omitting the
allocation changes. Render every changed order as a table, in plain columns:

| Order   | Dealer (priority) | Was arriving | Now arrives    | Promised   | Result |
|---------|-------------------|--------------|----------------|------------|--------|
| SO-4001 | Colmobil (A)      | 2026-10-05   | **2026-08-24** | 2026-08-24 | ✅ on time |

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

- **The override JSON**, `customer_id`s (`CUST-001`), `weight_mult`, the ledger,
  "replay", "seed", "reproducible", "turn N". Confirm the *translation* in plain
  words ("prioritizing Colmobil over the other dealers") before running — not the
  raw object.
- **The λ sweep table when it's flat.** §2 keeps the sweep as the high-value
  computation, but a frontier where every row is identical is noise — collapse it
  to one sentence ("churn/lateness didn't trade off this cycle"). Show the table
  only when the rows genuinely differ and the planner has a point to pick.
- **Self-check field dumps** (`every_order_placed`, `unfilled_count`,
  double-booked). Report it as one word — "checks passed" — or, on a violation,
  the plain-English violation only.
- **Vehicle IDs** (`VEH-9001→VEH-9169`), node indices, objective-in-micros,
  solver status. A vehicle swap is real, but the ID means nothing to a planner;
  mention a physical vehicle only if they track VINs, and never in the headline.
- **Internal vocabulary:** λ, Pareto frontier, weighted late-days, slushy/frozen
  fence, incumbent, mid-frontier default, arc, min-cost flow. Translate or omit.

## Steering contract (§6) — prompt compiles to parameters, NEVER code

Planner natural language → a typed override object (see
`xas_allocation/overrides_schema.json`). Same inputs + same override →
byte-identical plan. This object is the **flexibility surface**: you handle *any*
request by compiling it into `boosts` / `pins` / `forbid` / `lambda` / **`scope`**
— never by special-casing in prose. Your job is the **translation**: resolve
"Colmobil" → its customer_id, "these orders" → real keys from the previous turn's
change list, "next cycle"/"August" → a date or date range. **Show the override
object back to the planner before running.**

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

## The override ledger (§7)

Ordered, timestamped, **append-only** list of override entries — the source of
truth (replayable, attributable). The sandbox (loaded data, solver in memory) is
a performance convenience only. Per turn: translate NL → entry, confirm, **replay
the whole ledger top-to-bottom** (skipping TTL-expired entries) → combined
override, feed that + a fresh pull into the solver. Discard the sandbox, replay
the ledger against a fresh pull → the same plan. If not, it's a leak — the bug.

DECIDE-5: the *statefulness* is a Managed Agents platform concern; this
ledger schema/replay/TTL/attribution is the application pattern built on top
(`xas_allocation/ledger.py`). Verify the platform session-persistence API before
wiring them together.

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
uv run python -m xas_allocation.session          # full §8 loop over the bundled dataset
uv run python -m xas_allocation.decisions        # dump every open DECIDE + default
PYTHONPATH=. uv run python tests/test_invariant.py   # determinism proof
```

`scenario_engine/` lives OUTSIDE the skill bundle — only its output
(`data/pull.json`) ships in and `flatten` reads it. Regenerate the data and you
must re-run `setup_allocation_agent.py`.

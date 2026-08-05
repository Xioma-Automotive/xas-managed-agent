---
name: xas-allocation
description: >-
  Repair a vehicle-to-order allocation after a disruption (delayed shipment,
  changed inbound, manual steering). Translate the situation + planner
  instructions into inputs for a deterministic min-cost-flow solver, run the λ
  sweep, self-check hard constraints, and emit a reason-coded change list. Use
  whenever a planner asks to re-allocate, defer/pin/boost orders, or explain an
  allocation change. Does NOT allocate by reasoning — it drives the reference
  solver.
---

# XAS allocation repair skill

**Core invariant — the whole design in one line:**

> `plan = pure_function(data_snapshot, skill, ledger)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is lost. That is the
bug to guard against everywhere. You do **not** decide allocations; you build the
network and call the solver, then explain the result.

Reference solver version pinned by this skill: **0.1.0-prototype**
(`xas_allocation/` package; DECIDE-9 — canonical version moves to a tested repo
before real dealer data).

## The cost model (verbatim — §2)

```
cost(o → u) = W(o) · tardiness(o,u)^1.5  +  λ(fence) · [week(u) ≠ promised_week(o)]

W(o) = w_base(o) · priority · (1 + α · n_prior_delays)   [+ β·days_backordered]
```

Encodings — every business factor maps to exactly one lever:

| Factor            | Encoding                                            |
|-------------------|-----------------------------------------------------|
| customer priority | multiplicative weight on W                           |
| delayed before    | `(1 + α·n_prior_delays)` multiplier — escalating     |
| back-order aging   | `β · days_backordered` (DECIDE-1: additive default) |
| don't recall      | HARD pin — unit removed from choice, no cost        |
| minimal changes   | `λ` step penalty on changed-week arcs               |
| convex lateness   | exponent `1.5` so delay never dumps on one order    |
| time fence        | frozen / slushy / liquid → `λ` varies by horizon    |

Term placement matters: priority / prior-delays / aging build the **weight**
`W(o)`; the convex exponent shapes the **lateness term**; `λ` is a **separate
additive term**, untouched by weight escalation. `λ` is per-arc linear — the
problem stays a pure linear min-cost flow.

**The λ sweep is the highest-value output.** Re-solve for λ ∈ {0,5,10,25,50,100}
(same network, only some arc costs change) and hand the planner a Pareto frontier
("12 changes → 340 weighted late-days" vs "31 → 210"), not one opaque answer.

**Time fence (DECIDE-2):** promised ≤2 wks = frozen (hard pin); 3–6 wks = slushy
(`λ` applies); beyond = liquid (week changes free).

## Solver (§3)

OR-Tools `SimpleMinCostFlow` — provably optimal, deterministic, ms-scale. NEVER
implement the algorithm; build the network and call the library. When orders
become **coupled** (fleet all-or-nothing, transport batching), flow structure
breaks — switch to CP-SAT + LNS as a **reviewed PR against the solver repo with
tests**, never as live-session code.

## Procedure (§8) — each turn

1. Confirm MCP liveness (DECIDE-6). *Prototype: synthetic generator, skipped.*
2. Pull orders / inbound / current allocation (DECIDE-7 — synthetic in prototype).
3. Reconcile spec compatibility: rule-driven `is_compatible(unit, order)`. The
   model handles only the residual ambiguity rules can't resolve, and **any such
   judgment MUST be written back to the residual cache** so a replay inherits it.
4. Build index tables + graph (§4), apply the combined override from the ledger
   replay (§7), run the **λ sweep**.
5. **Self-check hard constraints:** no frozen/committed unit moved, no spec
   violation, every order has exactly one unit (or a surfaced backorder).
6. **Emit a reason-coded change list** — not a bare new plan. This is the hard
   part; spend the effort here. The internal line carries everything
   (`order 4471: W32 → W34 (promised W33, 1w late); unit 9a→9b; priority A,
   delayed 1× before`) — but that is the *source*, not the reply. Render it for
   the planner per **Planner-facing output** below.
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

| Order | Dealer (priority) | Was arriving | Now arrives | Promised | Result |
|-------|-------------------|--------------|-------------|----------|--------|
| 4001  | Colmobil (A)      | W41          | **W38**     | W38      | ✅ on time |

Then a second table for orders **still late / unfilled** under a "needs your
call" heading — these are the decisions the planner owns. Close with the count
of unchanged orders (e.g. "The other 112 orders are unchanged") so nothing is
silently hidden. Sort changed orders most-improved first; put the priority-A
dealers and the order named in the instruction where they're easy to find.

**Surface the one thing they'd miss.** If the steering barely mattered (the
boosted dealer had one order in play), if a high-priority order is still late, if
a pin cost extra changes — say so in a single plain sentence. Don't bury it under
the table.

**Weeks:** show as `W38` (or `2026-W38`), consistently. **Late:** say "2 wks
late", not a tardiness number. **End** with the natural next steering options in
the planner's words ("defer the late Delek order", "protect Colmobil next
cycle") — not internal levers.

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
- **Unit IDs** (`9001→9169`), node indices, objective-in-micros, solver status.
  A unit swap is real, but the ID means nothing to a planner; mention a physical
  unit only if they track VINs, and never in the headline.
- **Internal vocabulary:** λ, Pareto frontier, weighted late-days, slushy/frozen
  fence, incumbent, mid-frontier default, arc, min-cost flow. Translate or omit.

## Steering contract (§6) — prompt compiles to parameters, NEVER code

Planner natural language → a typed override object (see
`xas_allocation/overrides_schema.json`). Same inputs + same override →
byte-identical plan. Your job is the **translation**: resolve "Colmobil" → its
customer_id, "these orders" → real keys from the previous turn's change list,
"next cycle" → a week label. **Show the override object back to the planner
before running.**

Review gate:

| Request                                        | Handling                          |
|------------------------------------------------|-----------------------------------|
| "delay 4471", "prefer Colmobil", "more churn"  | Runtime override. Instant, safe.  |
| "never split a dealer's units across weeks"    | New constraint → **PR with tests**, not a live mutation. |

The prompt moves weights and pins; a human moves the model.

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
uv run python -m xas_allocation.session          # full §8 loop over synthetic data
uv run python -m xas_allocation.decisions        # dump every open DECIDE + default
PYTHONPATH=. uv run python tests/test_invariant.py   # determinism proof
```

"""The per-turn session loop (§8), over the flattened date-based snapshot.

Each turn:
  1. (DECIDE-6) MCP liveness check — SKIPPED in the prototype (synthetic data).
  2. Pull the rich dataset and ``flatten`` it -> the orders/units/incumbent
     snapshot (pure code; see flatten.py). This IS the data-prep step the flow
     chart draws.
  3. Detect discrepancies: SO lines whose allocated vehicle now delivers after
     the promised date. Map them for the planner BEFORE solving.
  4. Replay the ledger -> combined override, run the λ sweep.
  5. Self-check hard constraints (§8.5).
  6. Emit a reason-coded CHANGE LIST (§8.6) — rendered planner-facing per the
     SKILL.md "Planner-facing output" section, not a bare new plan.
  7. Human approves -> write back. Steering -> new ledger entry -> back to 4.

The change list (step 6) is the genuinely hard part, per the spec. Eligibility
is a hard ``sales_model`` equality now — there is no LLM judgment in the data
path, so a residual cache is no longer needed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

from . import decisions as D
from .flatten import flatten_default
from .ledger import Ledger, LedgerEntry
from .snapshot import Order, Snapshot, add_days, date_label
from .solver import (
    SolveResult,
    SweepPoint,
    lambda_sweep,
    partition,
    solve,
    tardiness,
)


@dataclass
class CycleResult:
    combined_override: dict
    sweep: list[SweepPoint]
    results: dict[int, SolveResult]
    chosen_lambda: int
    chosen: SolveResult
    change_list: list[str]


# --- Discrepancy detection (step 3 — the "map") ------------------------------


@dataclass
class Discrepancy:
    order_id: str
    customer: str
    priority: str
    sales_model: str
    vehicle_id: str
    promised: date
    now_arriving: date
    days_late: int


def find_discrepancies(snapshot: Snapshot) -> list[Discrepancy]:
    """SO lines whose currently-allocated vehicle now delivers past the promise.

    This is what the disruption actually broke — computed straight from the
    snapshot (allocated vehicle's planned_delivery_date vs promised_date), not
    read from the manifest, so it stays true even under manual steering."""
    orders = snapshot.order_by_id()
    units = snapshot.unit_by_id()
    out: list[Discrepancy] = []
    for oid, uid in snapshot.incumbent.items():
        o, u = orders[oid], units[uid]
        late = tardiness(o, u)
        if late > 0:
            out.append(
                Discrepancy(
                    order_id=oid,
                    customer=o.customer,
                    priority=o.priority,
                    sales_model=o.sales_model,
                    vehicle_id=uid,
                    promised=o.promised_date,
                    now_arriving=u.planned_delivery_date,
                    days_late=late,
                )
            )
    out.sort(key=lambda d: (-d.days_late, d.order_id))
    return out


def format_discrepancies(discs: list[Discrepancy]) -> str:
    if not discs:
        return "No discrepancies: every allocated vehicle still meets its promised date."
    lines = [
        f"{len(discs)} order(s) now late because their allocated vehicle slipped:",
        "  order    | customer            | model  | promised   | now arriving | late",
        "  ---------+---------------------+--------+------------+--------------+------",
    ]
    for d in discs:
        lines.append(
            f"  {d.order_id:<8} | {d.customer:<19} | {d.sales_model:<6} | "
            f"{date_label(d.promised)} | {date_label(d.now_arriving)}   | {d.days_late}d"
        )
    return "\n".join(lines)


# --- Data-prep flow chart (the "flow chart" the planner asked for) -----------


def data_prep_flowchart(snapshot: Snapshot) -> str:
    """Mermaid of how the rich pull became solver inputs — the data-prep hop.

    Not the allocation; the *pipeline*: rich PDN/Vehicle/SO -> flatten/freeze ->
    the three arrays -> the sparse sales_model arcs -> the solver."""
    n_orders = len(snapshot.orders)
    n_units = len(snapshot.units)
    n_incumbent = len(snapshot.incumbent)
    n_veh = sum(1 for u in snapshot.units if u.kind == "vehicle")
    n_slot = n_units - n_veh
    n_models = len({o.sales_model for o in snapshot.orders})
    d = snapshot.disruption
    n_disrupted = len(d.get("disrupted_orders", []))
    return "\n".join(
        [
            "```mermaid",
            "flowchart LR",
            (
                f'  SRC["rich pull<br/>PO→PDN→Vehicle · Customer→SO<br/>'
                f'{n_orders} SO rows · {n_veh} vehicles + {n_slot} PO slots"]'
            ),
            (
                f'  DIS["disruption<br/>PO {d.get("po", "?")} +{d.get("delay_days", 0)}d<br/>'
                f'{n_disrupted} rows freed"]'
            ),
            '  FL["flatten + freeze<br/>(pure code, no model judgment)"]',
            f'  ORD["orders[]<br/>{n_orders} rows"]',
            f'  UNI["units[]<br/>{n_units} rows"]',
            f'  INC["incumbent[]<br/>{n_incumbent} pairs"]',
            (
                f'  ARC["eligibility arcs<br/>sales_model equality · {n_models} models<br/>'
                '(computed, never stored)"]'
            ),
            '  SOLVE["min-cost-flow<br/>λ sweep"]',
            "  SRC --> FL",
            "  DIS --> FL",
            "  FL --> ORD",
            "  FL --> UNI",
            "  FL --> INC",
            "  ORD --> ARC",
            "  UNI --> ARC",
            "  INC --> SOLVE",
            "  ARC --> SOLVE",
            "```",
        ]
    )


# --- Solve + change list -----------------------------------------------------


def _choose_lambda(combined: dict, sweep: list[SweepPoint]) -> int:
    """Pick the λ to present as 'the' plan. Ledger λ wins if set; otherwise a
    mid-frontier default so the headline plan is neither max-churn nor max-late.
    The whole frontier is always shown regardless — the planner picks a point."""
    if combined.get("lambda") is not None:
        return int(combined["lambda"])
    mid = D.LAMBDA_SWEEP[len(D.LAMBDA_SWEEP) // 2]
    return int(mid)


def build_change_list(
    snapshot: Snapshot,
    result: SolveResult,
    ledger: Ledger,
    current_date: date,
) -> list[str]:
    """Reason-coded diff of the chosen plan vs the incumbent (§8.6)."""
    orders = snapshot.order_by_id()
    units = snapshot.unit_by_id()
    rp = partition(snapshot, ledger.replay(current_date))
    boosts = rp.boosts
    lines: list[str] = []

    for oid in sorted(orders):
        o = orders[oid]
        old_uid = snapshot.incumbent.get(oid)
        new_uid = result.plan.get(oid)

        if oid in result.unfilled:
            reasons = _order_reasons(o, ledger, current_date, boosts)
            lines.append(f"order {oid}: UNFILLED (backorder) — no compatible vehicle; {reasons}")
            continue
        if new_uid == old_uid:
            continue  # unchanged, don't clutter the list

        old_date = date_label(units[old_uid].planned_delivery_date) if old_uid else "unassigned"
        new_date = units[new_uid].planned_delivery_date
        promised = date_label(o.promised_date)
        late = tardiness(o, units[new_uid])
        timing = "on time" if late == 0 else f"{late}d late"

        reasons = _order_reasons(o, ledger, current_date, boosts)
        src = units[new_uid].pdn or units[new_uid].po_ref or units[new_uid].kind
        moved = (
            f"vehicle {new_uid} off {src}" if old_uid is None else f"vehicle {old_uid}→{new_uid}"
        )
        lines.append(
            f"order {oid}: {old_date} → {date_label(new_date)} (promised {promised}, {timing}); "
            f"{moved}; {reasons}"
        )
    return lines


def _order_reasons(o: Order, ledger: Ledger, current_date: date, boosts: dict) -> str:
    """The 'why' half of a change line: data factors + ledger attribution."""
    bits = [f"priority {o.priority}"]
    if o.n_prior_delays:
        bits.append(f"delayed {o.n_prior_delays}× before")
    if o.times_rescheduled:
        bits.append(f"rescheduled {o.times_rescheduled}× by us — protected")
    if o.days_backordered:
        bits.append(f"back-ordered {o.days_backordered}d")
    if o.customer_id in boosts and boosts[o.customer_id] != 1.0:
        bits.append(f"boosted ×{boosts[o.customer_id]:g} ({o.customer})")
    trail = ledger.who_touched(o.order_id, current_date)
    if trail:
        bits.append("steered: " + "; ".join(trail))
    return ", ".join(bits)


def format_sweep(sweep: list[SweepPoint]) -> str:
    lines = [
        "λ sweep (Pareto frontier — changes vs weighted late-days):",
        "   λ  | changes | weighted-late-days | unfilled",
        "  ----+---------+--------------------+---------",
    ]
    for p in sweep:
        lines.append(
            f"  {p.lam:>3} | {p.n_changes:>7} | {p.weighted_late_days:>18} | {p.unfilled:>8}"
        )
    return "\n".join(lines)


def run_cycle(
    snapshot: Snapshot,
    ledger: Ledger,
    current_date: date | None = None,
) -> CycleResult:
    """One deterministic turn: replay ledger -> sweep -> choose -> change list."""
    current_date = current_date or snapshot.now
    combined = ledger.replay(current_date)
    sweep, results = lambda_sweep(snapshot, combined)
    chosen_lambda = _choose_lambda(combined, sweep)
    if chosen_lambda not in results:
        # λ outside the sweep grid: solve it explicitly so the ledger value holds.
        results[chosen_lambda] = solve(snapshot, combined, lam=chosen_lambda)
    chosen = results[chosen_lambda]
    changes = build_change_list(snapshot, chosen, ledger, current_date)
    return CycleResult(combined, sweep, results, chosen_lambda, chosen, changes)


def _print_cycle(title: str, snapshot: Snapshot, cyc: CycleResult) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    print(format_sweep(cyc.sweep))
    print(
        f"\nchosen λ = {cyc.chosen_lambda}   "
        f"(changes={cyc.chosen.n_changes}, "
        f"weighted-late-days={cyc.chosen.weighted_late_days}, "
        f"unfilled={len(cyc.chosen.unfilled)})"
    )
    sc = cyc.chosen.self_check
    print(f"self-check: {'OK' if sc['ok'] else 'VIOLATIONS: ' + '; '.join(sc['violations'])}")
    print("\nchange list (reason-coded):")
    if not cyc.change_list:
        print("  (no changes vs incumbent)")
    for line in cyc.change_list:
        print(f"  • {line}")


def main() -> None:
    ap = argparse.ArgumentParser(description="XAS allocation session demo (date-based).")
    ap.add_argument("--ledger", default=None, help="path to persist the ledger JSON")
    args = ap.parse_args()

    print(D.format_decisions())
    print(f"\nsolver version: {D.SOLVER_VERSION}")

    # Steps 1-2: liveness (skipped) then pull + flatten the bundled dataset.
    snap = flatten_default()
    d = snap.disruption
    print(
        f"\nsnapshot now={date_label(snap.now)}: {len(snap.orders)} orders, "
        f"{len(snap.units)} supply items. Disruption: PO {d.get('po')} delayed "
        f"{d.get('delay_days')}d, {len(d.get('disrupted_orders', []))} orders to repair."
    )

    # Step 3: the discrepancy map + the data-prep flow chart.
    print("\n" + format_discrepancies(find_discrepancies(snap)))
    print("\ndata-prep flow chart:\n" + data_prep_flowchart(snap))

    ledger = Ledger.load(args.ledger)

    # --- Turn 1: base repair, empty ledger --------------------------------
    cyc1 = run_cycle(snap, ledger)
    _print_cycle("TURN 1 — base repair (no steering)", snap, cyc1)

    # --- Turn 2: planner steers. Defer one disrupted order, prefer Colmobil.
    disrupted = d.get("disrupted_orders", [])
    steer_order = disrupted[0] if disrupted else min(o.order_id for o in snap.orders)
    not_before = date_label(add_days(snap.now, 45))
    override = {
        "pins": [{"order": steer_order, "action": "defer", "not_before": not_before}],
        "boosts": [{"customer": "CUST-001", "weight_mult": 3.0}],  # Colmobil
        "lambda": 25,
        "ttl": None,
    }
    print(
        f"\n>>> planner steering (turn 2): defer {steer_order} to ≥{not_before}, prefer Colmobil ×3, λ=25"
    )
    print(f">>> override object shown back before running:\n    {override}")
    ledger.append(
        LedgerEntry(
            turn=ledger.next_turn(),
            author="Olga",
            override=override,
            timestamp="2026-08-04T09:15:00Z",  # fixed for reproducibility
            ttl=None,
        )
    )
    cyc2 = run_cycle(snap, ledger)
    _print_cycle("TURN 2 — after steering", snap, cyc2)

    # --- Turn 3: scope. "Allocate all of one dealer's rows" — the scope DEFINES
    #     the free set, so only that dealer's rows move; the rest stay pinned.
    scope_cid = snap.orders[0].customer_id
    scope_override = {
        "pins": [],
        "boosts": [],
        "forbid": [],
        "lambda": None,
        "scope": {"customers": [scope_cid]},
        "ttl": None,
    }
    print(f"\n>>> planner steering (turn 3): scope to customer {scope_cid} (work only that slice)")
    print(f">>> override object shown back before running:\n    {scope_override}")
    ledger.append(
        LedgerEntry(
            turn=ledger.next_turn(),
            author="Olga",
            override=scope_override,
            timestamp="2026-08-04T10:00:00Z",
            ttl=None,
        )
    )
    cyc3 = run_cycle(snap, ledger)
    _print_cycle(f"TURN 3 — scoped to {scope_cid}", snap, cyc3)

    print(
        "\n(ledger is the session: replay it against a fresh pull to reproduce "
        "this exact plan — see tests/test_invariant.py)"
    )


if __name__ == "__main__":
    main()

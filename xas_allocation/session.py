"""The per-turn session loop (§8), wired over synthetic data.

Each turn:
  1. (DECIDE-6) MCP liveness check — SKIPPED in the prototype (synthetic data);
     wired to a directory_tree probe once xas-code MCP is connected.
  2. Pull orders / inbound / incumbent — synthetic generator (DECIDE-7).
  3. Reconcile spec compatibility — rule-driven, with the LLM residual hook and
     cached-decision write-back handled inside the solver's arc build (§8.3).
  4. Replay the ledger -> combined override, build the graph, run the λ sweep.
  5. Self-check hard constraints (§8.5).
  6. Emit a CHANGE LIST with reason codes (§8.6) — not a bare new plan.
  7. Human approves -> write back (synthetic no-op here). Steering -> new ledger
     entry -> back to step 4.

The change list (step 6) is the genuinely hard part, per the spec — 12 date
changes each with a one-line justification get accepted where 12 bare changes
get rejected. That's where the design effort goes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import decisions as D
from .ledger import Ledger, LedgerEntry
from .solver import (
    NOW_WEEK,
    SolveResult,
    SweepPoint,
    effective_weight,
    lambda_sweep,
    partition,
)
from .spec_match import ResidualCache
from .synth_data import Snapshot, generate_snapshot, week_label


@dataclass
class CycleResult:
    combined_override: dict
    sweep: list[SweepPoint]
    results: dict[int, SolveResult]
    chosen_lambda: int
    chosen: SolveResult
    change_list: list[str]


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
    current_week: int,
) -> list[str]:
    """Reason-coded diff of the chosen plan vs the incumbent (§8.6)."""
    orders = snapshot.order_by_id()
    units = snapshot.unit_by_id()
    rp = partition(snapshot, ledger.replay(current_week))
    boosts = rp.boosts
    lines: list[str] = []

    for oid in sorted(orders):
        o = orders[oid]
        old_uid = snapshot.incumbent.get(oid)
        new_uid = result.plan.get(oid)

        if oid in result.unfilled:
            reasons = _order_reasons(o, ledger, current_week, boosts)
            lines.append(
                f"order {oid}: UNFILLED (backorder) — no compatible unit; {reasons}"
            )
            continue
        if new_uid == old_uid:
            continue  # unchanged, don't clutter the list

        old_week = units[old_uid].arrival_week if old_uid is not None else None
        new_week = units[new_uid].arrival_week
        old_lbl = week_label(old_week) if old_week is not None else "unassigned"
        promised = week_label(o.promised_week)
        late = max(0, new_week - o.promised_week)
        timing = "on time" if late == 0 else f"{late}w late"

        reasons = _order_reasons(o, ledger, current_week, boosts)
        moved_unit = (
            f"unit {new_uid} off {units[new_uid].shipment}"
            if old_uid is None
            else f"unit {old_uid}→{new_uid}"
        )
        lines.append(
            f"order {oid}: {old_lbl} → {week_label(new_week)} (promised {promised}, {timing}); "
            f"{moved_unit}; {reasons}"
        )
    return lines


def _order_reasons(o, ledger: Ledger, current_week: int, boosts: dict) -> str:
    """The 'why' half of a change line: data factors + ledger attribution."""
    bits = [f"priority {o.priority}"]
    if o.n_prior_delays:
        bits.append(f"delayed {o.n_prior_delays}× before")
    if o.days_backordered:
        bits.append(f"back-ordered {o.days_backordered}d")
    if o.customer_id in boosts and boosts[o.customer_id] != 1.0:
        bits.append(f"boosted ×{boosts[o.customer_id]:g} ({o.customer})")
    trail = ledger.who_touched(o.order_id, current_week)
    if trail:
        bits.append("steered: " + "; ".join(trail))
    return ", ".join(bits)


def format_sweep(sweep: list[SweepPoint]) -> str:
    lines = ["λ sweep (Pareto frontier — changes vs weighted late-days):",
             "   λ  | changes | weighted-late-days | unfilled",
             "  ----+---------+--------------------+---------"]
    for p in sweep:
        lines.append(
            f"  {p.lam:>3} | {p.n_changes:>7} | {p.weighted_late_days:>18} | {p.unfilled:>8}"
        )
    return "\n".join(lines)


def run_cycle(
    snapshot: Snapshot,
    ledger: Ledger,
    cache: Optional[ResidualCache] = None,
    current_week: int = NOW_WEEK,
) -> CycleResult:
    """One deterministic turn: replay ledger -> sweep -> choose -> change list."""
    cache = cache or ResidualCache.load(None)
    combined = ledger.replay(current_week)
    sweep, results = lambda_sweep(snapshot, combined, cache)
    chosen_lambda = _choose_lambda(combined, sweep)
    if chosen_lambda not in results:
        # λ outside the sweep grid: solve it explicitly so the ledger value holds.
        from .solver import solve
        results[chosen_lambda] = solve(snapshot, combined, cache, lam=chosen_lambda)
    chosen = results[chosen_lambda]
    changes = build_change_list(snapshot, chosen, ledger, current_week)
    return CycleResult(combined, sweep, results, chosen_lambda, chosen, changes)


def _print_cycle(title: str, snapshot: Snapshot, cyc: CycleResult) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    print(format_sweep(cyc.sweep))
    print(f"\nchosen λ = {cyc.chosen_lambda}   "
          f"(changes={cyc.chosen.n_changes}, "
          f"weighted-late-days={cyc.chosen.weighted_late_days}, "
          f"unfilled={len(cyc.chosen.unfilled)})")
    sc = cyc.chosen.self_check
    print(f"self-check: {'OK' if sc['ok'] else 'VIOLATIONS: ' + '; '.join(sc['violations'])}")
    print("\nchange list (reason-coded):")
    if not cyc.change_list:
        print("  (no changes vs incumbent)")
    for line in cyc.change_list:
        print(f"  • {line}")


def main() -> None:
    ap = argparse.ArgumentParser(description="XAS allocation session demo (synthetic).")
    ap.add_argument("--seed", type=int, default=20)
    ap.add_argument("--spare-ratio", type=float, default=0.2,
                    help="inbound spare units per order; lower = more contention "
                         "so the λ sweep trades off visibly")
    ap.add_argument("--delay-weeks", type=int, default=2)
    ap.add_argument("--ledger", default=None, help="path to persist the ledger JSON")
    args = ap.parse_args()

    print(D.format_decisions())
    print(f"\nsolver version: {D.SOLVER_VERSION}")

    # Step 1: liveness check — skipped (synthetic). Step 2: pull.
    snap = generate_snapshot(
        seed=args.seed, spare_ratio=args.spare_ratio, delay_weeks=args.delay_weeks
    )
    d = snap.disruption
    print(
        f"\nsnapshot seed={snap.seed}: {len(snap.orders)} orders, {len(snap.units)} units. "
        f"Disruption: shipment {d['shipment']} delayed {d['delay_weeks']}w, "
        f"{len(d['disrupted_orders'])} orders to repair: {d['disrupted_orders']}"
    )

    ledger = Ledger.load(args.ledger)
    cache = ResidualCache.load(None)

    # --- Turn 1: base repair, empty ledger --------------------------------
    cyc1 = run_cycle(snap, ledger, cache)
    _print_cycle("TURN 1 — base repair (no steering)", snap, cyc1)

    # --- Turn 2: planner steers. Defer one disrupted order past the delay,
    #     and prefer Colmobil. NL -> override object -> ledger entry (§6/§7).
    steer_order = d["disrupted_orders"][0] if d["disrupted_orders"] else sorted(
        o.order_id for o in snap.orders
    )[0]
    override = {
        "pins": [{"order": steer_order, "action": "defer", "not_before": "2026-W38"}],
        "boosts": [{"customer": "CUST-001", "weight_mult": 3.0}],  # Colmobil
        "lambda": 25,
        "ttl": None,
    }
    print(f"\n>>> planner steering (turn 2): defer {steer_order} to ≥2026-W38, "
          f"prefer Colmobil ×3, λ=25")
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
    cyc2 = run_cycle(snap, ledger, cache)
    _print_cycle("TURN 2 — after steering", snap, cyc2)

    print("\n(ledger is the session: replay it against a fresh pull to reproduce "
          "this exact plan — see tests/test_invariant.py)")


if __name__ == "__main__":
    main()

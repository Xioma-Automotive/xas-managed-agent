"""The per-turn session loop (§8), over the flattened date-based snapshot.

Each turn (there is no liveness-check step — DECIDE-6 settled as NOT APPLICABLE,
since the pull happens host-side before the session exists):
  1. Pull the rich dataset and ``flatten`` it -> the orders/units/incumbent
     snapshot (pure code; see flatten.py).
  2. Map the discrepancies — which orders the disruption broke, and **which of
     them are even repairable** vs locked-in — BEFORE solving (`discrepancy_report`).
  3. Solve with the current combined **override** (weights / pins / scope / bump).
  4. Emit the finished, planner-facing report (`planner_report`) — a plain table,
     no solver internals.

**Steering is a single combined override object** the agent carries forward and
shows each turn — there is no ledger/replay/TTL. The invariant holds as
`plan = pure_function(data_snapshot, skill, override)`: same snapshot + same
override → byte-identical plan. (Durable, cross-session persistence of that
override is a platform concern, deferred — DECIDE-5.)

The output helpers are the sanctioned way to talk to the planner: call
``discrepancy_report`` / ``repair_and_report`` and print them. Do NOT re-derive
the solver's result by hand.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import decisions as D
from .flatten import flatten_default
from .snapshot import Order, Snapshot, date_label
from .solver import (
    SolveResult,
    SweepPoint,
    disrupted_order_keys,
    is_locked_in,
    lambda_sweep,
    repairability,
    scale_units,
    solve,
    tardiness,
    time_scale_of,
)


@dataclass
class CycleResult:
    override: dict
    sweep: list[SweepPoint]
    results: dict[int, SolveResult]
    chosen_lambda: int
    chosen: SolveResult


# --- Discrepancy map (step 3): what broke, and what's even fixable -----------


@dataclass
class Discrepancy:
    order_key: str
    customer: str
    priority: str
    sales_model: str
    vehicle_id: str
    promised: date
    now_arriving: date
    days_late: int
    fixable: bool  # can it be re-slotted at all?
    reason: str  # "movable" | "frozen"


def find_discrepancies(snapshot: Snapshot) -> list[Discrepancy]:
    """Orders whose currently-allocated supply now delivers past the promise, each
    classified fixable vs stuck (only the frozen fence is truly stuck now) so the
    planner learns on turn 1 which broken orders can't be helped by re-allocation."""
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    out: list[Discrepancy] = []
    for oid, uid in snapshot.incumbent.items():
        o, u = orders[oid], units[uid]
        late = tardiness(o, u)
        if late > 0:
            reason = repairability(o, snapshot.now, u)
            out.append(
                Discrepancy(
                    order_key=oid,
                    customer=o.customer,
                    priority=o.priority,
                    sales_model=o.sales_model,
                    vehicle_id=uid,
                    promised=o.delivery_date,
                    now_arriving=u.eta_dealer,
                    days_late=late,
                    fixable=(reason == "movable"),
                    reason=reason,
                )
            )
    out.sort(key=lambda d: (d.fixable, -d.days_late, d.order_key))
    return out


# What the pull filtered out, in the planner's words. The keys are the reasons
# `datasource.map_response` (host-side) and `flatten` (here) count; each has one
# plain-English phrase, because the reply must never print a reason code.
DROP_PHRASES = {
    "no_model_on_the_order": "no model on the order",
    "no_promised_date": "no promised date",
    "order_without_a_promised_date": "no promised date",
    "order_line_without_a_model": "no model on the order",
    "vehicle_without_a_model": "no model on the car",
    "vehicle_without_an_arrival_date": "no arrival date on the car",
    "allocation_to_a_dropped_vehicle": "allocated to a car that is out of scope",
}


def _phrase(reason: str) -> str:
    return DROP_PHRASES.get(reason, reason.replace("_", " "))


def exclusion_note(snapshot: Snapshot) -> str:
    """The sales orders and cars that are NOT in this plan, and why.

    Mandatory on turn 1, BEFORE the discrepancy map. Real data is patchy — a
    sales order with no model on it has nothing to match a car against, so it
    cannot be planned — and a plan covering three of twenty-five orders that
    does not say so reads as the whole book. Empty string when the pull filtered
    nothing (the fabricated dataset), so this costs nothing there.
    """
    excluded = dict((snapshot.meta or {}).get("excluded") or {})
    conflicts = (snapshot.meta or {}).get("conflicts") or []
    drops: Counter[str] = Counter()
    for bucket in ("order_drops", "flatten_skips"):
        for reason, n in (excluded.get(bucket) or {}).items():
            drops[_phrase(reason)] += n

    lines: list[str] = []
    # A field the source does not return at all is not a data problem the planner
    # can fix by completing an order — say so, or they will go looking in the app
    # for something that is missing in the plumbing.
    gaps = (snapshot.meta or {}).get("projection_gaps") or {}
    if gaps:
        lines.append(
            "**The system is not returning some of the fields this needs** — "
            f"{', '.join(sorted({n for names in gaps.values() for n in names}))}. "
            "Until that is fixed the plan below can only cover part of the book, "
            "and it is not something you can correct on the orders themselves."
        )
    seen, kept = excluded.get("orders_seen"), excluded.get("orders_kept")
    if drops and seen:
        why = ", ".join(f"{n} with {phrase}" for phrase, n in sorted(drops.items()))
        # With a projection gap the reasons below are ARTEFACTS of it — every order
        # looks incomplete because the field never arrived. Telling the planner to
        # go complete 25 orders would send them after work that is already done.
        cause = (
            "That is the missing fields above, not the orders themselves — these "
            "counts mean nothing until the system returns them."
            if gaps
            else "They need completing in the system before they can be allocated."
        )
        lines.append(
            f"**{seen - (kept or 0)} of {seen} sales orders are not in this plan** — {why}. {cause}"
        )
    for c in conflicts:
        lines.append(
            f"Car {c['vehicle']} is allocated to {len(c['orders'])} orders at once "
            f"({', '.join(c['orders'])}); none of them is treated as allocated here."
        )
    no_car = excluded.get("orders_with_no_eligible_car") or []
    if no_car:
        shown = ", ".join(no_car[:6]) + ("…" if len(no_car) > 6 else "")
        lines.append(
            f"{len(no_car)} order(s) have no compatible car in stock or on order: {shown}."
        )
    units_seen, units_kept = excluded.get("units_seen"), excluded.get("units_kept")
    if units_seen and units_kept is not None:
        lines.append(
            f"Car pool: {units_kept} of {units_seen} cars in stock or on order match "
            "something someone has ordered."
        )
    return "\n\n".join(lines)


def discrepancy_report(snapshot: Snapshot, override: dict | None = None) -> str:
    """Turn-1 planner-facing map: what broke, split into fixable vs locked-in.

    ``override`` is optional and used only to speak durations in the active
    time-scale (DECIDE-14); the discrepancy set itself is scale-independent."""
    discs = find_discrepancies(snapshot)
    note = exclusion_note(snapshot)
    if not discs:
        clear = "No orders are late — every allocated car still meets its promised date."
        return f"{note}\n\n{clear}" if note else clear
    scale, unit_days = time_scale_of(override)
    stuck = [x for x in discs if not x.fixable]
    movable = [x for x in discs if x.fixable]

    lines = [note] if note else []
    lines.append(f"**A supply delay pushed {len(discs)} order(s) past their promised date.**")
    if stuck:
        lines.append(
            f"\n**{len(stuck)} locked in** — too close to delivery to re-slot; they will stay "
            "late unless the delivery itself is expedited (a call to those dealers, not a "
            "re-allocation):\n"
        )
        lines.append("| Order | Dealer (priority) | Promised | Now arriving | Late | Why |")
        lines.append("|---|---|---|---|---|---|")
        for x in stuck:
            why = "locked in (near delivery)" if x.reason == "frozen" else "already in final prep"
            lines.append(
                f"| {x.order_key} | {x.customer} ({x.priority}) "
                f"| {date_label(x.promised)} | {date_label(x.now_arriving)} "
                f"| {_dur(x.days_late, scale, unit_days)} | {why} |"
            )
    if movable:
        lines.append(
            f"\n**{len(movable)} can be repaired** — a re-allocation may get these back on track:\n"
        )
        lines.append("| Order | Dealer (priority) | Promised | Now arriving | Late |")
        lines.append("|---|---|---|---|---|")
        for x in movable:
            lines.append(
                f"| {x.order_key} | {x.customer} ({x.priority}) "
                f"| {date_label(x.promised)} | {date_label(x.now_arriving)} "
                f"| {_dur(x.days_late, scale, unit_days)} |"
            )
    return "\n".join(lines)


# --- Bump candidates (DECIDE-13: the agent ASKS before displacing anyone) -----


def bump_candidates(snapshot: Snapshot, result: SolveResult) -> list[dict]:
    """Untouched rows the planner *could* authorize bumping to rescue a still-late
    disrupted row — so the agent asks with a concrete list, lowest priority first."""
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    disrupted = disrupted_order_keys(snapshot)
    prio_rank = {"C": 0, "B": 1, "A": 2}

    # A rescue target that is LOCKED IN cannot accept a different car, so offering
    # a bump for it wastes the planner's authorization: the freed car sits idle and
    # the target stays on its late one. Observed 2026-08-25 — three authorized
    # bumps all no-oped for exactly this reason.
    still_late = [
        oid
        for oid in disrupted
        if result.plan.get(oid)
        and tardiness(orders[oid], units[result.plan[oid]]) > 0
        and not is_locked_in(orders[oid], snapshot.now, units.get(snapshot.incumbent.get(oid, "")))
    ]
    cands: dict[str, dict] = {}
    for lid in still_late:
        lo = orders[lid]
        for oid, o in orders.items():
            if oid in disrupted or oid in cands:
                continue
            uid = snapshot.incumbent.get(oid)
            if not uid:
                continue
            u = units[uid]
            if u.sales_model != lo.sales_model:
                continue
            if u.eta_dealer <= lo.delivery_date:
                cands[oid] = {
                    "row": oid,
                    "customer": o.customer,
                    "priority": o.priority,
                    "vehicle": uid,
                    "arrives": date_label(u.eta_dealer),
                    "would_rescue": lid,
                    "rescue_customer": lo.customer,
                }
    return sorted(cands.values(), key=lambda c: (prio_rank.get(c["priority"], 9), c["row"]))


# --- Data-prep flow chart ----------------------------------------------------


def data_prep_flowchart(snapshot: Snapshot) -> str:
    """Mermaid of how the rich pull became solver inputs — the data-prep hop."""
    n_orders = len(snapshot.orders)
    n_units = len(snapshot.units)
    n_incumbent = len(snapshot.incumbent)
    n_real = sum(1 for u in snapshot.units if u.is_hard)
    n_future = n_units - n_real
    n_models = len({o.sales_model for o in snapshot.orders})
    d = snapshot.disruption
    n_disrupted = len(d.get("disrupted_orders", []))
    n_delayed = len(d.get("delayed_vehicles", []))
    return "\n".join(
        [
            "```mermaid",
            "flowchart LR",
            (
                f'  SRC["rich pull<br/>VSO jobcards · vehicle pool<br/>'
                f'{n_orders} car lines · {n_real} real + {n_future} future"]'
            ),
            (
                f'  DIS["disruption<br/>{n_delayed} vehicles '
                f"{d.get('delay_label') or str(d.get('delay_days', 0)) + ' days'}<br/>"
                f'{n_disrupted} orders freed"]'
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


# --- Solve ------------------------------------------------------------------


def run_cycle(snapshot: Snapshot, override: dict | None = None) -> CycleResult:
    """One deterministic turn: sweep λ over the combined override, then choose the
    λ to present as 'the' plan — the override's if it set one, else the middle of
    the sweep (neither max-churn nor max-late). Only an off-sweep override λ needs
    a re-solve."""
    override = override or {}
    sweep, results = lambda_sweep(snapshot, override)
    # `or` would swallow a deliberate lambda=0 (the first sweep value), so this
    # tests for absence, not falsiness.
    steered = override.get("lambda")
    lam = int(steered) if steered is not None else D.LAMBDA_SWEEP[len(D.LAMBDA_SWEEP) // 2]
    if lam not in results:
        results[lam] = solve(snapshot, override, lam=lam)
    return CycleResult(override, sweep, results, lam, results[lam])


# --- Planner-facing report (the finished reply) ------------------------------

# The solver PRICES all earliness (DECIDE-15); the report only *mentions* it when
# it's notable, so a couple of days early isn't nagged about. Display threshold,
# not a solver knob.
EARLY_FLAG_DAYS = 14

_UNIT_LABEL = {"days": "day", "weeks": "week", "months": "month"}


def _dur(days: int, scale: str, unit_days: int) -> str:
    """A duration rendered in the active unit, rounding up (DECIDE-14)."""
    if unit_days <= 1:
        return f"{days} day" + ("" if days == 1 else "s")
    n = scale_units(days, unit_days)
    label = _UNIT_LABEL.get(scale, "unit")
    return f"{n} {label}" + ("" if n == 1 else "s")


def _cid_to_name(snapshot: Snapshot) -> dict[str, str]:
    """customer_id -> display name; first order wins, so it is stable."""
    return {o.customer_id: o.customer for o in reversed(snapshot.orders)}


def _steering_summary(override: dict, cid_to_name: dict[str, str]) -> str:
    if not override:
        return "default repair"
    parts: list[str] = []
    boosts = override.get("boosts") or []
    if boosts:
        names = ", ".join(
            cid_to_name.get(b.get("customer", ""), b.get("customer", "")) for b in boosts
        )
        parts.append(f"prioritized {names}")
    defers = [p for p in (override.get("pins") or []) if p.get("action") == "defer"]
    if defers:
        parts.append("deferred " + ", ".join(str(p.get("order")) for p in defers))
    forbids = [f for f in (override.get("forbid") or []) if f.get("action") == "no_move"]
    if forbids:
        parts.append("locked " + ", ".join(str(f.get("order")) for f in forbids))
    if override.get("scope"):
        parts.append("working only a selected slice")
    if override.get("bump"):
        b = override["bump"]
        who = (
            ", ".join(cid_to_name.get(c, c) for c in (b.get("customers") or []))
            or ", ".join(b.get("orders") or [])
            or "selected orders"
        )
        parts.append(f"allowed bumping {who}")
    return "; ".join(parts) if parts else "default repair"


def _result_phrase(order: Order, unit, scale: str, unit_days: int) -> str:
    late = tardiness(order, unit)
    if late > 0:
        return f"{_dur(late, scale, unit_days)} late"
    early = (order.delivery_date - unit.eta_dealer).days
    if early > EARLY_FLAG_DAYS:
        return (
            f"on time, but {_dur(early, scale, unit_days)} early (ties a car up sooner than needed)"
        )
    return "on time"


def _why_late(reason: str) -> str:
    return "locked in (near delivery)" if reason == "frozen" else "no compatible car free"


def planner_report(snapshot: Snapshot, result: SolveResult, override: dict | None = None) -> str:
    """The finished, jargon-free reply for the planner. No λ, no solver internals —
    headline, what changed (with the actual supply swap), what's still late (split
    locked-in vs no-car), unchanged count, one caveat."""
    override = override or {}
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    incumbent = snapshot.incumbent
    disrupted = disrupted_order_keys(snapshot)
    cid_to_name = _cid_to_name(snapshot)
    scale, unit_days = time_scale_of(override)
    plan = result.plan

    # `disrupted` is a set — sort every derived list so the report is byte-stable.
    def late_by(oid: str, uid: str | None) -> int:
        """Days the order runs late on vehicle ``uid``; 0 if it has none."""
        return tardiness(orders[oid], units[uid]) if uid else 0

    broken = sorted(oid for oid in disrupted if late_by(oid, incumbent.get(oid)))
    n_fixed = sum(1 for oid in broken if plan.get(oid) and not late_by(oid, plan[oid]))
    changed = [
        oid
        for oid in sorted(orders)
        if oid not in result.unfilled and plan.get(oid) and plan[oid] != incumbent.get(oid)
    ]
    still_late = [oid for oid in sorted(orders) if late_by(oid, plan.get(oid))]
    # Why each still-late order is still late: the frozen fence, or no free car.
    reason_of = {
        oid: repairability(orders[oid], snapshot.now, units.get(incumbent.get(oid, "")))
        for oid in still_late
    }
    stuck = [oid for oid in still_late if reason_of[oid] == "frozen"]
    no_car = [oid for oid in still_late if reason_of[oid] != "frozen"]

    lines = [f"**Done — {_steering_summary(override, cid_to_name)}.**"]
    head = f"{n_fixed} of {len(broken)} delayed orders now on time"
    if still_late:
        head += f"; {len(still_late)} still late"
        if stuck:
            head += f" ({len(stuck)} locked in — can't be re-slotted)"
    lines.append(head + ".")

    # An order can be BOTH re-allocated and still late (a bump victim, or a move
    # that only narrowed the gap). Both facts are true and both tables need it:
    # the first says what we did, the second is the planner's call list. Mark the
    # overlap rather than dropping it from either -- dropping it from the call
    # list would hide exactly the order that moved and still failed.
    moved_and_late = set(changed) & set(still_late)

    if changed:
        lines.append("\n**What I moved**\n")
        lines.append(
            "| Order | Dealer (priority) | Was arriving | Now arrives | Promised "
            "| New allocation | Result |"
        )
        lines.append("|---|---|---|---|---|---|---|")

        for oid in changed:
            o, was_id, u = orders[oid], incumbent.get(oid), units[plan[oid]]
            res = _result_phrase(o, u, scale, unit_days)
            if was_id is not None and oid not in disrupted:
                res += " — **bumped**"
            kind = "future" if not u.is_hard else "car"
            alloc = f"`{plan[oid]}` [{kind}]" + (f" (was `{was_id}`)" if was_id else "")
            was = date_label(units[was_id].eta_dealer) if was_id else "—"
            lines.append(
                f"| {oid} | {o.customer} ({o.priority}) | {was} "
                f"| **{date_label(u.eta_dealer)}** | {date_label(o.delivery_date)} "
                f"| {alloc} | {res} |"
            )
    else:
        lines.append("\nNo allocation changes.")

    if still_late or result.unfilled:
        lines.append("\n**Still needs your call**\n")
        lines.append("| Order | Dealer (priority) | Arrives | Promised | Late | Why |")
        lines.append("|---|---|---|---|---|---|")

        for oid in stuck + no_car:
            o, u = orders[oid], units[plan[oid]]
            label = oid + (" ↑moved" if oid in moved_and_late else "")
            lines.append(
                f"| {label} | {o.customer} ({o.priority}) | {date_label(u.eta_dealer)} "
                f"| {date_label(o.delivery_date)} | {_dur(tardiness(o, u), scale, unit_days)} "
                f"| {_why_late(reason_of[oid])} |"
            )

        for oid in result.unfilled:
            o = orders[oid]
            lines.append(
                f"| {oid} | {o.customer} ({o.priority}) "
                f"| — | {date_label(o.delivery_date)} | — | no car at all (backordered) |"
            )

        if moved_and_late:
            lines.append(
                "\n↑moved = also in the list above: re-allocated and still late. This is a "
                "call list, not a second count."
            )

    n_unchanged = len(orders) - len(changed) - len(result.unfilled)
    lines.append(f"\nThe other {n_unchanged} orders are unchanged.")

    caveat = _caveat(orders, stuck, no_car)
    if caveat:
        lines.append(f"\n**Worth knowing:** {caveat}")
    return "\n".join(lines)


def _caveat(orders: dict[str, Order], stuck: list[str], no_car: list[str]) -> str | None:
    """One closing line about the worst thing re-allocation could not fix."""
    rank = {"A": 0, "B": 1, "C": 2}
    # Named by LINE plus the car, like every other planner-facing mention — never
    # the raw three-part key, which is an id the planner has no use for.
    if stuck:
        o = orders[min(stuck, key=lambda x: rank.get(orders[x].priority, 9))]
        return (
            f"{o.customer} ({o.priority}) order {o.key} is locked in and stays late — "
            "that's a delivery/expedite call, not something re-allocation can fix."
        )
    if no_car:
        o = orders[no_car[0]]
        return (
            f"{o.customer} order {o.key} stays late — no compatible car is free; "
            "allowing a bump on another priority tier might help."
        )
    return None


# Where the new allocations are written. The agent must answer follow-ups from
# THIS FILE, never from the report text in the conversation: a retyped table is a
# table that can lose a row or mistype a vehicle id, and the transcript is not a
# record the next turn can trust.
PLAN_FILENAME = "plan.json"


def plan_rows(snapshot: Snapshot, result: SolveResult, override: dict | None = None) -> list[dict]:
    """One row per order — the full allocation, as data rather than prose.

    Everything the report shows and a few things it does not, so a follow-up
    question ("show me the new allocations", "what did VSO-4007 get?") is answered
    by reading this back, not by re-reading the report.
    """
    override = override or {}
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    incumbent = snapshot.incumbent
    disrupted = disrupted_order_keys(snapshot)
    rows: list[dict] = []
    for oid in sorted(orders):
        o = orders[oid]
        was_id = incumbent.get(oid)
        now_id = result.plan.get(oid)
        was, now = units.get(was_id or ""), units.get(now_id or "")
        late = tardiness(o, now) if now else None
        if oid in result.unfilled:
            status = "no_car"
        elif now_id and now_id != was_id:
            status = "moved"
        else:
            status = "unchanged"
        rows.append(
            {
                "order": oid,
                "customer": o.customer,
                "customer_id": o.customer_id,
                "priority": o.priority,
                "model": o.sales_model,
                "promised": date_label(o.delivery_date),
                "was_car": was_id,
                "was_arriving": date_label(was.eta_dealer) if was else None,
                "now_car": now_id,
                "now_arriving": date_label(now.eta_dealer) if now else None,
                "on_the_lot": (now.is_hard if now else None),
                "days_late": late,
                "on_time": (late == 0) if late is not None else None,
                "status": status,
                # An order the disruption did not touch, moved anyway = displaced.
                "bumped": status == "moved" and oid not in disrupted and was_id is not None,
                "why_late": _why_late(repairability(o, snapshot.now, units.get(was_id or "")))
                if late
                else None,
            }
        )
    return rows


def save_plan(
    snapshot: Snapshot,
    result: SolveResult,
    override: dict | None = None,
    path: str | Path = PLAN_FILENAME,
) -> Path:
    """Write the plan to ``path`` and return it. The durable record of a turn."""
    out = Path(path)
    payload = {
        "now": date_label(snapshot.now),
        "override": override or {},
        "lambda": result.lam,
        "self_check": result.self_check,
        "counts": {
            "orders": len(snapshot.orders),
            "moved": sum(
                1 for r in plan_rows(snapshot, result, override) if r["status"] == "moved"
            ),
            "still_late": sum(1 for r in plan_rows(snapshot, result, override) if r["days_late"]),
            "no_car": len(result.unfilled),
        },
        "allocations": plan_rows(snapshot, result, override),
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out


def repair_and_report(
    snapshot: Snapshot,
    override: dict | None = None,
    plan_path: str | Path = PLAN_FILENAME,
) -> str:
    """Solve with the current combined override, WRITE the plan, return the reply.

    Two outputs on purpose. The string is what the planner reads; the file is the
    record — every allocation this turn produced, as data. Answer any follow-up
    from the file. Re-typing allocations out of the conversation is how a row goes
    missing or a vehicle id changes by one character.
    """
    cyc = run_cycle(snapshot, override)
    save_plan(snapshot, cyc.chosen, override, plan_path)
    return planner_report(snapshot, cyc.chosen, override)


# --- Demo (host-side) --------------------------------------------------------


def _banner(title: str) -> str:
    rule = "=" * 70
    return f"\n{rule}\n{title}\n{rule}"


def main() -> None:
    print(D.format_decisions())
    print(f"\nsolver version: {D.SOLVER_VERSION}")

    snap = flatten_default()
    d = snap.disruption
    print(
        f"\nsnapshot now={date_label(snap.now)}: {len(snap.orders)} orders, "
        f"{len(snap.units)} vehicles. Disruption: {len(d.get('delayed_vehicles', []))} vehicles "
        f"delayed {d.get('delay_label') or str(d.get('delay_days')) + ' days'}, "
        f"{len(d.get('disrupted_orders', []))} orders to repair."
    )
    print(_banner("DISCREPANCY MAP (what broke, before solving)"))
    print(discrepancy_report(snap))

    # Steer toward a dealer that actually has a disrupted row in play, so the demo
    # boost resolves to a real name instead of a placeholder id. Resolved to real
    # order keys — the manifest may name lines, not cars.
    disrupted = sorted(disrupted_order_keys(snap))
    fav = snap.order_by_key()[disrupted[0]] if disrupted else snap.orders[0]

    # ONE override, edited in place and carried across the turns — the point of the
    # demo is that nothing else is threaded between them.
    override: dict = {}
    turns = [
        ("default repair", {}),
        (
            f"steer: prefer {fav.customer} (same override, carried forward)",
            {"boosts": [{"customer": fav.customer_id, "weight_mult": 3.0}]},
        ),
        ("steer: also scope to that one dealer", {"scope": {"customers": [fav.customer_id]}}),
        ("steer: think in weeks (durations coarsen to the unit)", {"time_scale": "weeks"}),
    ]
    for n, (title, steer) in enumerate(turns, start=1):
        override.update(steer)
        print(_banner(f"TURN {n} — {title}"))
        print(repair_and_report(snap, override))

    print(
        "\n(steering is one combined override, carried forward and shown each turn — "
        "no ledger; re-solving the same override + a fresh pull reproduces the plan.)"
    )


if __name__ == "__main__":
    main()

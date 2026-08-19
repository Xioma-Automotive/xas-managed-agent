"""The per-turn session loop (§8), over the flattened date-based snapshot.

Each turn:
  1. (DECIDE-6) MCP liveness check — SKIPPED in the prototype (synthetic data).
  2. Pull the rich dataset and ``flatten`` it -> the orders/units/incumbent
     snapshot (pure code; see flatten.py).
  3. Map the discrepancies — which orders the disruption broke, and **which of
     them are even repairable** vs locked-in — BEFORE solving (`discrepancy_report`).
  4. Solve with the current combined **override** (weights / pins / scope / bump).
  5. Emit the finished, planner-facing report (`planner_report`) — a plain table,
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

from dataclasses import dataclass
from datetime import date

from . import decisions as D
from .flatten import flatten_default
from .snapshot import Order, Snapshot, date_label
from .solver import (
    SolveResult,
    SweepPoint,
    lambda_sweep,
    partition,
    repairability,
    scale_units,
    solve,
    tardiness,
)


@dataclass
class CycleResult:
    override: dict
    sweep: list[SweepPoint]
    results: dict[int, SolveResult]
    chosen_lambda: int
    chosen: SolveResult
    change_list: list[str]


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


def discrepancy_report(snapshot: Snapshot, override: dict | None = None) -> str:
    """Turn-1 planner-facing map: what broke, split into fixable vs locked-in.

    ``override`` is optional and used only to speak durations in the active
    time-scale (DECIDE-14); the discrepancy set itself is scale-independent."""
    discs = find_discrepancies(snapshot)
    if not discs:
        return "No orders are late — every allocated car still meets its promised date."
    scale, unit_days = _scale_of(override)
    stuck = [x for x in discs if not x.fixable]
    movable = [x for x in discs if x.fixable]

    lines = [f"**A supply delay pushed {len(discs)} order(s) past their promised date.**"]
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
                f"| {x.order_key} | {x.customer} ({x.priority}) | {date_label(x.promised)} | "
                f"{date_label(x.now_arriving)} | {_dur(x.days_late, scale, unit_days)} | {why} |"
            )
    if movable:
        lines.append(
            f"\n**{len(movable)} can be repaired** — a re-allocation may get these back on track:\n"
        )
        lines.append("| Order | Dealer (priority) | Promised | Now arriving | Late |")
        lines.append("|---|---|---|---|---|")
        for x in movable:
            lines.append(
                f"| {x.order_key} | {x.customer} ({x.priority}) | {date_label(x.promised)} | "
                f"{date_label(x.now_arriving)} | {_dur(x.days_late, scale, unit_days)} |"
            )
    return "\n".join(lines)


# --- Bump candidates (DECIDE-13: the agent ASKS before displacing anyone) -----


def bump_candidates(snapshot: Snapshot, result: SolveResult) -> list[dict]:
    """Untouched rows the planner *could* authorize bumping to rescue a still-late
    disrupted row — so the agent asks with a concrete list, lowest priority first."""
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    disrupted = set(snapshot.disruption.get("disrupted_orders", []))
    prio_rank = {"C": 0, "B": 1, "A": 2}

    still_late = [
        oid
        for oid in disrupted
        if result.plan.get(oid) and tardiness(orders[oid], units[result.plan[oid]]) > 0
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


def format_bump_candidates(cands: list[dict]) -> str:
    if not cands:
        return "No bump would help — no untouched, movable vehicle fits a still-late order."
    lines = ["Bumping one of these UNTOUCHED orders could rescue a late one — may I? (name who):"]
    for c in cands:
        lines.append(
            f"  {c['row']} ({c['customer']}, {c['priority']}) on {c['vehicle']} "
            f"arriving {c['arrives']} → could free it for {c['would_rescue']} ({c['rescue_customer']})"
        )
    return "\n".join(lines)


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
                f'  DIS["disruption<br/>{n_delayed} vehicles +{d.get("delay_days", 0)}d<br/>'
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


def _choose_lambda(override: dict, sweep: list[SweepPoint]) -> int:
    """Pick the λ to present as 'the' plan: override λ wins if set, else a
    mid-frontier default (neither max-churn nor max-late)."""
    if override.get("lambda") is not None:
        return int(override["lambda"])
    mid = D.LAMBDA_SWEEP[len(D.LAMBDA_SWEEP) // 2]
    return int(mid)


def run_cycle(
    snapshot: Snapshot,
    override: dict | None = None,
    current_date: date | None = None,
) -> CycleResult:
    """One deterministic turn: sweep λ over the combined override, choose, diff."""
    override = override or {}
    current_date = current_date or snapshot.now
    sweep, results = lambda_sweep(snapshot, override)
    chosen_lambda = _choose_lambda(override, sweep)
    if chosen_lambda not in results:
        results[chosen_lambda] = solve(snapshot, override, lam=chosen_lambda)
    chosen = results[chosen_lambda]
    changes = build_change_list(snapshot, chosen, override)
    return CycleResult(override, sweep, results, chosen_lambda, chosen, changes)


def build_change_list(snapshot: Snapshot, result: SolveResult, override: dict) -> list[str]:
    """Reason-coded diff of the chosen plan vs the incumbent (§8.6) — the internal
    'source' the planner report renders from."""
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    boosts = partition(snapshot, override).boosts
    disrupted = set(snapshot.disruption.get("disrupted_orders", []))
    lines: list[str] = []

    for oid in sorted(orders):
        o = orders[oid]
        old_uid = snapshot.incumbent.get(oid)
        new_uid = result.plan.get(oid)

        if oid in result.unfilled:
            lines.append(
                f"order {oid}: UNFILLED (backorder) — no compatible supply; "
                f"{_order_reasons(o, boosts)}"
            )
            continue
        if new_uid == old_uid:
            continue

        old_date = date_label(units[old_uid].eta_dealer) if old_uid else "unassigned"
        new_date = units[new_uid].eta_dealer
        late = tardiness(o, units[new_uid])
        timing = "on time" if late == 0 else f"{late}d late"
        new_kind = "future" if not units[new_uid].is_hard else "vehicle"
        allocation = (
            f"now {new_uid} [{new_kind}]"
            if old_uid is None
            else f"now {new_uid} [{new_kind}] (was {old_uid})"
        )
        bumped = old_uid is not None and oid not in disrupted
        tag = " — BUMPED (freed its vehicle for a disrupted order)" if bumped else ""
        lines.append(
            f"order {oid} ({o.customer}): {old_date} → {date_label(new_date)} "
            f"(promised {date_label(o.delivery_date)}, {timing}); {allocation}{tag}; "
            f"{_order_reasons(o, boosts)}"
        )
    return lines


def _order_reasons(o: Order, boosts: dict) -> str:
    """The 'why' half of a change line: data factors (no attribution trail — the
    ledger is gone; audit is deferred)."""
    bits = [f"priority {o.priority}"]
    if o.n_prior_delays:
        bits.append(f"delayed {o.n_prior_delays}× before")
    if o.times_rescheduled:
        bits.append(f"rescheduled {o.times_rescheduled}× by us — protected")
    if o.days_backordered:
        bits.append(f"back-ordered {o.days_backordered}d")
    if o.customer_id in boosts and boosts[o.customer_id] != 1.0:
        bits.append(f"boosted ×{boosts[o.customer_id]:g} ({o.customer})")
    return ", ".join(bits)


# --- Planner-facing report (the finished reply) ------------------------------

# The solver PRICES all earliness (DECIDE-15); the report only *mentions* it when
# it's notable, so a couple of days early isn't nagged about. Display threshold,
# not a solver knob.
EARLY_FLAG_DAYS = 14

_UNIT_LABEL = {"days": "day", "weeks": "week", "months": "month"}


def _scale_of(override: dict | None) -> tuple[str, int]:
    """Active time-scale (name, days-per-unit) from the override (DECIDE-14)."""
    scale = (override or {}).get("time_scale") or D.DEFAULT_TIME_SCALE
    return scale, D.SCALE_DAYS.get(scale, D.SCALE_DAYS[D.DEFAULT_TIME_SCALE])


def _dur(days: int, scale: str, unit_days: int) -> str:
    """A duration rendered in the active unit, rounding up (DECIDE-14)."""
    if unit_days <= 1:
        return f"{days} day" + ("" if days == 1 else "s")
    n = scale_units(days, unit_days)
    label = _UNIT_LABEL.get(scale, "unit")
    return f"{n} {label}" + ("" if n == 1 else "s")


def _cid_to_name(snapshot: Snapshot) -> dict[str, str]:
    m: dict[str, str] = {}
    for o in snapshot.orders:
        m.setdefault(o.customer_id, o.customer)
    return m


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
    if reason == "frozen":
        return "locked in (near delivery)"
    return "no compatible car free"


def planner_report(snapshot: Snapshot, result: SolveResult, override: dict | None = None) -> str:
    """The finished, jargon-free reply for the planner. No λ, no solver internals —
    headline, what changed (with the actual supply swap), what's still late (split
    locked-in vs no-car), unchanged count, one caveat."""
    override = override or {}
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    incumbent = snapshot.incumbent
    disrupted = set(snapshot.disruption.get("disrupted_orders", []))
    cid_to_name = _cid_to_name(snapshot)
    scale, unit_days = _scale_of(override)
    plan = result.plan

    broken = [
        oid
        for oid in disrupted
        if incumbent.get(oid) and tardiness(orders[oid], units[incumbent[oid]]) > 0
    ]
    n_fixed = sum(
        1 for oid in broken if plan.get(oid) and tardiness(orders[oid], units[plan[oid]]) == 0
    )
    changed = [
        oid
        for oid in sorted(orders)
        if oid not in result.unfilled and plan.get(oid) and plan[oid] != incumbent.get(oid)
    ]
    still_late = [
        oid
        for oid in sorted(orders)
        if plan.get(oid) and tardiness(orders[oid], units[plan[oid]]) > 0
    ]
    stuck, no_car = [], []
    for oid in still_late:
        inc = units.get(incumbent.get(oid)) if incumbent.get(oid) else None
        r = repairability(orders[oid], snapshot.now, inc)
        (stuck if r == "frozen" else no_car).append((oid, r))

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
            o, old, new = orders[oid], incumbent.get(oid), plan[oid]
            u = units[new]
            was = date_label(units[old].eta_dealer) if old else "—"
            kind = "future" if not u.is_hard else "car"
            alloc = f"`{new}` [{kind}]" + (f" (was `{old}`)" if old else "")
            res = _result_phrase(o, u, scale, unit_days)
            if old is not None and oid not in disrupted:
                res += " — **bumped**"
            lines.append(
                f"| {oid} | {o.customer} ({o.priority}) | {was} | "
                f"**{date_label(u.eta_dealer)}** | {date_label(o.delivery_date)} "
                f"| {alloc} | {res} |"
            )
    else:
        lines.append("\nNo allocation changes.")

    if still_late or result.unfilled:
        lines.append("\n**Still needs your call**\n")
        lines.append("| Order | Dealer (priority) | Arrives | Promised | Late | Why |")
        lines.append("|---|---|---|---|---|---|")
        for oid, r in stuck + no_car:
            o, u = orders[oid], units[plan[oid]]
            label = f"{oid} ↑moved" if oid in moved_and_late else oid
            lines.append(
                f"| {label} | {o.customer} ({o.priority}) | {date_label(u.eta_dealer)} "
                f"| {date_label(o.delivery_date)} | {_dur(tardiness(o, u), scale, unit_days)} "
                f"| {_why_late(r)} |"
            )
        for oid in result.unfilled:
            o = orders[oid]
            lines.append(
                f"| {oid} | {o.customer} ({o.priority}) | — | {date_label(o.delivery_date)} "
                f"| — | no car at all (backordered) |"
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


def _caveat(orders: dict[str, Order], stuck: list, no_car: list) -> str | None:
    rank = {"A": 0, "B": 1, "C": 2}
    worst_stuck = sorted((oid for oid, _ in stuck), key=lambda x: rank.get(orders[x].priority, 9))
    if worst_stuck:
        o = orders[worst_stuck[0]]
        return (
            f"{o.customer} ({o.priority}) order {worst_stuck[0]} is locked in and stays late — "
            "that's a delivery/expedite call, not something re-allocation can fix."
        )
    if no_car:
        o = orders[no_car[0][0]]
        return (
            f"{o.customer} order {no_car[0][0]} stays late — no compatible car is free; "
            "allowing a bump on another priority tier might help."
        )
    return None


def repair_and_report(snapshot: Snapshot, override: dict | None = None) -> str:
    """Solve with the current combined override and return the finished reply.
    This is the one call the agent makes per turn — print it verbatim."""
    cyc = run_cycle(snapshot, override)
    return planner_report(snapshot, cyc.chosen, override)


# --- Demo (host-side) --------------------------------------------------------


def main() -> None:
    print(D.format_decisions())
    print(f"\nsolver version: {D.SOLVER_VERSION}")

    snap = flatten_default()
    d = snap.disruption
    print(
        f"\nsnapshot now={date_label(snap.now)}: {len(snap.orders)} orders, "
        f"{len(snap.units)} vehicles. Disruption: {len(d.get('delayed_vehicles', []))} vehicles "
        f"delayed {d.get('delay_days')}d, {len(d.get('disrupted_orders', []))} orders to repair."
    )

    print("\n" + "=" * 70 + "\nDISCREPANCY MAP (what broke, before solving)\n" + "=" * 70)
    print(discrepancy_report(snap))

    print("\n" + "=" * 70 + "\nTURN 1 — default repair\n" + "=" * 70)
    override: dict = {}
    print(repair_and_report(snap, override))

    # Steer toward a dealer that actually has a disrupted row in play, so the
    # demo boost resolves to a real name instead of a placeholder id.
    orders_by_id = snap.order_by_key()
    disrupted = d.get("disrupted_orders", [])
    fav = orders_by_id[disrupted[0]] if disrupted else snap.orders[0]

    print(
        "\n"
        + "=" * 70
        + f"\nTURN 2 — steer: prefer {fav.customer} (same override, carried forward)\n"
        + "=" * 70
    )
    override["boosts"] = [{"customer": fav.customer_id, "weight_mult": 3.0}]
    print(repair_and_report(snap, override))

    print("\n" + "=" * 70 + "\nTURN 3 — steer: also scope to that one dealer\n" + "=" * 70)
    override["scope"] = {"customers": [fav.customer_id]}
    print(repair_and_report(snap, override))

    print(
        "\n"
        + "=" * 70
        + "\nTURN 4 — steer: think in weeks (durations coarsen to the unit)\n"
        + "=" * 70
    )
    override["time_scale"] = "weeks"
    print(repair_and_report(snap, override))

    print(
        "\n(steering is one combined override, carried forward and shown each turn — "
        "no ledger; re-solving the same override + a fresh pull reproduces the plan.)"
    )


if __name__ == "__main__":
    main()

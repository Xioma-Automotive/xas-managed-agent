"""The per-turn session loop (§8), over the flattened date-based snapshot.

Each turn (there is no liveness-check step — DECIDE-6 settled as NOT APPLICABLE,
since the pull happens host-side before the session exists):
  1. Flatten the two mounted files (``orders.json`` + ``vehicles.json``) -> the
     orders/vehicles/allocations snapshot (pure code; see flatten.py).
  2. Map the discrepancies — which orders the disruption broke — BEFORE solving
     (`discrepancy_report`). Every one of them is repairable now: the time fence
     that used to make some "locked in" was removed on 2026-08-26, and it was the
     only thing that ever made a broken order unfixable in principle.
  3. Solve with the current combined **override** (`priority` / `may_move` /
     `churn_price`).
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
from .flatten import flatten
from .snapshot import Order, Snapshot, date_label
from .solver import (
    CHURN_PRICE_SWEEP,
    DEFAULT_STEP,
    SOLVER_VERSION,
    SolveResult,
    SweepPoint,
    churn_sweep,
    disrupted_order_keys,
    effective_weight,
    partition,
    solve,
    tardiness,
)


@dataclass
class CycleResult:
    override: dict
    sweep: list[SweepPoint]
    results: dict[int, SolveResult]
    chosen_churn_price: int
    chosen: SolveResult


# --- Discrepancy map (step 2): what broke ------------------------------------


@dataclass
class Discrepancy:
    order_key: str
    sales_model: str
    vehicle_id: str
    promised: date
    now_arriving: date
    days_late: int
    # The client's name — display only. Defaulted so nothing that builds a
    # Discrepancy by hand has to know about it.
    customer: str = ""


def find_discrepancies(snapshot: Snapshot) -> list[Discrepancy]:
    """Orders whose currently-allocated supply now delivers past the promise.

    No fixable/stuck split any more: the time fence was the only thing that could
    call a broken order unrepairable, and it is gone. Every order here is worth
    trying — whether a re-allocation actually helps is the solver's answer, not
    a property of the order."""
    orders = snapshot.order_by_key()
    vehicles = snapshot.vehicle_by_id()
    out = [
        Discrepancy(
            order_key=oid,
            sales_model=orders[oid].sales_model,
            vehicle_id=vid,
            promised=orders[oid].delivery_date,
            now_arriving=vehicles[vid].eta_dealer,
            days_late=tardiness(orders[oid], vehicles[vid]),
            customer=orders[oid].customer,
        )
        for oid, vid in snapshot.allocations.items()
        if tardiness(orders[oid], vehicles[vid]) > 0
    ]
    out.sort(key=lambda d: (-d.days_late, d.order_key))
    return out


# What the pull filtered out, in the planner's words. The keys are the reasons
# `datasource.translate` (host-side) and `flatten` (here) count; each has one
# plain-English phrase, because the reply must never print a reason code.
DROP_PHRASES = {
    "no_model": "no model on the order",
    "no_promised_date": "no promised date",
    "no_order_id": "no order number",
    "order_without_an_id": "no order number",
    "order_without_a_model": "no model on the order",
    "order_without_a_promised_date": "no promised date",
    "vehicle_without_a_model": "no model on the car",
    "vehicle_without_an_arrival_date": "no arrival date on the car",
    "allocation_to_a_dropped_vehicle": "allocated to a car that is out of scope",
}


def _phrase(reason: str) -> str:
    return DROP_PHRASES.get(reason, reason.replace("_", " "))


def exclusion_note(snapshot: Snapshot) -> str:
    """What is NOT in this plan, and what is in it holding no car.

    Mandatory on turn 1, BEFORE the discrepancy map. Real data is patchy — an
    order with no model on it has nothing to match a car against, so it cannot be
    planned — and a plan covering three of twenty-five orders that does not say so
    reads as the whole book. Empty string when the pull filtered nothing and every
    order holds a car, so it costs nothing on a clean book.
    """
    excluded = dict((snapshot.meta or {}).get("excluded") or {})
    conflicts = (snapshot.meta or {}).get("conflicts") or []
    drops: Counter[str] = Counter()
    for bucket in ("order_drops", "flatten_skips"):
        for reason, n in (excluded.get(bucket) or {}).items():
            drops[_phrase(reason)] += n

    lines: list[str] = []
    seen, kept = excluded.get("orders_seen"), excluded.get("orders_kept")
    if drops and seen:
        why = ", ".join(f"{n} with {phrase}" for phrase, n in sorted(drops.items()))
        lines.append(
            f"**{seen - (kept or 0)} of {seen} orders are not in this plan** — {why}. "
            "They need completing in the system before they can be allocated."
        )
    # Orders that ARE in the plan but hold no car at all. Not a drop — this is
    # real unfilled demand, and it is the whole of a pure-unallocated book, where
    # "no orders are late" would otherwise read as "nothing to do".
    unallocated = sorted(o.key for o in snapshot.orders if o.key not in snapshot.allocations)
    if unallocated:
        shown = ", ".join(unallocated[:6]) + ("…" if len(unallocated) > 6 else "")
        lines.append(
            f"**{len(unallocated)} of {len(snapshot.orders)} orders hold no car yet** "
            f"({shown}) — they need allocating, not repairing."
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
    vehicles_seen, vehicles_kept = excluded.get("vehicles_seen"), excluded.get("vehicles_kept")
    if vehicles_seen and vehicles_kept is not None:
        lines.append(
            f"Car pool: {vehicles_kept} of {vehicles_seen} cars in stock or on order match "
            "something someone has ordered."
        )
    return "\n\n".join(lines)


def discrepancy_report(snapshot: Snapshot) -> str:
    """Turn-1 planner-facing map: what broke, before anything is solved."""
    discs = find_discrepancies(snapshot)
    note = exclusion_note(snapshot)
    if not discs:
        clear = "No orders are late — every allocated car still meets its promised date."
        return f"{note}\n\n{clear}" if note else clear

    lines = [note] if note else []
    lines.append(
        f"**A supply delay pushed {len(discs)} order(s) past their promised date.** "
        "A re-allocation may get these back on track:\n"
    )
    lines.append("| Order | Customer | Model | Promised | Now arriving | Late |")
    lines.append("|---|---|---|---|---|---|")
    for x in discs:
        lines.append(
            f"| {x.order_key} | {x.customer or '—'} | {x.sales_model} "
            f"| {date_label(x.promised)} | {date_label(x.now_arriving)} "
            f"| {_dur(x.days_late)} |"
        )
    return "\n".join(lines)


# --- Bump candidates (DECIDE-13: the agent ASKS before displacing anyone) -----


def bump_candidates(
    snapshot: Snapshot, result: SolveResult, override: dict | None = None
) -> list[dict]:
    """Untouched orders the planner *could* authorize displacing to rescue one that
    is still late — so the agent asks with a concrete list, lightest first.

    "Lightest" is the priority step the planner set this turn, not a letter on the
    record: with nothing steered every order weighs the same and the list is in key
    order. There is no longer a locked-in filter on the rescue targets — the fence
    that made a target unable to accept a different car is gone, which is what made
    three authorized bumps no-op on 2026-08-25."""
    orders = snapshot.order_by_key()
    vehicles = snapshot.vehicle_by_id()
    disrupted = disrupted_order_keys(snapshot)
    priority = partition(snapshot, override or {}).priority

    still_late = [
        oid
        for oid in disrupted
        if result.plan.get(oid) and tardiness(orders[oid], vehicles[result.plan[oid]]) > 0
    ]
    cands: dict[str, dict] = {}
    for lid in sorted(still_late):
        lo = orders[lid]
        for oid, o in orders.items():
            if oid in disrupted or oid in cands:
                continue
            vid = snapshot.allocations.get(oid)
            if not vid:
                continue
            u = vehicles[vid]
            if u.sales_model != lo.sales_model:
                continue
            if u.eta_dealer <= lo.delivery_date:
                cands[oid] = {
                    "row": oid,
                    "customer": o.customer,
                    "model": o.sales_model,
                    "priority": priority.get(oid, DEFAULT_STEP),
                    "vehicle": vid,
                    "arrives": date_label(u.eta_dealer),
                    "would_rescue": lid,
                }
    return sorted(
        cands.values(), key=lambda c: (effective_weight(orders[c["row"]], priority), c["row"])
    )


# --- Data-prep flow chart ----------------------------------------------------


def data_prep_flowchart(snapshot: Snapshot) -> str:
    """Mermaid of how the mounted pull became solver inputs — the data-prep hop."""
    n_orders = len(snapshot.orders)
    n_vehicles = len(snapshot.vehicles)
    n_allocations = len(snapshot.allocations)
    n_free = n_vehicles - n_allocations
    n_models = len({o.sales_model for o in snapshot.orders})
    d = snapshot.disruption
    n_disrupted = len(d.get("disrupted_orders", []))
    return "\n".join(
        [
            "```mermaid",
            "flowchart LR",
            (
                f'  SRC["mounted pull<br/>orders.json · vehicles.json<br/>'
                f'{n_orders} orders · {n_vehicles} cars ({n_free} free)"]'
            ),
            (f'  DIS["late arrivals<br/>derived, not declared<br/>{n_disrupted} orders freed"]'),
            '  FL["flatten + freeze<br/>(pure code, no model judgment)"]',
            f'  ORD["orders[]<br/>{n_orders} rows"]',
            f'  VEH["vehicles[]<br/>{n_vehicles} rows"]',
            f'  ALC["allocations[]<br/>{n_allocations} pairs"]',
            (
                f'  ARC["eligibility arcs<br/>sales_model equality · {n_models} models<br/>'
                '(computed, never stored)"]'
            ),
            '  SOLVE["min-cost-flow<br/>churn-price sweep"]',
            "  SRC --> FL",
            "  DIS --> FL",
            "  FL --> ORD",
            "  FL --> VEH",
            "  FL --> ALC",
            "  ORD --> ARC",
            "  VEH --> ARC",
            "  ALC --> SOLVE",
            "  ARC --> SOLVE",
            "```",
        ]
    )


# --- Solve ------------------------------------------------------------------


def run_cycle(snapshot: Snapshot, override: dict | None = None) -> CycleResult:
    """One deterministic turn: sweep the churn price over the combined override,
    then choose the one to present as 'the' plan — the override's if it set one,
    else the middle of the sweep (neither max-churn nor max-late). Only an
    off-sweep price needs a re-solve."""
    override = override or {}
    sweep, results = churn_sweep(snapshot, override)
    # `or` would swallow a deliberate churn_price=0 (the first sweep value), so
    # this tests for absence, not falsiness.
    steered = override.get("churn_price")
    price = int(steered) if steered is not None else CHURN_PRICE_SWEEP[len(CHURN_PRICE_SWEEP) // 2]
    if price not in results:
        results[price] = solve(snapshot, override, churn_price=price)
    return CycleResult(override, sweep, results, price, results[price])


def carry_forward(override: dict | None) -> dict:
    """The override for the NEXT turn: this turn's bump authorisation is spent.

    ``may_move.also`` is permission for ONE solve (decided 2026-08-27). Every
    other key is a standing instruction that holds until the planner changes it,
    which is why this drops exactly one thing. A permission that quietly persisted
    would mean a later turn displacing a settled order on the strength of a
    sentence said three turns ago.

    Returns a NEW object: the override that was solved is stamped into
    ``plan.json`` by ``save_plan``, and that record is what makes the turn
    reproducible, so it must not be edited in place.
    """
    out = {key: value for key, value in (override or {}).items() if key != "may_move"}
    may_move = override.get("may_move") if override else None
    kept = {key: value for key, value in (may_move or {}).items() if key != "also" and value}
    if kept:
        out["may_move"] = kept
    return out


# --- Planner-facing report (the finished reply) ------------------------------

# The solver PRICES all earliness (DECIDE-15); the report only *mentions* it when
# it's notable, so a couple of days early isn't nagged about. Display threshold,
# not a solver knob.
EARLY_FLAG_DAYS = 14


def _dur(days: int) -> str:
    """A duration in days — the only unit the solver measures and the report
    speaks, since the days/weeks/months knob was cut on 2026-08-26."""
    return f"{days} day" + ("" if days == 1 else "s")


def _who(filt: dict) -> str:
    """Whatever a may_move filter names, in the planner's own words. Order ids
    first, then models — there is no customer FILTER since 2026-08-27, only the label."""
    return (
        ", ".join(filt.get("orders") or [])
        or ", ".join(filt.get("models") or [])
        or "selected orders"
    )


def _steering_summary(override: dict) -> str:
    """The three steering keys, said back in plain words. Every turn shows this:
    the override is the only state a turn carries, so a planner who cannot see
    what is still in force cannot tell why a plan changed."""
    parts: list[str] = []
    for entry in override.get("priority") or []:
        parts.append(f"{entry['order']} set to {entry['step']}")
    may_move = override.get("may_move") or {}
    if may_move.get("only"):
        parts.append(f"working only {_who(may_move['only'])}")
    # `is True` FIRST: `_who` reads filter keys off a dict, so handed the
    # fleet-wide sentinel it raises and takes the whole report with it.
    if may_move.get("also") is True:
        parts.append("allowed bumping anyone still settled, this turn")
    elif may_move.get("also"):
        parts.append(f"allowed bumping {_who(may_move['also'])}")
    if may_move.get("never"):
        parts.append("leaving " + ", ".join(may_move["never"]) + " alone")
    if override.get("churn_price") is not None:
        parts.append(f"churn priced at {override['churn_price']}")
    return "; ".join(parts) if parts else "default repair"


def _result_phrase(order: Order, vehicle) -> str:
    late = tardiness(order, vehicle)
    if late > 0:
        return f"{_dur(late)} late"
    early = (order.delivery_date - vehicle.eta_dealer).days
    if early > EARLY_FLAG_DAYS:
        return f"on time, but {_dur(early)} early (ties a car up sooner than needed)"
    return "on time"


# The one reason an order can still be late after a repair. There used to be two:
# "locked in (near delivery)" went with the time fence on 2026-08-26.
WHY_LATE = "no compatible car free"


def _client(order: Order) -> str:
    """The client's name for a table cell. A dash rather than an empty cell when
    the order carries no name — a blank reads as a rendering fault."""
    return order.customer or "—"


def planner_report(snapshot: Snapshot, result: SolveResult, override: dict | None = None) -> str:
    """The finished, jargon-free reply for the planner. No churn price, no solver
    internals — headline, what changed (with the actual supply swap), what is
    still late, unchanged count, one caveat."""
    override = override or {}
    orders = snapshot.order_by_key()
    vehicles = snapshot.vehicle_by_id()
    allocations = snapshot.allocations
    disrupted = disrupted_order_keys(snapshot)
    priority = partition(snapshot, override).priority
    plan = result.plan

    # `disrupted` is a set — sort every derived list so the report is byte-stable.
    def late_by(oid: str, vid: str | None) -> int:
        """Days the order runs late on vehicle ``uid``; 0 if it has none."""
        return tardiness(orders[oid], vehicles[vid]) if vid else 0

    broken = sorted(oid for oid in disrupted if late_by(oid, allocations.get(oid)))
    n_fixed = sum(1 for oid in broken if plan.get(oid) and not late_by(oid, plan[oid]))
    changed = [
        oid
        for oid in sorted(orders)
        if oid not in result.unfilled and plan.get(oid) and plan[oid] != allocations.get(oid)
    ]
    still_late = [oid for oid in sorted(orders) if late_by(oid, plan.get(oid))]

    lines = [f"**Done — {_steering_summary(override)}.**"]
    head = f"{n_fixed} of {len(broken)} delayed orders now on time"
    if still_late:
        head += f"; {len(still_late)} still late"
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
            "| Order | Customer | Model | Was arriving | Now arrives | Promised "
            "| New allocation | Result |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")

        for oid in changed:
            o, was_id, u = orders[oid], allocations.get(oid), vehicles[plan[oid]]
            res = _result_phrase(o, u)
            if was_id is not None and oid not in disrupted:
                res += " — **bumped**"
            alloc = f"`{plan[oid]}`" + (f" (was `{was_id}`)" if was_id else "")
            was = date_label(vehicles[was_id].eta_dealer) if was_id else "—"
            lines.append(
                f"| {oid} | {_client(o)} | {o.sales_model} | {was} "
                f"| **{date_label(u.eta_dealer)}** | {date_label(o.delivery_date)} "
                f"| {alloc} | {res} |"
            )
    else:
        lines.append("\nNo allocation changes.")

    if still_late or result.unfilled:
        lines.append("\n**Still needs your call**\n")
        lines.append("| Order | Customer | Model | Arrives | Promised | Late | Why |")
        lines.append("|---|---|---|---|---|---|---|")

        for oid in still_late:
            o, u = orders[oid], vehicles[plan[oid]]
            label = oid + (" ↑moved" if oid in moved_and_late else "")
            lines.append(
                f"| {label} | {_client(o)} | {o.sales_model} | {date_label(u.eta_dealer)} "
                f"| {date_label(o.delivery_date)} | {_dur(tardiness(o, u))} "
                f"| {WHY_LATE} |"
            )

        for oid in result.unfilled:
            o = orders[oid]
            lines.append(
                f"| {oid} | {_client(o)} | {o.sales_model} | — | {date_label(o.delivery_date)} "
                "| — | no car at all |"
            )

        if moved_and_late:
            lines.append(
                "\n↑moved = also in the list above: re-allocated and still late. This is a "
                "call list, not a second count."
            )

    n_unchanged = len(orders) - len(changed) - len(result.unfilled)
    lines.append(f"\nThe other {n_unchanged} orders are unchanged.")

    caveat = _caveat(orders, still_late, priority)
    if caveat:
        lines.append(f"\n**Worth knowing:** {caveat}")
    return "\n".join(lines)


def _caveat(
    orders: dict[str, Order], still_late: list[str], priority: dict[str, str]
) -> str | None:
    """One closing line about the worst thing re-allocation could not fix — the
    heaviest order still late, which is the one the planner set highest if they
    set anything."""
    if not still_late:
        return None
    o = orders[max(still_late, key=lambda x: (effective_weight(orders[x], priority), x))]
    return (
        f"order {o.key} ({o.sales_model}) stays late — no compatible car is free; "
        "authorising a bump on another order might help."
    )


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
    vehicles = snapshot.vehicle_by_id()
    allocations = snapshot.allocations
    disrupted = disrupted_order_keys(snapshot)
    priority = partition(snapshot, override).priority
    rows: list[dict] = []
    for oid in sorted(orders):
        o = orders[oid]
        was_id = allocations.get(oid)
        now_id = result.plan.get(oid)
        was, now = vehicles.get(was_id or ""), vehicles.get(now_id or "")
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
                "priority": priority.get(oid, DEFAULT_STEP),
                "model": o.sales_model,
                "promised": date_label(o.delivery_date),
                "was_car": was_id,
                "was_arriving": date_label(was.eta_dealer) if was else None,
                "now_car": now_id,
                "now_arriving": date_label(now.eta_dealer) if now else None,
                "days_late": late,
                "on_time": (late == 0) if late is not None else None,
                "status": status,
                # An order the disruption did not touch, moved anyway = displaced.
                "bumped": status == "moved" and oid not in disrupted and was_id is not None,
                "why_late": WHY_LATE if late else None,
            }
        )
    return rows


def save_plan(
    snapshot: Snapshot,
    result: SolveResult,
    override: dict | None = None,
    path: str | Path = PLAN_FILENAME,
) -> Path:
    """Write the plan to ``path`` and return it. The durable record of a turn.

    ``solver_version`` is stamped because a plan is only reproducible against the
    config it was priced with — the numbers live in ``solver_config.yaml`` now,
    so a saved plan that did not name a version could not be traced to them."""
    out = Path(path)
    payload = {
        "now": date_label(snapshot.now),
        "solver_version": SOLVER_VERSION,
        "override": override or {},
        "churn_price": result.churn_price,
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


def _demo_snapshot() -> Snapshot:
    """The demo's snapshot, built the way the host does it: translate a scenario
    directory, then flatten the two payloads. Host-side only — `datasource` never
    ships to the sandbox, which is why this import is inside the function."""
    import datasource

    pull = datasource.get_source().pull()
    return flatten(datasource.orders_payload(pull), datasource.vehicles_payload(pull))


def _banner(title: str) -> str:
    rule = "=" * 70
    return f"\n{rule}\n{title}\n{rule}"


def main() -> None:
    print(D.format_decisions())
    print(f"\nsolver version: {SOLVER_VERSION} (from solver_config.yaml)")

    snap = _demo_snapshot()
    d = snap.disruption
    print(
        f"\nsnapshot now={date_label(snap.now)}: {len(snap.orders)} orders, "
        f"{len(snap.vehicles)} vehicles, {len(snap.allocations)} allocated. "
        f"{len(d.get('disrupted_orders', []))} orders arrive late and are free to repair."
    )
    print(_banner("DISCREPANCY MAP (what broke, before solving)"))
    print(discrepancy_report(snap))

    # Steer an order that is actually in play, so the demo resolves to a real
    # dealer instead of a placeholder id.
    disrupted = sorted(disrupted_order_keys(snap))
    fav = snap.order_by_key()[disrupted[0]] if disrupted else snap.orders[0]

    # ONE override, edited in place and carried across the turns — the point of the
    # demo is that nothing else is threaded between them.
    override: dict = {}
    turns = [
        ("default repair", {}),
        (
            f"steer: {fav.key} is urgent (same override, carried forward)",
            {"priority": [{"order": fav.key, "step": "urgent"}]},
        ),
        (
            f"steer: and work only the {fav.sales_model} orders",
            {"may_move": {"only": {"models": [fav.sales_model]}}},
        ),
        ("steer: change as little as possible", {"churn_price": 100}),
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

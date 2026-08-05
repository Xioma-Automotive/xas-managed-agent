"""Deterministic min-cost-flow repair solver (OR-Tools).

The agent NEVER implements the algorithm — it builds the network and calls
``ortools.graph.SimpleMinCostFlow`` (§3). Everything here is a pure function of
(snapshot, combined_override, lambda): same inputs -> byte-identical plan (the
core invariant).

Design highlights, mapped to the spec:
- §4  Integer node positions from a FIXED sort (order_id / vehicle_id asc); real
      keys recovered through explicit lookup tables before anything leaves here.
- §1  Repair, don't re-solve: pin the whole incumbent, free ONLY the disrupted
      orders, re-match just those. Change count is structural.
- §2  cost(o->u) = W(o)·tardiness_days^1.5 + λ(fence)·[date(u) ≠ promised_date(o)].
- §5  Data pins (frozen orders, committed vehicles) are pre-committed OUT of the
      graph; instruction pins/defers/forbids are large finite soft penalties
      (DECIDE-4 / DECIDE-8) so a conflict surfaces as a cost line, never a crash.
- A per-order dummy "backorder" arc keeps the flow always feasible; an order
  routed to it is an explicitly surfaced unfilled order, not a silent drop.

Eligibility (the sparse arc rule) is a HARD ``sales_model`` equality, computed
here and never stored — the old fuzzy spec-match + LLM residual is gone. There
is no model judgment left in the data path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ortools.graph.python import min_cost_flow

from . import decisions as D
from .snapshot import Order, Snapshot, Unit, days_late, parse_date

# Float costs are scaled to ints for the integer-only min-cost-flow.
COST_SCALE = 1000
# Leaving an order unfilled must cost more than violating any soft instruction
# pin, so the solver fills where it can and only backorders as a last resort.
BACKORDER_COST = D.SOFT_PIN_COST * 10.0


# --- Fence + cost model -------------------------------------------------------


def fence_of(order: Order, now: date) -> str:
    """DECIDE-2 time fence by days-until-promised: frozen / slushy / liquid."""
    days_out = (order.promised_date - now).days
    if days_out <= D.FROZEN_MAX_DAYS:
        return "frozen"
    if days_out <= D.SLUSHY_MAX_DAYS:
        return "slushy"
    return "liquid"


def effective_weight(order: Order, boosts: dict[str, float]) -> float:
    """W(o) per §2, with DECIDE-1 controlling how back-order aging enters.

    Two escalation terms ride the priority weight: ``n_prior_delays`` (supply-side
    history, α) and ``times_rescheduled`` (reschedules OUR repair loop caused, γ —
    DECIDE-11). The latter makes an already-bumped order heavier, so the solver
    protects it from being delayed *again* and picks someone else to absorb the
    next slip — the fairness lever, not a new hard constraint.

    boosts maps customer_id -> multiplier (from ledger override 'boosts')."""
    escalation = 1 + D.ALPHA * order.n_prior_delays + D.GAMMA * order.times_rescheduled
    w = D.PRIORITY_WEIGHT[order.priority] * escalation
    if D.AGING_MODE == "multiplicative":
        w *= 1 + D.BETA * order.days_backordered
    else:  # additive (default)
        w += D.BETA * order.days_backordered
    w *= boosts.get(order.customer_id, 1.0)
    return w


def tardiness(order: Order, unit: Unit) -> int:
    """Days late = how far the vehicle's planned delivery runs past the promise."""
    return days_late(unit.planned_delivery_date, order.promised_date)


def arc_cost_float(
    order: Order,
    unit: Unit,
    lam: int,
    now: date,
    boosts: dict[str, float],
    not_before: date | None,
) -> float:
    """§2 cost for one order->unit arc, in float space (scaled to int later)."""
    late = tardiness(order, unit)
    cost = effective_weight(order, boosts) * (late**D.CONVEX_EXPONENT)

    # λ additive term, gated by the fence (liquid => free to change the date).
    if unit.planned_delivery_date != order.promised_date and fence_of(order, now) == "slushy":
        cost += lam

    # Soft instruction pin: 'defer'/not_before violated by an early arrival.
    if not_before is not None and unit.planned_delivery_date < not_before:
        cost += D.SOFT_PIN_COST
    return cost


def eligible(order: Order, unit: Unit) -> bool:
    """The sparse arc rule (computed, never stored): hard sales_model equality.

    DECIDE-10: a reserved_for_customer term would AND in here once modelled.
    Lateness is NOT a feasibility gate — it is priced in ``arc_cost_float`` so a
    slightly-late vehicle can still be placed instead of forcing a backorder."""
    return order.sales_model == unit.sales_model


# --- Repair problem partition (§1, §5) ---------------------------------------


@dataclass
class RepairPlan:
    """The pinned/free partition + the combined override, resolved once."""

    pinned: dict[str, str]  # order_id -> vehicle_id (hard, kept as-is)
    free_orders: list[str]  # order_ids to (re)match
    free_units: list[str]  # vehicle_ids available to the free orders
    boosts: dict[str, float]  # customer_id -> weight multiplier
    lam_default: int | None  # ledger-supplied λ (sweep still explores all)
    not_before: dict[str, date]  # order_id -> earliest allowed delivery date
    forbid_no_move: set[str]  # orders explicitly pinned by instruction


def _combined_boosts(override: dict) -> dict[str, float]:
    boosts: dict[str, float] = {}
    for b in override.get("boosts", []):
        cid = b.get("customer") or b.get("customer_id")
        if cid is None:
            continue
        boosts[cid] = boosts.get(cid, 1.0) * float(b.get("weight_mult", 1.0))
    return boosts


def partition(snapshot: Snapshot, override: dict) -> RepairPlan:
    """Decide what is pinned vs free (§1, §5) from data rules + the override."""
    orders = snapshot.order_by_id()
    units = snapshot.unit_by_id()
    incumbent = dict(snapshot.incumbent)
    disrupted = set(snapshot.disruption.get("disrupted_orders", []))

    # Instruction-driven sets from the combined override.
    boosts = _combined_boosts(override)
    lam_default = override.get("lambda")
    not_before: dict[str, date] = {}
    deferred: set[str] = set()
    for p in override.get("pins", []):
        if p.get("action") == "defer":
            oid = str(p["order"])
            deferred.add(oid)
            if p.get("not_before"):
                not_before[oid] = parse_date(p["not_before"])
    forbid_no_move = {
        str(f["order"]) for f in override.get("forbid", []) if f.get("action") == "no_move"
    }

    # Free orders: disrupted or actively deferred, EXCEPT
    #   - frozen orders (can't move, §2 time fence),
    #   - orders explicitly pinned no_move,
    #   - orders riding a committed vehicle (can't recall a bonded/pdi unit).
    # Plus any order the incumbent left unassigned (must get a vehicle).
    free_orders: list[str] = []
    for oid, o in orders.items():
        assigned = incumbent.get(oid)
        wants_move = (oid in disrupted) or (oid in deferred)
        if oid in forbid_no_move:
            continue
        if fence_of(o, snapshot.now) == "frozen":
            continue
        if assigned is not None and units[assigned].committed:
            continue
        if wants_move or assigned is None:
            free_orders.append(oid)
    free_orders.sort()  # §4 fixed key

    free_set = set(free_orders)
    # Pinned = everyone else keeps their incumbent vehicle (if any).
    pinned = {oid: uid for oid, uid in incumbent.items() if oid not in free_set}

    # Free vehicles: not consumed by a pinned assignment, and not committed.
    consumed = set(pinned.values())
    free_units = [uid for uid, u in units.items() if uid not in consumed and not u.committed]
    free_units.sort()  # §4 fixed key

    return RepairPlan(
        pinned=pinned,
        free_orders=free_orders,
        free_units=free_units,
        boosts=boosts,
        lam_default=lam_default,
        not_before=not_before,
        forbid_no_move=forbid_no_move,
    )


# --- Solve one λ --------------------------------------------------------------


@dataclass
class SolveResult:
    lam: int
    plan: dict[str, str]  # order_id -> vehicle_id (full: pinned+free)
    unfilled: list[str]  # free orders routed to backorder
    node_index: dict[int, object]  # position -> ('order'|'unit', real_id)
    n_changes: int  # free orders whose vehicle differs from incumbent
    weighted_late_days: float  # Σ W(o)·tardiness over ALL orders
    objective_micro: int  # solver objective in scaled int space
    self_check: dict


def _solve_one(snapshot: Snapshot, rp: RepairPlan, lam: int) -> SolveResult:
    orders = snapshot.order_by_id()
    units = snapshot.unit_by_id()

    # §4 integer node positions from the fixed sort already applied to rp.
    # 0=S, 1=T, 2=D(backorder); orders then units follow.
    S, T, DUMMY = 0, 1, 2
    node_index: dict[int, object] = {}
    order_pos: dict[str, int] = {}
    unit_pos: dict[str, int] = {}
    pos = 3
    for oid in rp.free_orders:
        order_pos[oid] = pos
        node_index[pos] = ("order", oid)
        pos += 1
    for uid in rp.free_units:
        unit_pos[uid] = pos
        node_index[pos] = ("unit", uid)
        pos += 1

    smcf = min_cost_flow.SimpleMinCostFlow()
    N = len(rp.free_orders)

    # S -> each free order (cap 1, cost 0)
    for oid in rp.free_orders:
        smcf.add_arc_with_capacity_and_unit_cost(S, order_pos[oid], 1, 0)

    # order -> compatible free vehicle (cap 1, §2 cost) ; order -> backorder dummy
    for oid in rp.free_orders:
        o = orders[oid]
        nb = rp.not_before.get(oid)
        for uid in rp.free_units:
            u = units[uid]
            if not eligible(o, u):
                continue  # incompatible -> no arc (keeps graph sparse, §4)
            c = arc_cost_float(o, u, lam, snapshot.now, rp.boosts, nb)
            smcf.add_arc_with_capacity_and_unit_cost(
                order_pos[oid], unit_pos[uid], 1, round(c * COST_SCALE)
            )
        smcf.add_arc_with_capacity_and_unit_cost(
            order_pos[oid], DUMMY, 1, round(BACKORDER_COST * COST_SCALE)
        )

    # free vehicle -> T (cap 1) ; backorder dummy -> T (cap N)
    for uid in rp.free_units:
        smcf.add_arc_with_capacity_and_unit_cost(unit_pos[uid], T, 1, 0)
    smcf.add_arc_with_capacity_and_unit_cost(DUMMY, T, N, 0)

    smcf.set_node_supply(S, N)
    smcf.set_node_supply(T, -N)

    status = smcf.solve()
    if status != smcf.OPTIMAL:
        raise RuntimeError(f"min-cost-flow did not solve to optimality: status={status}")

    # Read back flow -> real keys (§4).
    plan: dict[str, str] = dict(rp.pinned)
    unfilled: list[str] = []
    assigned_free: dict[str, str] = {}
    for arc in range(smcf.num_arcs()):
        if smcf.flow(arc) <= 0:
            continue
        tail, head = smcf.tail(arc), smcf.head(arc)
        if tail in node_index and node_index[tail][0] == "order":
            oid = node_index[tail][1]
            if head == DUMMY:
                unfilled.append(oid)
            elif head in node_index and node_index[head][0] == "unit":
                assigned_free[oid] = node_index[head][1]
    plan.update(assigned_free)
    unfilled.sort()

    n_changes = sum(1 for oid in rp.free_orders if snapshot.incumbent.get(oid) != plan.get(oid))

    weighted_late = 0.0
    for oid, o in orders.items():
        uid = plan.get(oid)
        if uid is not None:
            weighted_late += effective_weight(o, rp.boosts) * tardiness(o, units[uid])

    check = _self_check(snapshot, plan, unfilled)
    return SolveResult(
        lam=lam,
        plan=plan,
        unfilled=unfilled,
        node_index=node_index,
        n_changes=n_changes,
        weighted_late_days=round(weighted_late, 4),
        objective_micro=smcf.optimal_cost(),
        self_check=check,
    )


def _self_check(snapshot: Snapshot, plan: dict, unfilled: list) -> dict:
    """§8.5 hard-constraint self-check. Returns findings; never silently relaxes."""
    orders = snapshot.order_by_id()
    units = snapshot.unit_by_id()
    violations: list[str] = []

    # No committed vehicle reassigned away from its incumbent order.
    for oid, uid in snapshot.incumbent.items():
        if units[uid].committed and plan.get(oid) != uid:
            violations.append(f"committed vehicle {uid} moved off order {oid}")
    # No sales_model violation on any assignment.
    for oid, uid in plan.items():
        if not eligible(orders[oid], units[uid]):
            violations.append(f"order {oid} assigned incompatible vehicle {uid}")
    # Every order has exactly one vehicle (or is a surfaced backorder).
    every_order_placed = all((oid in plan) or (oid in unfilled) for oid in orders)
    # No vehicle double-booked.
    used = list(plan.values())
    if len(used) != len(set(used)):
        violations.append("a vehicle is assigned to more than one order")

    return {
        "ok": not violations and every_order_placed,
        "violations": violations,
        "unfilled_count": len(unfilled),
        "every_order_placed": every_order_placed,
    }


# --- λ sweep (§2, highest-value output) --------------------------------------


@dataclass
class SweepPoint:
    lam: int
    n_changes: int
    weighted_late_days: float
    unfilled: int


def solve(
    snapshot: Snapshot,
    override: dict | None = None,
    lam: int | None = None,
) -> SolveResult:
    """Single deterministic solve at one λ (default: ledger λ, else first sweep value)."""
    override = override or {}
    rp = partition(snapshot, override)
    if lam is None:
        lam = rp.lam_default if rp.lam_default is not None else D.LAMBDA_SWEEP[0]
    return _solve_one(snapshot, rp, int(lam))


def lambda_sweep(
    snapshot: Snapshot,
    override: dict | None = None,
    lambdas=D.LAMBDA_SWEEP,
) -> tuple[list[SweepPoint], dict[int, SolveResult]]:
    """Re-solve across λ (same network, only some arc costs change) -> Pareto
    frontier of (changes vs weighted late-days). Returns the frontier points and
    the full per-λ results keyed by λ."""
    override = override or {}
    rp = partition(snapshot, override)
    points: list[SweepPoint] = []
    results: dict[int, SolveResult] = {}
    for lam in lambdas:
        res = _solve_one(snapshot, rp, int(lam))
        results[int(lam)] = res
        points.append(
            SweepPoint(int(lam), res.n_changes, res.weighted_late_days, len(res.unfilled))
        )
    return points, results

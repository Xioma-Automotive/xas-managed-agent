"""Deterministic min-cost-flow repair solver (OR-Tools).

The agent NEVER implements the algorithm — it builds the network and calls
``ortools.graph.SimpleMinCostFlow`` (§3). Everything here is a pure function of
(snapshot, combined_override, residual_cache, lambda): same inputs -> byte-
identical plan (the core invariant).

Design highlights, mapped to the spec:
- §4  Integer node positions from a FIXED sort (order_id / unit_id asc); real
      keys recovered through explicit lookup tables before anything leaves here.
- §1  Repair, don't re-solve: pin the whole incumbent, free ONLY the disrupted
      (planned) orders, re-match just those. Change count is structural.
- §2  cost(o->u) = W(o)·tardiness^1.5 + λ(fence)·[week(u)≠promised_week(o)].
- §5  Data pins (frozen orders, committed units) are pre-committed OUT of the
      graph; instruction pins/defers/forbids are large finite soft penalties
      (DECIDE-4 / DECIDE-8) so a conflict surfaces as a cost line, never a crash.
- A per-order dummy "backorder" arc keeps the flow always feasible; an order
  routed to it is an explicitly surfaced unfilled order, not a silent drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ortools.graph.python import min_cost_flow

from . import decisions as D
from .spec_match import PRIORITY_WEIGHT, ResidualCache, resolve_compatibility
from .synth_data import Order, Snapshot, Unit, parse_week_label

NOW_WEEK = 32  # "this cycle" — the front of the horizon (see synth_data.HORIZON_START_WEEK)

# Float costs are scaled to ints for the integer-only min-cost-flow.
COST_SCALE = 1000
# Leaving an order unfilled must cost more than violating any soft instruction
# pin, so the solver fills where it can and only backorders as a last resort.
BACKORDER_COST = D.SOFT_PIN_COST * 10.0


# --- Fence + cost model -------------------------------------------------------

def fence_of(order: Order, now_week: int = NOW_WEEK) -> str:
    """DECIDE-2 time fence by weeks-until-promised: frozen / slushy / liquid."""
    weeks_out = order.promised_week - now_week
    if weeks_out <= D.FROZEN_MAX_WEEKS:
        return "frozen"
    if weeks_out <= D.SLUSHY_MAX_WEEKS:
        return "slushy"
    return "liquid"


def effective_weight(order: Order, boosts: dict[str, float]) -> float:
    """W(o) per §2, with DECIDE-1 controlling how back-order aging enters.

    boosts maps customer_id -> multiplier (from ledger override 'boosts')."""
    w = 1.0 * PRIORITY_WEIGHT[order.priority] * (1 + D.ALPHA * order.n_prior_delays)
    if D.AGING_MODE == "multiplicative":
        w *= (1 + D.BETA * order.days_backordered)
    else:  # additive (default)
        w += D.BETA * order.days_backordered
    w *= boosts.get(order.customer_id, 1.0)
    return w


def tardiness(order: Order, unit: Unit) -> int:
    return max(0, unit.arrival_week - order.promised_week)


def arc_cost_float(
    order: Order,
    unit: Unit,
    lam: int,
    boosts: dict[str, float],
    not_before_week: Optional[int],
) -> float:
    """§2 cost for one order->unit arc, in float space (scaled to int later)."""
    late = tardiness(order, unit)
    cost = effective_weight(order, boosts) * (late ** D.CONVEX_EXPONENT)

    # λ additive term, gated by the fence (liquid => free to change week).
    if unit.arrival_week != order.promised_week and fence_of(order) == "slushy":
        cost += lam

    # Soft instruction pin: 'defer'/not_before violated by an early arrival.
    if not_before_week is not None and unit.arrival_week < not_before_week:
        cost += D.SOFT_PIN_COST
    return cost


# --- Repair problem partition (§1, §5) ---------------------------------------

@dataclass
class RepairPlan:
    """The pinned/free partition + the combined override, resolved once."""
    pinned: dict[int, int]          # order_id -> unit_id (hard, kept as-is)
    free_orders: list[int]          # order_ids to (re)match
    free_units: list[int]           # unit_ids available to the free orders
    boosts: dict[str, float]        # customer_id -> weight multiplier
    lam_default: Optional[int]      # ledger-supplied λ (sweep still explores all)
    not_before: dict[int, int]      # order_id -> earliest allowed arrival week
    forbid_no_move: set[int]        # orders explicitly pinned by instruction


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
    disrupted = set(snapshot.disruption["disrupted_orders"])

    # Instruction-driven sets from the combined override.
    boosts = _combined_boosts(override)
    lam_default = override.get("lambda")
    not_before: dict[int, int] = {}
    deferred: set[int] = set()
    for p in override.get("pins", []):
        if p.get("action") == "defer":
            oid = int(p["order"])
            deferred.add(oid)
            if p.get("not_before"):
                not_before[oid] = parse_week_label(p["not_before"])
    forbid_no_move = {
        int(f["order"]) for f in override.get("forbid", []) if f.get("action") == "no_move"
    }

    # A unit is committed (physically fixed) if its state is a commit point.
    def committed(uid: int) -> bool:
        return units[uid].state in D.COMMIT_POINT_STATES

    incumbent_unit = incumbent  # order_id -> unit_id

    # Free orders: disrupted or actively deferred, EXCEPT
    #   - frozen orders (can't move, §2 time fence),
    #   - orders explicitly pinned no_move,
    #   - orders riding a committed unit (can't recall an in-prep/shipped unit).
    # Plus any order the incumbent left unassigned (must get a unit).
    free_orders: list[int] = []
    for oid, o in orders.items():
        assigned = incumbent_unit.get(oid)
        wants_move = (oid in disrupted) or (oid in deferred)
        if oid in forbid_no_move:
            continue
        if fence_of(o) == "frozen":
            continue
        if assigned is not None and committed(assigned):
            continue
        if wants_move or assigned is None:
            free_orders.append(oid)
    free_orders.sort()  # §4 fixed key

    free_set = set(free_orders)
    # Pinned = everyone else keeps their incumbent unit (if any).
    pinned = {oid: uid for oid, uid in incumbent.items() if oid not in free_set}

    # Free units: not consumed by a pinned assignment, and not committed.
    consumed = set(pinned.values())
    free_units = [
        uid
        for uid, u in units.items()
        if uid not in consumed and u.state not in D.COMMIT_POINT_STATES
    ]
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
    plan: dict[int, int]                 # order_id -> unit_id (full: pinned+free)
    unfilled: list[int]                  # free orders routed to backorder
    node_index: dict[int, object]        # position -> ('order'|'unit', real_id)
    n_changes: int                       # free orders whose unit differs from incumbent
    weighted_late_days: float            # Σ W(o)·tardiness over ALL orders
    objective_micro: int                 # solver objective in scaled int space
    self_check: dict


def _solve_one(
    snapshot: Snapshot,
    rp: RepairPlan,
    lam: int,
    cache: ResidualCache,
) -> SolveResult:
    orders = snapshot.order_by_id()
    units = snapshot.unit_by_id()

    # §4 integer node positions from the fixed sort already applied to rp.
    # 0=S, 1=T, 2=D(backorder); orders then units follow.
    S, T, DUMMY = 0, 1, 2
    node_index: dict[int, object] = {}
    order_pos: dict[int, int] = {}
    unit_pos: dict[int, int] = {}
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

    # order -> compatible free unit (cap 1, §2 cost) ; order -> backorder dummy
    for oid in rp.free_orders:
        o = orders[oid]
        nb = rp.not_before.get(oid)
        for uid in rp.free_units:
            u = units[uid]
            if not resolve_compatibility(u.spec, o.spec, cache):
                continue  # incompatible -> no arc (keeps graph sparse, §4)
            c = arc_cost_float(o, u, lam, rp.boosts, nb)
            smcf.add_arc_with_capacity_and_unit_cost(
                order_pos[oid], unit_pos[uid], 1, int(round(c * COST_SCALE))
            )
        smcf.add_arc_with_capacity_and_unit_cost(
            order_pos[oid], DUMMY, 1, int(round(BACKORDER_COST * COST_SCALE))
        )

    # free unit -> T (cap 1) ; backorder dummy -> T (cap N)
    for uid in rp.free_units:
        smcf.add_arc_with_capacity_and_unit_cost(unit_pos[uid], T, 1, 0)
    smcf.add_arc_with_capacity_and_unit_cost(DUMMY, T, N, 0)

    smcf.set_node_supply(S, N)
    smcf.set_node_supply(T, -N)

    status = smcf.solve()
    if status != smcf.OPTIMAL:
        raise RuntimeError(f"min-cost-flow did not solve to optimality: status={status}")

    # Read back flow -> real keys (§4).
    plan: dict[int, int] = dict(rp.pinned)
    unfilled: list[int] = []
    assigned_free: dict[int, int] = {}
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

    n_changes = sum(
        1
        for oid in rp.free_orders
        if snapshot.incumbent.get(oid) != plan.get(oid)
    )

    weighted_late = 0.0
    for oid, o in orders.items():
        uid = plan.get(oid)
        if uid is not None:
            weighted_late += effective_weight(o, rp.boosts) * tardiness(o, units[uid])

    check = _self_check(snapshot, rp, plan, unfilled, cache)
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


def _self_check(snapshot, rp, plan, unfilled, cache) -> dict:
    """§8.5 hard-constraint self-check. Returns findings; never silently relaxes."""
    orders = snapshot.order_by_id()
    units = snapshot.unit_by_id()
    violations: list[str] = []

    # No committed/frozen unit reassigned away from its incumbent order.
    for oid, uid in snapshot.incumbent.items():
        if units[uid].state in D.COMMIT_POINT_STATES and plan.get(oid) != uid:
            violations.append(f"committed unit {uid} moved off order {oid}")
    # No spec violation on any assignment.
    for oid, uid in plan.items():
        if not resolve_compatibility(units[uid].spec, orders[oid].spec, cache):
            violations.append(f"order {oid} assigned incompatible unit {uid}")
    # Every order has exactly one unit (or is a surfaced backorder).
    every_order_placed = all(
        (oid in plan) or (oid in unfilled) for oid in orders
    )
    # No unit double-booked.
    used = [uid for uid in plan.values()]
    double_booked = len(used) != len(set(used))
    if double_booked:
        violations.append("a unit is assigned to more than one order")

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
    override: Optional[dict] = None,
    cache: Optional[ResidualCache] = None,
    lam: Optional[int] = None,
) -> SolveResult:
    """Single deterministic solve at one λ (default: ledger λ, else first sweep value)."""
    override = override or {}
    cache = cache or ResidualCache.load(None)
    rp = partition(snapshot, override)
    if lam is None:
        lam = rp.lam_default if rp.lam_default is not None else D.LAMBDA_SWEEP[0]
    return _solve_one(snapshot, rp, int(lam), cache)


def lambda_sweep(
    snapshot: Snapshot,
    override: Optional[dict] = None,
    cache: Optional[ResidualCache] = None,
    lambdas=D.LAMBDA_SWEEP,
) -> tuple[list[SweepPoint], dict[int, SolveResult]]:
    """Re-solve across λ (same network, only some arc costs change) -> Pareto
    frontier of (changes vs weighted late-days). Returns the frontier points and
    the full per-λ results keyed by λ."""
    override = override or {}
    cache = cache or ResidualCache.load(None)
    rp = partition(snapshot, override)
    points: list[SweepPoint] = []
    results: dict[int, SolveResult] = {}
    for lam in lambdas:
        res = _solve_one(snapshot, rp, int(lam), cache)
        results[int(lam)] = res
        points.append(
            SweepPoint(int(lam), res.n_changes, res.weighted_late_days, len(res.unfilled))
        )
    return points, results

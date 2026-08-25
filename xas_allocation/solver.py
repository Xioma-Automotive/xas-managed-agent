"""Deterministic min-cost-flow repair solver (OR-Tools).

The agent NEVER implements the algorithm — it builds the network and calls
``ortools.graph.SimpleMinCostFlow`` (§3). Everything here is a pure function of
(snapshot, combined_override, lambda): same inputs -> byte-identical plan (the
core invariant).

Design highlights, mapped to the spec:
- §4  Integer node positions from a FIXED sort (order key / vehicle_id asc); real
      keys recovered through explicit lookup tables before anything leaves here.
- §1  Repair, don't re-solve: pin the whole incumbent, free ONLY the disrupted
      orders, re-match just those. Change count is structural.
- §2  cost(o->u) = W(o)·tardiness_days^1.5 + λ(fence)·[eta(u) ≠ delivery_date(o)].
- §5  Frozen-fence orders are pre-committed OUT of the graph; instruction
      pins/defers/forbids are large finite soft penalties (DECIDE-4 / DECIDE-8) so
      a conflict surfaces as a cost line, never a crash.
- §5b Moving an allocation OFF its current binding costs a finite BREAK_COST
      (DECIDE-3): hard (real vehicle) is expensive-but-movable, soft (future
      vehicle) is free. No hard wall — a hard allocation can be bumped 'for
      the sake of another' order when the tardiness saved exceeds the break cost.
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
from .snapshot import Order, Snapshot, Unit, parse_date

# Float costs are scaled to ints for the integer-only min-cost-flow.
COST_SCALE = 1000
# Leaving an order unfilled must cost more than violating any soft instruction
# pin, so the solver fills where it can and only backorders as a last resort.
BACKORDER_COST = D.SOFT_PIN_COST * 10.0


# --- Fence + cost model -------------------------------------------------------


def fence_of(order: Order, now: date) -> str:
    """DECIDE-2 time fence by days-until-delivery: frozen / slushy / liquid."""
    days_out = (order.delivery_date - now).days
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

    boosts maps customer_id -> multiplier (from override 'boosts')."""
    escalation = 1 + D.ALPHA * order.n_prior_delays + D.GAMMA * order.times_rescheduled
    w = D.PRIORITY_WEIGHT[order.priority] * escalation
    if D.AGING_MODE == "multiplicative":
        w *= 1 + D.BETA * order.days_backordered
    else:  # additive (default)
        w += D.BETA * order.days_backordered
    w *= boosts.get(order.customer_id, 1.0)
    return w


def tardiness(order: Order, unit: Unit) -> int:
    """Days late = how far the vehicle's ETA runs past the promise (0 if on/before)."""
    return max(0, (unit.eta_dealer - order.delivery_date).days)


def earliness(order: Order, unit: Unit) -> int:
    """Days early = how far the delivery lands before the promise (0 if on/after)."""
    return max(0, (order.delivery_date - unit.eta_dealer).days)


def scale_units(days: int, unit_days: int) -> int:
    """Round a non-negative day count UP to whole time-scale units (DECIDE-14).

    unit_days=1 (day scale) is the identity, so day-scale behaviour is unchanged.
    Round-up ('bill by the week'): 1..unit_days days => 1 unit, so any nonzero gap
    is at least one unit and a coarse scale never under-states a gap."""
    if days <= 0:
        return 0
    return (days + unit_days - 1) // unit_days


def time_scale_of(override: dict | None) -> tuple[str, int]:
    """The active time scale as (name, nominal days per unit) — DECIDE-14.

    The ONE resolver: the solver costs in these units and the report speaks them,
    so an unknown name has to fall back to the default in exactly one place or a
    plan and its report can disagree about what "2 weeks late" means."""
    scale = (override or {}).get("time_scale") or D.DEFAULT_TIME_SCALE
    if scale not in D.SCALE_DAYS:
        scale = D.DEFAULT_TIME_SCALE
    return scale, D.SCALE_DAYS[scale]


def arc_cost_float(
    order: Order,
    unit: Unit,
    lam: int,
    now: date,
    boosts: dict[str, float],
    not_before: date | None,
    unit_days: int = 1,
) -> float:
    """§2 cost for one order->unit arc, in float space (scaled to int later).

    Two time-aware pieces layer in here (DECIDE-14 / DECIDE-15) without colliding:
      * every day-gap is quantized to whole units by ``unit_days`` (round up), so
        the solver reasons at the planner's scale; at day scale this is a no-op;
      * a gentle LINEAR earliness term discourages needlessly-early cars, small
        enough that the convex lateness term always wins for comparable gaps."""
    w = effective_weight(order, boosts)

    # Lateness (convex) — the dominant term, measured in units.
    late_units = scale_units(tardiness(order, unit), unit_days)
    cost = w * (late_units**D.CONVEX_EXPONENT)

    # Earliness (linear, small) — a car that lands too early ties up inventory.
    early_units = scale_units(earliness(order, unit), unit_days)
    if early_units:
        cost += D.EARLY_WEIGHT * w * early_units

    # λ additive term, gated by the fence (liquid => free to change the date).
    # "Changed" means the delivery differs from the promise by at least one unit,
    # so a within-unit shuffle isn't counted as churn at a coarse scale.
    gap_days = abs((unit.eta_dealer - order.delivery_date).days)
    if gap_days >= unit_days and fence_of(order, now) == "slushy":
        cost += lam

    # Soft instruction pin: 'defer'/not_before violated by an early arrival.
    if not_before is not None and unit.eta_dealer < not_before:
        cost += D.SOFT_PIN_COST
    return cost


def break_cost_of(order: Order, incumbent_unit: Unit | None, break_cost: dict[str, float]) -> float:
    """Cost to move ``order`` OFF its current binding (DECIDE-3).

    Zero when it has no binding, OR when that binding is already LATE — a broken
    promise protects nothing, so re-allocating a disrupted order is free. Only
    displacing an ON-TIME binding costs: BREAK_COST['hard'] for a real vehicle,
    BREAK_COST['soft'] for a future vehicle. This is what makes "bump someone for the
    sake of another" price the *victim* (whose kept promise is disturbed), not the
    disrupted order being rescued."""
    if incumbent_unit is None or tardiness(order, incumbent_unit) > 0:
        return 0.0
    return break_cost["hard"] if incumbent_unit.is_hard else break_cost["soft"]


def repairability(order: Order, now: date, incumbent_unit: Unit | None) -> str:
    """Can a broken order even be re-slotted? Returns 'movable' | 'frozen' — the
    only hard wall left is the frozen fence (DECIDE-2), surfaced for the
    discrepancy map so the planner learns on turn 1 (not turn 4) that an order is
    stuck. A real-vehicle (hard) allocation is NOT stuck: it is movable, just
    expensive (DECIDE-3), so it reads 'movable' — the break cost, not a wall,
    decides whether the solver actually moves it."""
    # An order with NO car has no commitment for the fence to protect, and
    # calling it frozen would report "locked in" for something that is simply
    # unallocated. Only an existing allocation can be locked in.
    if incumbent_unit is not None and fence_of(order, now) == "frozen":
        return "frozen"
    return "movable"


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

    pinned: dict[str, str]  # order_key -> vehicle_id (hard, kept as-is)
    free_orders: list[str]  # order_keys to (re)match
    free_units: list[str]  # vehicle_ids available to the free orders
    boosts: dict[str, float]  # customer_id -> weight multiplier
    lam_default: int | None  # override-supplied λ (sweep still explores all)
    # Whatever name the pin used (car / line / VSO) -> earliest allowed date.
    # Read through `not_before_for`, never by exact order key.
    not_before: dict[str, date]
    forbid_no_move: set[str]  # orders explicitly pinned by instruction
    unit_days: int  # DECIDE-14 time-scale resolution (days per unit; 1 = day scale)
    break_cost: dict[str, float]  # DECIDE-3 cost to move off a hard/soft binding


def _combined_boosts(override: dict) -> dict[str, float]:
    boosts: dict[str, float] = {}
    for b in override.get("boosts", []):
        cid = b.get("customer") or b.get("customer_id")
        if cid is None:
            continue
        boosts[cid] = boosts.get(cid, 1.0) * float(b.get("weight_mult", 1.0))
    return boosts


_FILTER_DIMS = ("customers", "models", "orders", "from_date", "to_date")


def _filter_active(filt: dict) -> bool:
    """A filter with at least one dimension set is in effect (used by scope + bump)."""
    return bool(filt) and any(filt.get(k) for k in _FILTER_DIMS)


def names_order(order: Order, names: set[str] | dict) -> bool:
    """Whether a set of order NAMES refers to this order, at any of its three key
    levels: one car, its car line, or its whole VSO.

    Every place an order is named by a string has to go through this. A pin, a
    forbid, or the disruption manifest naming the LINE (`VSO-4000-1`) would
    otherwise never match a per-car key (`VSO-4000-1-1`) and the instruction would
    silently do nothing — the schema tells the agent to prefer the line level, so
    an exact-match lookup makes that advice a trap."""
    return bool(names) and bool({order.key, order.line_key, order.so_id} & set(names))


def disrupted_order_keys(snapshot: Snapshot) -> set[str]:
    """The disruption manifest resolved to real order keys.

    What slips is a VEHICLE: a VPO/VGR shipment runs late, so the cars on it do,
    and a VSO line is affected only through the vehicle allocated to it. A line's
    cars can come from different shipments, so the manifest is derived per CAR
    (`xas_allocation.flatten`) and normally needs no resolving at all.

    This exists for a manifest named more coarsely — a line, or a whole VSO, as a
    hand-written override or an older snapshot may carry. Comparing such names raw
    against per-car keys is the silent version of the bug: nothing matches, nothing
    is freed, and the report reads "0 of 0 delayed orders" over a broken book."""
    names = set(snapshot.disruption.get("disrupted_orders", []))
    return {o.key for o in snapshot.orders if names_order(o, names)}


def not_before_for(not_before: dict, order: Order) -> date | None:
    """The `not_before` a pin set for this order, matched most-specific-first.

    A pin on one car beats one on its line, which beats one on the whole VSO."""
    for name in (order.key, order.line_key, order.so_id):
        if name in not_before:
            return not_before[name]
    return None


def _matches(order: Order, filt: dict) -> bool:
    """AND across whichever filter dimensions are set. Empty dimension = no filter.

    Customers match by id OR display name; `orders` matches at any of the THREE
    levels a key has — one car (`{so_id}-{line}-{n}`), the car line
    (`{so_id}-{line}`, so a qty-3 line can be named as a whole), or the bare
    `so_id` (the whole VSO). The line level is the one a planner actually uses:
    the per-car index is an arbitrary label. A date range is against
    delivery_date. Used by both `scope` (defines the working set) and `bump`
    (authorizes which untouched rows the solver may displace)."""
    customers = filt.get("customers")
    if customers and order.customer_id not in customers and order.customer not in customers:
        return False
    models = filt.get("models")
    if models and order.sales_model not in models:
        return False
    orders = filt.get("orders")
    if orders and not names_order(order, orders):
        return False
    if filt.get("from_date") and order.delivery_date < parse_date(filt["from_date"]):
        return False
    return not (filt.get("to_date") and order.delivery_date > parse_date(filt["to_date"]))


def partition(snapshot: Snapshot, override: dict) -> RepairPlan:
    """Decide what is pinned vs free (§1, §5) from data rules + the override."""
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    incumbent = dict(snapshot.incumbent)
    disrupted = disrupted_order_keys(snapshot)

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
    scope = override.get("scope") or {}
    scoped = _filter_active(scope)
    bump = override.get("bump") or {}
    bumpable = _filter_active(bump)

    # Free orders — what the solver may re-allocate. The base set is either:
    #   - SCOPED: exactly the rows matching the scope filter (a deliberate slice,
    #     e.g. "all Colmobil orders for August"); everything else stays pinned so
    #     the fix doesn't disturb the rest of the book.
    #   - UNSCOPED (default): disrupted or actively-deferred rows, plus any the
    #     incumbent left unassigned.
    # PLUS the `bump` set: untouched rows the planner has EXPLICITLY authorized
    # the solver to displace (DECIDE-13). The solver only moves a bumpable row if
    # doing so lowers total cost — so it bumps a low-priority row to rescue a
    # higher-priority one, never gratuitously, and the fairness term protects
    # already-rescheduled rows. Nothing is ever bumped without this authorization.
    # The only rows that can NEVER move: frozen-fence rows and explicit no_move
    # pins. A row riding a real (hard) vehicle CAN move — it just costs
    # BREAK_COST['hard'] (DECIDE-3), so it is freed like any other and the cost,
    # not a wall, decides whether the solver actually moves it.
    free_orders: list[str] = []
    for oid, o in orders.items():
        assigned = incumbent.get(oid)
        if names_order(o, forbid_no_move):
            continue
        # The frozen fence stops an EXISTING allocation being churned this close
        # to delivery. An order with no car has nothing to protect, and refusing
        # to free it means it can never be filled at all — a permanent backorder
        # manufactured by a rule about churn. Qty expansion makes this routine:
        # the cars of a qty-3 line beyond its one resolvable vehicle all arrive
        # unallocated. Left frozen they land in neither `plan` nor `unfilled`, and
        # `_self_check` then reports not-ok with an empty violation list.
        if assigned is not None and fence_of(o, snapshot.now) == "frozen":
            continue
        if scoped:
            include = _matches(o, scope)
        else:
            include = (oid in disrupted) or names_order(o, deferred) or (assigned is None)
        if not include and bumpable:
            include = _matches(o, bump)
        if include:
            free_orders.append(oid)
    free_orders.sort()  # §4 fixed key

    free_set = set(free_orders)
    # Pinned = everyone else keeps their incumbent vehicle (if any).
    pinned = {oid: uid for oid, uid in incumbent.items() if oid not in free_set}

    # Free vehicles: any not consumed by a pinned assignment. A real (hard)
    # vehicle is NOT walled off — freeing its order (above) frees it here too, so
    # it can be reassigned at BREAK_COST['hard'] (DECIDE-3).
    consumed = set(pinned.values())
    free_units = [uid for uid in units if uid not in consumed]
    free_units.sort()  # §4 fixed key

    break_cost = {**D.BREAK_COST, **(override.get("break_cost") or {})}

    return RepairPlan(
        pinned=pinned,
        free_orders=free_orders,
        free_units=free_units,
        boosts=boosts,
        lam_default=lam_default,
        not_before=not_before,
        forbid_no_move=forbid_no_move,
        unit_days=time_scale_of(override)[1],
        break_cost=break_cost,
    )


# --- Solve one λ --------------------------------------------------------------


@dataclass
class SolveResult:
    lam: int
    plan: dict[str, str]  # order_key -> vehicle_id (full: pinned+free)
    unfilled: list[str]  # free orders routed to backorder
    n_changes: int  # free orders whose vehicle differs from incumbent
    weighted_late_days: float  # Σ W(o)·tardiness over ALL orders
    objective_micro: int  # solver objective in scaled int space
    self_check: dict


def _solve_one(snapshot: Snapshot, rp: RepairPlan, lam: int) -> SolveResult:
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()

    # §4 integer node positions from the fixed sort already applied to rp.
    # 0=S, 1=T, 2=D(backorder); orders then units follow.
    S, T, DUMMY = 0, 1, 2
    order_pos = {oid: 3 + i for i, oid in enumerate(rp.free_orders)}
    unit_pos = {uid: 3 + len(order_pos) + i for i, uid in enumerate(rp.free_units)}

    smcf = min_cost_flow.SimpleMinCostFlow()
    N = len(rp.free_orders)

    # S -> each free order (cap 1, cost 0)
    for oid in rp.free_orders:
        smcf.add_arc_with_capacity_and_unit_cost(S, order_pos[oid], 1, 0)

    # order -> compatible free vehicle (cap 1, §2 cost) ; order -> backorder dummy.
    # Each assignment arc is tagged with the (order, vehicle-or-backorder) it means
    # as it is added, so reading the flow back needs no reverse node lookup (§4).
    choice_of: dict[int, tuple[str, str | None]] = {}
    for oid in rp.free_orders:
        o = orders[oid]
        nb = not_before_for(rp.not_before, orders[oid])
        # Break cost (DECIDE-3): displacing an on-time binding costs
        # BREAK_COST[hard|soft]; keeping it, having none, or leaving an
        # already-late one is free. Charged to the order that gives up its
        # vehicle — so bumping an on-time hard allocation for another order pays
        # the hard break, and the solver only does so when the tardiness saved
        # elsewhere outweighs it.
        inc_uid = snapshot.incumbent.get(oid)
        brk = break_cost_of(o, units.get(inc_uid) if inc_uid else None, rp.break_cost)
        for uid in rp.free_units:
            u = units[uid]
            if not eligible(o, u):
                continue  # incompatible -> no arc (keeps graph sparse, §4)
            c = arc_cost_float(o, u, lam, snapshot.now, rp.boosts, nb, rp.unit_days)
            if uid != inc_uid:
                c += brk  # this assignment breaks the current binding
            arc = smcf.add_arc_with_capacity_and_unit_cost(
                order_pos[oid], unit_pos[uid], 1, round(c * COST_SCALE)
            )
            choice_of[arc] = (oid, uid)
        # Backordering also gives up the incumbent binding -> also pays the break.
        arc = smcf.add_arc_with_capacity_and_unit_cost(
            order_pos[oid], DUMMY, 1, round((BACKORDER_COST + brk) * COST_SCALE)
        )
        choice_of[arc] = (oid, None)

    # free vehicle -> T (cap 1) ; backorder dummy -> T (cap N)
    for uid in rp.free_units:
        smcf.add_arc_with_capacity_and_unit_cost(unit_pos[uid], T, 1, 0)
    smcf.add_arc_with_capacity_and_unit_cost(DUMMY, T, N, 0)

    smcf.set_node_supply(S, N)
    smcf.set_node_supply(T, -N)

    status = smcf.solve()
    if status != smcf.OPTIMAL:
        raise RuntimeError(f"min-cost-flow did not solve to optimality: status={status}")

    # Read back flow -> real keys (§4), straight off the arc tags.
    plan: dict[str, str] = dict(rp.pinned)
    unfilled: list[str] = []
    for arc, (oid, uid) in choice_of.items():
        if smcf.flow(arc) <= 0:
            continue
        if uid is None:
            unfilled.append(oid)
        else:
            plan[oid] = uid
    unfilled.sort()

    n_changes = sum(1 for oid in rp.free_orders if snapshot.incumbent.get(oid) != plan.get(oid))

    weighted_late = sum(
        effective_weight(o, rp.boosts) * tardiness(o, units[plan[oid]])
        for oid, o in orders.items()
        if oid in plan
    )
    return SolveResult(
        lam=lam,
        plan=plan,
        unfilled=unfilled,
        n_changes=n_changes,
        weighted_late_days=round(weighted_late, 4),
        objective_micro=smcf.optimal_cost(),
        self_check=_self_check(snapshot, plan, unfilled),
    )


def _self_check(snapshot: Snapshot, plan: dict, unfilled: list) -> dict:
    """§8.5 hard-constraint self-check. Returns findings; never silently relaxes."""
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    violations: list[str] = []

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
    """Single deterministic solve at one λ (default: override λ, else first sweep value)."""
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

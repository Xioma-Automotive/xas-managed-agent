"""Deterministic min-cost-flow repair solver (OR-Tools).

The agent NEVER implements the algorithm — it builds the network and calls
``ortools.graph.SimpleMinCostFlow`` (§3). Everything here is a pure function of
(snapshot, combined_override, churn_price): same inputs -> byte-identical plan
(the core invariant). Every NUMBER it prices with comes from
``solver_config.yaml`` beside this file; there are no cost literals in the code.

Two halves, and only the second is arithmetic:

* ``partition`` — who may move. Reads the snapshot and the override, returns the
  orders that may be re-allocated and the cars available to them. No maths.
  Everything off those lists keeps what it has and its car is off the table.
* ``_solve_one`` — the arithmetic. Prices every pairing whose spec matches
  exactly, hands the graph to min-cost-flow, reads the assignment back, checks it.

Design highlights, mapped to the spec:
- §4  Integer node positions from a FIXED sort (order key / vehicle_id asc); real
      keys recovered through explicit lookup tables before anything leaves here.
- §1  Repair, don't re-solve: pin the whole incumbent, free ONLY the orders that
      need help, re-match just those. Change count is structural.
- §2  cost(o->u) = W(o)·late^e + early_weight·W(o)·early, plus churn_price and
      BREAK_COST once each if this pairing changes the order's car.
- §5b Moving an allocation OFF its current binding costs a finite break cost:
      hard (real vehicle) is expensive-but-movable, soft (future vehicle) is
      free. No hard wall — a hard allocation can be bumped 'for the sake of
      another' order when the tardiness saved exceeds the break cost.
- A per-order dummy arc keeps the flow always feasible; an order routed to it is
  an explicitly surfaced order with no car, not a silent drop.

Eligibility (the sparse arc rule) is a HARD ``sales_model`` equality, computed
here and never stored — the old fuzzy spec-match + LLM residual is gone. There
is no model judgment left in the data path.

Retired 2026-08-26, and none of it is coming back as a smaller version:

* **The time fence** (frozen ≤14d / slushy 15–42d). It fired BEFORE the
  authorisation check, so it silently cancelled displacements a planner had
  explicitly authorised. What everyone thought it protected — a settled, on-time
  order — is already protected by the free-set rule below: such an order is
  simply never freed.
* **The soft instruction pin** and its ``not_before`` date. It was the only price
  on an instruction rather than on an outcome, and deferring an order never moved
  its promised date, so a deferred order paid a lateness charge and a pin charge
  at once. Pushing an order back is a NEW PROMISED DATE, which the lateness and
  earliness terms already price correctly.
* **The three weight-escalation terms** (prior delays, back-order aging,
  reschedule fairness). Every one read a field that is zero on every row of real
  data; two of them existed only because the fabricated generator rolled a die.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from ortools.graph.python import min_cost_flow

from .snapshot import Order, Snapshot, Unit, parse_date

# Every solver parameter, read ONCE at import. Loading it per solve would let two
# turns of one conversation price the same override differently — the quiet way
# to break the invariant. Editing the YAML means re-running setup_agent.py, the
# same as editing this file: it ships in the skill bundle.
CONFIG_PATH = Path(__file__).resolve().parent / "solver_config.yaml"
CFG: dict = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

SOLVER_VERSION: str = CFG["version"]
CHURN_PRICE_SWEEP: tuple[int, ...] = tuple(CFG["churn_price_sweep"])
DEFAULT_STEP: str = CFG["default_priority_step"]


def weight_of_step(step: str) -> float:
    """A named priority step -> its weight multiplier.

    Raises on a name the config does not define: falling back to normal would
    make a mistyped steering instruction look like it had been applied."""
    steps = CFG["priority_steps"]
    if step not in steps:
        raise ValueError(f"unknown priority step {step!r} — the config defines: {', '.join(steps)}")
    return float(steps[step])


# --- Cost model ---------------------------------------------------------------


def effective_weight(order: Order, priority: dict[str, str]) -> float:
    """W(o) — how much this order's lateness counts.

    One lookup, and deliberately so. The step is whatever the planner set for
    this order this turn (override key ``priority``), else the config's default;
    the config turns it into a weight. There is no history riding on it any
    more — the prior-delay, aging and reschedule-fairness terms all read fields
    that are zero on every real row."""
    return weight_of_step(priority.get(order.key, DEFAULT_STEP))


def tardiness(order: Order, unit: Unit) -> int:
    """Days late = how far the vehicle's ETA runs past the promise (0 if on/before)."""
    return max(0, (unit.eta_dealer - order.delivery_date).days)


def earliness(order: Order, unit: Unit) -> int:
    """Days early = how far the delivery lands before the promise (0 if on/after)."""
    return max(0, (order.delivery_date - unit.eta_dealer).days)


def arc_cost_float(order: Order, unit: Unit, priority: dict[str, str]) -> float:
    """§2 cost for one order->unit pairing, in float space (scaled to int later).

    Lateness dominates and curves upward, so two orders four days late cost less
    than one order eight days late — that is "spread the pain" in arithmetic.
    Earliness is charged gently and linearly, so a car landing months early is
    never read as a win, but a slightly-early car still beats a late one.

    The price of CHANGING something is not here: churn and break cost depend on
    what the order already had, not on the pairing, so ``_solve_one`` adds them
    once to the arcs that actually move a car."""
    w = effective_weight(order, priority)
    cost = w * (tardiness(order, unit) ** CFG["convex_exponent"])
    cost += CFG["early_weight"] * w * earliness(order, unit)
    return cost


def break_cost_of(order: Order, incumbent_unit: Unit | None) -> float:
    """Cost to move ``order`` OFF its current binding.

    Zero when it has no binding, OR when that binding is already LATE — a broken
    promise protects nothing, so re-allocating an order that is already in
    trouble is free. Only displacing an ON-TIME binding costs: ``hard`` for a
    real vehicle, ``soft`` for a future one. This is what makes "bump someone for
    the sake of another" price the *victim* (whose kept promise is disturbed),
    not the order being rescued."""
    if incumbent_unit is None or tardiness(order, incumbent_unit) > 0:
        return 0.0
    return CFG["break_cost"]["hard" if incumbent_unit.is_hard else "soft"]


def eligible(order: Order, unit: Unit) -> bool:
    """The sparse arc rule (computed, never stored): hard sales_model equality.

    DECIDE-10: a reserved_for_customer term would AND in here once modelled.
    Lateness is NOT a feasibility gate — it is priced in ``arc_cost_float`` so a
    slightly-late vehicle can still be placed instead of leaving an order with
    no car at all."""
    return order.sales_model == unit.sales_model


# --- Repair problem partition (§1, §5) ---------------------------------------


@dataclass
class RepairPlan:
    """The pinned/free partition + the combined override, resolved once."""

    pinned: dict[str, str]  # order_key -> vehicle_id (kept as-is)
    free_orders: list[str]  # order_keys to (re)match
    free_units: list[str]  # vehicle_ids available to the free orders
    priority: dict[str, str]  # order_key -> named priority step, from the override
    churn_default: int | None  # override-supplied churn price (sweep still explores all)


_FILTER_DIMS = ("customers", "models", "orders", "from_date", "to_date")


def _filter_active(filt: dict) -> bool:
    """A filter with at least one dimension set is in effect."""
    return bool(filt) and any(filt.get(k) for k in _FILTER_DIMS)


def names_order(order: Order, names: set[str] | dict | list) -> bool:
    """Whether a set of order NAMES refers to this order, at either key level:
    the car line itself, or its whole VSO.

    Every place an order is named by a string has to go through this. A priority
    step, a `never`, or the disruption manifest naming the whole VSO (`VSO-4000`)
    would otherwise never match a line key (`VSO-4000-1`) and the instruction
    would silently do nothing."""
    return bool(names) and bool({order.key, order.so_id} & set(names))


def disrupted_order_keys(snapshot: Snapshot) -> set[str]:
    """The disruption manifest resolved to real order keys.

    What slips is a VEHICLE: a VPO/VGR shipment runs late, so the cars on it do,
    and an order is affected only through the vehicle allocated to it. The manifest
    is derived at order grain (`xas_allocation.flatten`) and normally needs no
    resolving at all.

    This exists for a manifest named more coarsely — a whole VSO, as a hand-written
    override or an older snapshot may carry. Comparing such names raw against order
    keys is the silent version of the bug: nothing matches, nothing is freed, and
    the report reads "0 of 0 delayed orders" over a broken book."""
    names = set(snapshot.disruption.get("disrupted_orders", []))
    return {o.key for o in snapshot.orders if names_order(o, names)}


def _matches(order: Order, filt: dict) -> bool:
    """AND across whichever filter dimensions are set. Empty dimension = no filter.

    Customers match by id OR display name; `orders` matches at either level a key
    has — the car line (`{so_id}-{line}`) or the bare `so_id` (the whole VSO). A
    date range is against delivery_date. Used by both halves of `may_move` that
    take a filter (`only` and `also`)."""
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


def _combined_priority(snapshot: Snapshot, override: dict) -> dict[str, str]:
    """order_key -> named priority step, from the override.

    Priority is a LEVER, not a column: nothing on the record says an order
    matters more, so every order starts at the config's default step and only
    what the planner named this turn moves off it. A step naming a whole VSO
    applies to all of its lines (`names_order`); the last entry naming an order
    wins, so restating one replaces it rather than compounding.

    Steps are resolved to a weight HERE, once, purely to reject an unknown name
    while the override is being compiled — the report and the cost model then
    both read the name, so they can never disagree about what a planner asked
    for."""
    steps: dict[str, str] = {}
    for entry in override.get("priority") or []:
        name, step = str(entry["order"]), str(entry["step"])
        weight_of_step(step)  # raises on a step the config does not define
        for o in snapshot.orders:
            if names_order(o, {name}):
                steps[o.key] = step
    return steps


def partition(snapshot: Snapshot, override: dict) -> RepairPlan:
    """Decide what is pinned vs free (§1, §5) from data rules + the override.

    The DEFAULT free set is the orders that need help: the ones whose car now
    lands past the promise, plus the ones with no car at all. A settled, on-time
    order is not in it — that, and nothing else, is what protects it. There is no
    second wall: the time fence that used to sit here fired before the
    authorisation check below and silently cancelled bumps a planner had asked
    for.

    ``may_move`` then adjusts that set, and PRECEDENCE IS PART OF THE CONTRACT —
    ``never`` beats ``only`` beats ``also``:

    * ``only`` NARROWS: with it set, nothing outside the filter moves, however
      else the turn was steered. ("Just fix August.") It narrows the default
      rather than replacing it, so it can no longer free a settled order nobody
      authorised — the hole the old ``scope`` key had.
    * ``also`` WIDENS, inside ``only``: untouched orders the planner has
      EXPLICITLY authorised the solver to displace to rescue someone late
      (DECIDE-13). The solver moves one only if it lowers total cost, and pays
      ``break_cost`` to do it. This is the one place permission to displace is
      given; nothing is ever bumped without it.
    * ``never`` REMOVES, absolutely. "I already called that customer, leave it."
      It is the only way to protect an order that IS in trouble, and it beats an
      explicit permission granted in the same breath.
    """
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    incumbent = dict(snapshot.incumbent)
    disrupted = disrupted_order_keys(snapshot)

    may_move = override.get("may_move") or {}
    only = may_move.get("only") or {}
    also = may_move.get("also") or {}
    never = set(may_move.get("never") or [])
    narrowed = _filter_active(only)
    widened = _filter_active(also)

    free_orders: list[str] = []
    for oid, o in orders.items():
        if names_order(o, never):
            continue
        needs_help = (oid in disrupted) or (incumbent.get(oid) is None)
        if not (needs_help or (widened and _matches(o, also))):
            continue
        if narrowed and not _matches(o, only):
            continue
        free_orders.append(oid)
    free_orders.sort()  # §4 fixed key

    free_set = set(free_orders)
    # Pinned = everyone else keeps their incumbent vehicle (if any).
    pinned = {oid: uid for oid, uid in incumbent.items() if oid not in free_set}

    # Free vehicles: any not consumed by a pinned assignment. A real (hard)
    # vehicle is NOT walled off — freeing its order (above) frees it here too, so
    # it can be reassigned at break_cost['hard'].
    consumed = set(pinned.values())
    free_units = sorted(uid for uid in units if uid not in consumed)  # §4 fixed key

    return RepairPlan(
        pinned=pinned,
        free_orders=free_orders,
        free_units=free_units,
        priority=_combined_priority(snapshot, override),
        churn_default=override.get("churn_price"),
    )


# --- Solve one churn price ----------------------------------------------------


@dataclass
class SolveResult:
    churn_price: int
    plan: dict[str, str]  # order_key -> vehicle_id (full: pinned+free)
    unfilled: list[str]  # free orders that ended with no car
    n_changes: int  # free orders whose vehicle differs from incumbent
    weighted_late_days: float  # Σ W(o)·tardiness over ALL orders
    objective_micro: int  # solver objective in scaled int space
    self_check: dict


def _solve_one(snapshot: Snapshot, rp: RepairPlan, churn_price: int) -> SolveResult:
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    scale = CFG["cost_scale"]

    # §4 integer node positions from the fixed sort already applied to rp.
    # 0=S, 1=T, 2=D(no car); orders then units follow.
    S, T, DUMMY = 0, 1, 2
    order_pos = {oid: 3 + i for i, oid in enumerate(rp.free_orders)}
    unit_pos = {uid: 3 + len(order_pos) + i for i, uid in enumerate(rp.free_units)}

    smcf = min_cost_flow.SimpleMinCostFlow()
    N = len(rp.free_orders)

    # S -> each free order (cap 1, cost 0)
    for oid in rp.free_orders:
        smcf.add_arc_with_capacity_and_unit_cost(S, order_pos[oid], 1, 0)

    # order -> compatible free vehicle (cap 1, §2 cost) ; order -> the no-car dummy.
    # Each assignment arc is tagged with the (order, vehicle-or-nothing) it means
    # as it is added, so reading the flow back needs no reverse node lookup (§4).
    choice_of: dict[int, tuple[str, str | None]] = {}
    for oid in rp.free_orders:
        o = orders[oid]
        # The price of CHANGING this order's car, charged once on every arc that
        # does: `churn_price` is the planner's "change as little as possible"
        # dial, `brk` the price of disturbing a kept promise. Both are about the
        # move, not the pairing — which is the fix made 2026-08-26: churn used to
        # be charged whenever the car's date differed from the PROMISED date,
        # true of 98.9% of eligible pairings, so it was a near-constant added to
        # almost every option and could not steer a choice.
        inc_uid = snapshot.incumbent.get(oid)
        move_cost = churn_price + break_cost_of(o, units.get(inc_uid) if inc_uid else None)
        for uid in rp.free_units:
            u = units[uid]
            if not eligible(o, u):
                continue  # incompatible -> no arc (keeps graph sparse, §4)
            c = arc_cost_float(o, u, rp.priority)
            if uid != inc_uid:
                c += move_cost
            arc = smcf.add_arc_with_capacity_and_unit_cost(
                order_pos[oid], unit_pos[uid], 1, round(c * scale)
            )
            choice_of[arc] = (oid, uid)
        # Ending with no car. An order that HAD a car and loses it also changes,
        # so it pays the move; one that never had a car changes nothing by
        # staying without one.
        c = CFG["no_car_cost"] + (move_cost if inc_uid else 0.0)
        arc = smcf.add_arc_with_capacity_and_unit_cost(order_pos[oid], DUMMY, 1, round(c * scale))
        choice_of[arc] = (oid, None)

    # free vehicle -> T (cap 1) ; no-car dummy -> T (cap N)
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
        effective_weight(o, rp.priority) * tardiness(o, units[plan[oid]])
        for oid, o in orders.items()
        if oid in plan
    )
    return SolveResult(
        churn_price=churn_price,
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
    # Every order has exactly one vehicle (or is a surfaced no-car order).
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


# --- The churn-price sweep (§2, highest-value output) -------------------------


@dataclass
class SweepPoint:
    churn_price: int
    n_changes: int
    weighted_late_days: float
    unfilled: int


def solve(
    snapshot: Snapshot,
    override: dict | None = None,
    churn_price: int | None = None,
) -> SolveResult:
    """One deterministic solve at one churn price (default: the override's, else
    the first sweep value)."""
    override = override or {}
    rp = partition(snapshot, override)
    if churn_price is None:
        churn_price = rp.churn_default if rp.churn_default is not None else CHURN_PRICE_SWEEP[0]
    return _solve_one(snapshot, rp, int(churn_price))


def churn_sweep(
    snapshot: Snapshot,
    override: dict | None = None,
    prices=CHURN_PRICE_SWEEP,
) -> tuple[list[SweepPoint], dict[int, SolveResult]]:
    """Re-solve across churn prices (same network, only some arc costs change) ->
    Pareto frontier of (changes vs weighted late-days). Returns the frontier
    points and the full per-price results."""
    override = override or {}
    rp = partition(snapshot, override)
    points: list[SweepPoint] = []
    results: dict[int, SolveResult] = {}
    for price in prices:
        res = _solve_one(snapshot, rp, int(price))
        results[int(price)] = res
        points.append(
            SweepPoint(int(price), res.n_changes, res.weighted_late_days, len(res.unfilled))
        )
    return points, results

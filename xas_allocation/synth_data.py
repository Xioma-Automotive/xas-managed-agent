"""Synthetic data generator — stands in for the (not-yet-existing) XAS pull.

DECIDE-7: the real XAS API data contract does not exist yet. The field shapes
below ARE the prototype's proposed contract:

  order : order_id, customer, priority, promised_week, spec, n_prior_delays,
          days_backordered
  unit  : unit_id, arrival_week, spec, state, shipment

Everything is derived from a single integer ``seed`` via ``random.Random`` — no
module-level randomness, no wall-clock — so a given seed regenerates a
byte-identical snapshot on every replay (the core determinism requirement).

Weeks are integer ISO week numbers within one year (the 13-week horizon), with
``week_label`` for display as ``2026-Wxx``. Vehicle specs are deliberately
*half-populated* (some fields ``None``) so the spec-match residual (§8.3) has
something real to resolve.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict
from typing import Optional

YEAR = 2026
HORIZON_START_WEEK = 32       # first week of the 13-week horizon
HORIZON_WEEKS = 13

# Customers: dealer name -> (customer_id, priority letter). Priority maps to a
# weight multiplier in spec_match.PRIORITY_WEIGHT. "Colmobil" is included so the
# spec's NL-resolution example ("prefer Colmobil") has a real target.
CUSTOMERS: dict[str, tuple[str, str]] = {
    "Colmobil":     ("CUST-001", "A"),
    "Delek Motors": ("CUST-002", "A"),
    "Champion":     ("CUST-003", "B"),
    "Talcar":       ("CUST-004", "B"),
    "Carasso":      ("CUST-005", "C"),
    "Lubinski":     ("CUST-006", "C"),
}

MODELS = ("SUV", "Sedan", "Hatch", "Pickup")
DRIVETRAINS = ("FWD", "AWD")
TRIMS = ("Base", "Sport", "Lux")
COLORS = ("White", "Black", "Blue", "Silver", "Red")

# Unit lifecycle states. COMMIT_POINT states (shipped / in_prep, see DECIDE-3)
# are physically committed and get hard-pinned.
UNIT_STATES = ("planned", "in_prep", "shipped")


def week_label(week: int) -> str:
    """Integer ISO week -> display label, e.g. 34 -> '2026-W34'."""
    return f"{YEAR}-W{week:02d}"


def parse_week_label(label: str) -> int:
    """'2026-W36' -> 36. Tolerates a bare int-like string too."""
    label = label.strip()
    if "-W" in label:
        return int(label.split("-W")[1])
    return int(label)


@dataclass(frozen=True)
class Order:
    order_id: int
    customer: str          # dealer name (see CUSTOMERS)
    customer_id: str
    priority: str          # "A" | "B" | "C"
    promised_week: int
    spec: dict             # may contain None values (half-populated)
    n_prior_delays: int
    days_backordered: int


@dataclass(frozen=True)
class Unit:
    unit_id: int
    arrival_week: int
    spec: dict
    state: str             # see UNIT_STATES
    shipment: str          # batch id; a disruption delays a whole shipment


@dataclass
class Snapshot:
    """Everything a single solve consumes, per §8.2."""
    orders: list[Order]
    units: list[Unit]
    incumbent: dict[int, int]        # order_id -> unit_id (pre-disruption plan)
    disruption: dict                 # the delayed shipment + who it hit
    seed: int

    def order_by_id(self) -> dict[int, Order]:
        return {o.order_id: o for o in self.orders}

    def unit_by_id(self) -> dict[int, Unit]:
        return {u.unit_id: u for u in self.units}

    def as_dict(self) -> dict:
        return {
            "orders": [asdict(o) for o in self.orders],
            "units": [asdict(u) for u in self.units],
            "incumbent": self.incumbent,
            "disruption": self.disruption,
            "seed": self.seed,
        }


def _maybe(rng: random.Random, value, blank_prob: float) -> Optional[object]:
    """Return value, or None with probability blank_prob (half-populated spec)."""
    return None if rng.random() < blank_prob else value


def _make_spec(rng: random.Random, blank_prob: float) -> dict:
    return {
        "model": rng.choice(MODELS),                       # always populated (bin key)
        "drivetrain": _maybe(rng, rng.choice(DRIVETRAINS), blank_prob),
        "trim": _maybe(rng, rng.choice(TRIMS), blank_prob),
        "color": _maybe(rng, rng.choice(COLORS), blank_prob),
    }


def _compatible_unit_spec(rng: random.Random, order_spec: dict, blank_prob: float) -> dict:
    """Build a unit spec guaranteed compatible with order_spec: copy every field
    the order specifies (so no mismatch), and for fields the order left blank,
    fill a concrete value or leave blank. Keeps the incumbent feasible under the
    SAME resolve_compatibility the solver uses — no optimistic-vs-conservative
    disagreement."""
    spec: dict = {}
    for f, pool in (
        ("model", MODELS),
        ("drivetrain", DRIVETRAINS),
        ("trim", TRIMS),
        ("color", COLORS),
    ):
        if order_spec.get(f) is not None:
            spec[f] = order_spec[f]
        else:
            spec[f] = _maybe(rng, rng.choice(pool), blank_prob)
    return spec


def _state_by_arrival(rng: random.Random, arrival: int) -> str:
    """Near-term units are likelier already committed (in_prep / shipped)."""
    weeks_out = arrival - HORIZON_START_WEEK
    if weeks_out <= 1:
        return rng.choices(UNIT_STATES, weights=[20, 40, 40])[0]
    if weeks_out <= 3:
        return rng.choices(UNIT_STATES, weights=[55, 35, 10])[0]
    return "planned"


def generate_snapshot(
    seed: int = 42,
    n_orders: int = 120,
    spare_ratio: float = 0.6,
    delay_weeks: int = 3,
) -> Snapshot:
    """Generate a reproducible snapshot with a complete incumbent + a disruption.

    A real XAS pull carries a *complete current allocation*, so we build the
    incumbent by construction (one compatible unit per order) rather than a lossy
    greedy pass — that keeps the free set equal to exactly the disrupted orders
    (§1). ``spare_ratio`` adds unassigned inbound units so a repair has somewhere
    better to go. The disruption delays one whole shipment by ``delay_weeks``,
    unpinning the orders whose incumbent unit rode it.
    """
    rng = random.Random(seed)
    weeks = list(range(HORIZON_START_WEEK, HORIZON_START_WEEK + HORIZON_WEEKS))
    last_week = HORIZON_START_WEEK + HORIZON_WEEKS - 1
    names = list(CUSTOMERS.keys())

    # --- Orders -------------------------------------------------------------
    orders: list[Order] = []
    for i in range(n_orders):
        name = rng.choice(names)
        cid, prio = CUSTOMERS[name]
        promised = rng.choice(weeks[: HORIZON_WEEKS - 2])  # leave slack for lateness
        n_prior = rng.choices([0, 1, 2, 3], weights=[70, 18, 8, 4])[0]
        backorder = rng.choices([0, 0, 0, 7, 14, 30], weights=[50, 15, 10, 12, 8, 5])[0]
        orders.append(
            Order(
                order_id=4000 + i,
                customer=name,
                customer_id=cid,
                priority=prio,
                promised_week=promised,
                spec=_make_spec(rng, blank_prob=0.25),
                n_prior_delays=n_prior,
                days_backordered=backorder,
            )
        )

    # --- Incumbent units: one compatible unit per order, grouped into
    #     shipments of ~8. Arrival on-time or a week late; state by proximity.
    per_shipment = 8
    units: list[Unit] = []
    incumbent: dict[int, int] = {}
    uid = 9000
    for idx, o in enumerate(sorted(orders, key=lambda o: o.order_id)):
        arrival = min(o.promised_week + rng.choice([0, 0, 1]), last_week)
        shipment = f"SHP-{idx // per_shipment:03d}"
        units.append(
            Unit(
                unit_id=uid,
                arrival_week=arrival,
                spec=_compatible_unit_spec(rng, o.spec, blank_prob=0.20),
                state=_state_by_arrival(rng, arrival),
                shipment=shipment,
            )
        )
        incumbent[o.order_id] = uid
        uid += 1

    # --- Spare units (unassigned inbound) so a repair can improve on the
    #     delayed incumbent unit. Grouped into their own shipments.
    n_spares = int(n_orders * spare_ratio)
    for s in range(n_spares):
        arrival = rng.choice(weeks)
        units.append(
            Unit(
                unit_id=uid,
                arrival_week=arrival,
                spec=_make_spec(rng, blank_prob=0.20),
                state=_state_by_arrival(rng, arrival),
                shipment=f"SPARE-{s // per_shipment:03d}",
            )
        )
        uid += 1

    # --- Disruption: delay one shipment that actually carries incumbent units.
    unit_by_id = {u.unit_id: u for u in units}
    assigned_shipments = {unit_by_id[uid].shipment for uid in incumbent.values()}
    # Pick deterministically: the lowest-id shipment that is (a) carrying
    # incumbent units and (b) not already fully committed/shipped, so a repair
    # is meaningful.
    candidate = None
    for shp in sorted(assigned_shipments):
        shp_units = [u for u in units if u.shipment == shp]
        if any(u.state == "planned" for u in shp_units):
            candidate = shp
            break
    if candidate is None:
        candidate = sorted(assigned_shipments)[0]

    delayed_unit_ids = sorted(u.unit_id for u in units if u.shipment == candidate)
    # Apply the delay: push arrival later (units are frozen dataclasses -> rebuild).
    delayed_set = set(delayed_unit_ids)
    new_units: list[Unit] = []
    for u in units:
        if u.unit_id in delayed_set:
            new_units.append(
                Unit(u.unit_id, u.arrival_week + delay_weeks, u.spec, u.state, u.shipment)
            )
        else:
            new_units.append(u)
    units = new_units

    incumbent_by_unit = {uid_: oid for oid, uid_ in incumbent.items()}
    disrupted_orders = sorted(
        incumbent_by_unit[uid_] for uid_ in delayed_unit_ids if uid_ in incumbent_by_unit
    )

    disruption = {
        "shipment": candidate,
        "delay_weeks": delay_weeks,
        "delayed_units": delayed_unit_ids,
        "disrupted_orders": disrupted_orders,
    }

    return Snapshot(
        orders=orders,
        units=units,
        incumbent=incumbent,
        disruption=disruption,
        seed=seed,
    )


if __name__ == "__main__":
    snap = generate_snapshot()
    d = snap.disruption
    print(f"seed={snap.seed}  orders={len(snap.orders)}  units={len(snap.units)}")
    print(f"incumbent assignments: {len(snap.incumbent)}")
    print(
        f"disruption: shipment {d['shipment']} delayed {d['delay_weeks']}w -> "
        f"{len(d['delayed_units'])} units, {len(d['disrupted_orders'])} orders to repair"
    )

"""The allocation snapshot the solver reads — date-based, real-XAS-shaped.

This is the *frozen* half of the core invariant:

    plan = pure_function(data_snapshot, skill, override)

The export's two row streams (`orders.csv` + `vehicles.csv`, carved out of the
real XAS export by `scenario_engine/real_*.py`) are translated host-side by
`datasource.translate` and flattened into the three arrays here by `flatten.py`,
in the sandbox. This module owns only the flattened shape the solver consumes and
its JSON (de)serialization.

Grain: the allocatable **order** is one **wanted car**, and it is one row of the
export's ``orders.csv`` — so the key is the row's own ``OrderId`` (e.g.
``502377``), one level. The job-card/car-line grain went with the app MCP on
2026-08-27, and the "a line asking for 3 cars is planned as 1" question went with
it: this export has no lines and no ``Quantity``.

Supply is ONE ``vehicles`` list; each vehicle is capacity-1 with a
``sales_model`` and an ``eta_dealer`` date. There is no hard/soft binding any
more (2026-08-27): in this export a car's status IS its allocation state, so what
matters about a car is whether an order holds it (``allocations``) and when it
lands (``eta_dealer``). Breaking a kept promise costs the same whatever kind of
car it is — one ``break_cost`` in the config, not two.

Everything is keyed on **real dates** (`YYYY-MM-DD`); tardiness is in **days**.
`now` is the pull date, carried on the snapshot as the provenance of the picture —
nothing in the cost model reads it any more, since the time fence that did was
removed on 2026-08-26.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date


def parse_date(value: str | date) -> date:
    """'2026-08-24' -> date(2026, 8, 24). Idempotent on a date."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(value.strip())


def date_label(d: date) -> str:
    """date -> ISO 'YYYY-MM-DD' for display and serialization."""
    return d.isoformat()


@dataclass(frozen=True)
class Order:
    """One order row — one wanted car, the demand side of the match.

    Three fields, and that is the whole of the demand side. What is NOT here:

    * **no priority.** The record's letter was never a planner's decision and the
      export has no such column, so priority is a per-turn LEVER on the override
      (``solver._combined_priority``).
    * **no customer.** ``orders.csv`` has no customer column, and with priority a
      per-order lever there was nothing left for it to do (2026-08-27). Steering
      names order ids.
    * **no delay history and no price.** The three escalation fields were read
      only by weight terms deleted on 2026-08-26 and were zero on every real row;
      ``price`` was display-only and the export does not carry it.
    """

    order_id: str  # the export's OrderId, e.g. "502377" — the whole key
    sales_model: str  # the eligibility key (SalesModel), matched by equality
    delivery_date: date  # etaDealer on the ORDER — the promise. NOT the car's date.

    @property
    def key(self) -> str:
        """The unique order key. One row, one order, so the id IS the key —
        ``key`` stays as the name every caller reads it by."""
        return self.order_id

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "sales_model": self.sales_model,
            "delivery_date": date_label(self.delivery_date),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Order:
        return cls(
            order_id=str(d["order_id"]),
            sales_model=d["sales_model"],
            delivery_date=parse_date(d["delivery_date"]),
        )


@dataclass(frozen=True)
class Vehicle:
    """One supply item — a car in the pool, free or currently held by an order.

    Capacity 1, a ``sales_model`` and an ``eta_dealer`` date, and nothing else.
    Whether an order holds it lives in ``Snapshot.allocations``, not on the car,
    and there is no hard/soft flavour: the export's own status is its allocation
    state, and breaking a kept promise costs the same whatever stage the car is at
    (2026-08-27, retiring DECIDE-3's mechanism).
    """

    vehicle_id: str  # VehicleCode — the supply id allocations and the plan key on
    sales_model: str  # SalesModel — the trim/colour eligibility key
    eta_dealer: date  # availableBy — the ONE mutable field a delay writes

    def to_dict(self) -> dict:
        return {
            "vehicle_id": self.vehicle_id,
            "sales_model": self.sales_model,
            "eta_dealer": date_label(self.eta_dealer),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Vehicle:
        return cls(
            vehicle_id=str(d["vehicle_id"]),
            sales_model=d["sales_model"],
            eta_dealer=parse_date(d["eta_dealer"]),
        )


@dataclass
class Snapshot:
    """Everything one solve consumes — the flattened, frozen pull."""

    orders: list[Order]  # one per wanted CAR — one row of orders.csv
    vehicles: list[Vehicle]  # the car pool: free ∪ currently allocated
    allocations: dict[str, str]  # order_key -> vehicle_id (current allocation)
    disruption: dict  # the delayed vehicles + who they touched
    now: date  # the pull date this picture was frozen at (provenance, not a cost input)
    # The pull's own provenance, carried through so the sandbox can report it:
    # `meta["excluded"]` is what the source filtered out and why, which the turn-1
    # reply MUST say — a plan over 1 of 25 orders that doesn't mention the other 24
    # reads as the whole book. Empty for a pull that filtered nothing.
    meta: dict = field(default_factory=dict)

    def order_by_key(self) -> dict[str, Order]:
        """Orders by key — and the guard that a key really is unique.

        The whole solver reads demand through this dict, so a duplicated key does
        not raise: it silently collapses two orders into one, and `orders` and this
        mapping then disagree about how much demand exists. `datasource.translate`
        raises on a duplicate OrderId in the export for the same reason; this is the
        backstop for a snapshot assembled any other way."""
        by_key = {o.key: o for o in self.orders}
        if len(by_key) != len(self.orders):
            counts = Counter(o.key for o in self.orders)
            dupes = sorted(k for k, n in counts.items() if n > 1)
            raise ValueError(f"duplicate order keys — demand would be lost: {dupes}")
        return by_key

    def vehicle_by_id(self) -> dict[str, Vehicle]:
        return {u.vehicle_id: u for u in self.vehicles}

    def as_dict(self) -> dict:
        return {
            "orders": [o.to_dict() for o in self.orders],
            "vehicles": [u.to_dict() for u in self.vehicles],
            "allocations": self.allocations,
            "disruption": self.disruption,
            "now": date_label(self.now),
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot:
        return cls(
            orders=[Order.from_dict(o) for o in d["orders"]],
            vehicles=[Vehicle.from_dict(u) for u in d["vehicles"]],
            allocations={str(k): str(v) for k, v in d["allocations"].items()},
            disruption=d.get("disruption", {}),
            now=parse_date(d["now"]),
            meta=d.get("meta", {}),
        )

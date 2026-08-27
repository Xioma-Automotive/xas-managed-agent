"""The allocation snapshot the solver reads — date-based, real-XAS-shaped.

This is the *frozen* half of the core invariant:

    plan = pure_function(data_snapshot, skill, override)

The rich relational world (VSO jobcards with car lines, the vehicle pool of real
and future vehicles, allocation links) is fabricated by the standalone
`scenario_engine/` and flattened into the three arrays here by `flatten.py`. This
module owns only the flattened shape the solver consumes and its JSON
(de)serialization.

Grain: the allocatable **order** is one **wanted car**. A VSO (one customer's
sales order) groups several car lines, and a line's ``Quantity`` says how many
car — so the key has two levels, ``{so_id}-{line}`` (VSO ``JobKey`` + jobitem
``LineNum``, e.g. ``VSO-4000-2``).

ONE CAR PER LINE is an assumption, not a fact the data guarantees: a jobitem
carries a ``Quantity`` that can read 3, and this snapshot reads it as 1. It is
deliberate (2026-08-25) — a line resolves to at most one vehicle, so the cars
beyond the first could never be linked to anything — and it is pending a
response-shape decision: one allocation cap per line, or per-car fields. Until
that lands, ``Quantity`` is not read at all.
Supply is ONE ``vehicles`` list; each vehicle is capacity-1 with a
``sales_model`` and an ``eta_dealer`` date, and a ``vehicle_classification`` of
``"Vehicle"`` (a real car, a hard binding) or ``"Future"`` (a not-yet-built car,
a soft binding). The solver matches orders ↔ vehicles and only cares about the
classification to price breaking a binding (DECIDE-3).

Everything is keyed on **real dates** (`YYYY-MM-DD`); tardiness is in **days**.
`now` is the pull date, carried on the snapshot as the provenance of the picture —
nothing in the cost model reads it any more, since the time fence that did was
removed on 2026-08-26.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

# The two supply flavors, from a vehicle's ``VehicleClassification`` (DECIDE-3).
HARD_CLASSIFICATION = "Vehicle"  # a real car (VIN) — a HARD binding
SOFT_CLASSIFICATION = "Future"  # a not-yet-built car — a SOFT binding


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
    """One VSO jobitem — one wanted car, the demand side of the match.

    No priority, and no delay history. The record's priority letter was never a
    planner's decision and the real export has no such column, so priority is a
    per-turn LEVER on the override instead (``solver._combined_priority``). The
    three escalation fields that used to ride here (prior delays, days
    back-ordered, times rescheduled) were read only by weight terms deleted on
    2026-08-26; every one of them was zero on every row of real data.
    """

    so_id: str  # VSO JobKey / DMSJCEntry, e.g. "VSO-4000"
    line: int  # jobitem LineNum; (so_id, line) is the car line
    customer: str  # dealer display name (Accounts.Owner.AccountName)
    customer_id: str  # stable id the override object carries (Accounts.Owner.AccountUUID)
    sales_model: str  # the hard eligibility key (jobitem SalesModelCode, model-level)
    delivery_date: date  # DeliveryDate — the promise; tardiness is measured against it
    price: float  # display-only (Σ Prices[].GrossTotal; not a cost-model input, for now)

    @property
    def key(self) -> str:
        """The unique order key: one car line, e.g. 'VSO-4000-2'."""
        return f"{self.so_id}-{self.line}"

    def to_dict(self) -> dict:
        return {
            "so_id": self.so_id,
            "line": self.line,
            "customer": self.customer,
            "customer_id": self.customer_id,
            "sales_model": self.sales_model,
            "delivery_date": date_label(self.delivery_date),
            "price": self.price,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Order:
        return cls(
            so_id=str(d["so_id"]),
            line=int(d["line"]),
            customer=d["customer"],
            customer_id=d["customer_id"],
            sales_model=d["sales_model"],
            delivery_date=parse_date(d["delivery_date"]),
            price=float(d.get("price", 0.0)),
        )


@dataclass(frozen=True)
class Vehicle:
    """One supply item — a vehicle in the pool, real or future.

    ``vehicle_classification == "Vehicle"`` is a concrete car (a VIN) and a
    **hard** binding; ``"Future"`` is a not-yet-built car and a **soft** binding.
    Both are capacity-1 supply with a ``sales_model`` and an ``eta_dealer`` date —
    the only difference is what it costs to move an allocation OFF each (DECIDE-3):
    hard is expensive-but-movable, soft is free to reshuffle. ``is_hard`` is the
    single bit that drives that, derived from the classification.
    """

    vehicle_id: str  # VehicleCode — the supply id allocations and the plan key on
    vehicle_classification: str  # "Vehicle" (real, hard) | "Future" (future, soft)
    sales_model: str  # SalesModel — the trim/colour eligibility key
    eta_dealer: date  # EtaDealer — the ONE mutable field disruptions write

    @property
    def is_hard(self) -> bool:
        """A real vehicle is a HARD binding (expensive to break); a future vehicle
        is SOFT (free to reshuffle). See DECIDE-3."""
        return self.vehicle_classification == HARD_CLASSIFICATION

    def to_dict(self) -> dict:
        return {
            "vehicle_id": self.vehicle_id,
            "vehicle_classification": self.vehicle_classification,
            "sales_model": self.sales_model,
            "eta_dealer": date_label(self.eta_dealer),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Vehicle:
        return cls(
            vehicle_id=str(d["vehicle_id"]),
            vehicle_classification=d["vehicle_classification"],
            sales_model=d["sales_model"],
            eta_dealer=parse_date(d["eta_dealer"]),
        )


@dataclass
class Snapshot:
    """Everything one solve consumes — the flattened, frozen pull."""

    orders: list[Order]  # one per wanted CAR (a qty-3 line contributes 3)
    vehicles: list[Vehicle]  # the vehicle pool: real ∪ future
    allocations: dict[str, str]  # order_key -> vehicle_id (current allocation)
    disruption: dict  # the delayed vehicles + who they touched
    now: date  # the pull date this picture was frozen at (provenance, not a cost input)
    # The pull's own provenance, carried through so the sandbox can report it:
    # `meta["excluded"]` is what the source filtered out and why, which the turn-1
    # reply MUST say — a plan over 1 of 25 sales orders that doesn't mention the
    # other 24 reads as the whole book. Empty for a pull that filtered nothing.
    meta: dict = field(default_factory=dict)

    def order_by_key(self) -> dict[str, Order]:
        """Orders by key — and the guard that a key really is unique.

        The whole solver reads demand through this dict, so a duplicated key does
        not raise: it silently collapses two cars into one, and `orders` and this
        mapping then disagree about how much demand exists. That is exactly the
        failure a botched qty expansion produces, and it would show up only as a
        plan that quietly under-serves a customer."""
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

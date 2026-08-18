"""The allocation snapshot the solver reads — date-based, real-XAS-shaped.

This is the *frozen* half of the core invariant:

    plan = pure_function(data_snapshot, skill, override)

The rich relational world (VSO jobcards with car lines, the vehicle pool of real
and future vehicles, allocation links) is fabricated by the standalone
`scenario_engine/` and flattened into the three arrays here by `flatten.py`. This
module owns only the flattened shape the solver consumes and its JSON
(de)serialization.

Grain: the allocatable **order** is one **VSO jobitem** — one wanted car. A VSO
(one customer's sales order) groups several car lines; the order key is
``{so_id}-{line}`` (VSO ``JobKey`` + jobitem ``LineNum``, e.g. ``VSO-4000-2``).
Supply is ONE ``vehicles`` list; each vehicle is capacity-1 with a
``sales_model`` and an ``eta_dealer`` date, and a ``vehicle_classification`` of
``"Vehicle"`` (a real car, a hard binding) or ``"Future"`` (a not-yet-built car,
a soft binding). The solver matches orders ↔ vehicles and only cares about the
classification to price breaking a binding (DECIDE-3).

Everything is keyed on **real dates** (`YYYY-MM-DD`); tardiness is in **days**.
`now` is the pull date, carried on the snapshot so the fence is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

DATE_FMT = "%Y-%m-%d"

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


def days_late(eta: date, delivery_date: date) -> int:
    """Tardiness in whole days, floored at 0 (early is not negative-late)."""
    return max(0, (eta - delivery_date).days)


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


@dataclass(frozen=True)
class Order:
    """One VSO jobitem — one wanted car, the demand side of the match."""

    so_id: str  # VSO JobKey / DMSJCEntry, e.g. "VSO-4000"
    line: int  # jobitem LineNum; (so_id, line) is the unique car line
    customer: str  # dealer display name (Accounts.Owner.AccountName)
    customer_id: str  # stable id the override object carries (Accounts.Owner.AccountUUID)
    sales_model: str  # the hard eligibility key (jobitem SalesModelCode, model-level)
    priority: str  # "A" | "B" | "C" (JobPriority.Code)
    delivery_date: date  # DeliveryDate — the promise; tardiness is measured against it
    price: float  # display-only (Σ Prices[].GrossTotal; not a cost-model input, for now)
    n_prior_delays: int  # supply-chain delays before us (escalates weight, §2)
    days_backordered: int
    times_rescheduled: int = 0  # reschedules OUR repair loop caused — fairness (DECIDE-11)

    @property
    def key(self) -> str:
        """The unique order key: VSO JobKey + jobitem LineNum, e.g. 'VSO-4000-2'."""
        return f"{self.so_id}-{self.line}"

    def to_dict(self) -> dict:
        return {
            "so_id": self.so_id,
            "line": self.line,
            "customer": self.customer,
            "customer_id": self.customer_id,
            "sales_model": self.sales_model,
            "priority": self.priority,
            "delivery_date": date_label(self.delivery_date),
            "price": self.price,
            "n_prior_delays": self.n_prior_delays,
            "days_backordered": self.days_backordered,
            "times_rescheduled": self.times_rescheduled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Order:
        return cls(
            so_id=str(d["so_id"]),
            line=int(d["line"]),
            customer=d["customer"],
            customer_id=d["customer_id"],
            sales_model=d["sales_model"],
            priority=d["priority"],
            delivery_date=parse_date(d["delivery_date"]),
            price=float(d.get("price", 0.0)),
            n_prior_delays=int(d.get("n_prior_delays", 0)),
            days_backordered=int(d.get("days_backordered", 0)),
            times_rescheduled=int(d.get("times_rescheduled", 0)),
        )


@dataclass(frozen=True)
class Unit:
    """One supply item — a vehicle in the pool, real or future.

    ``vehicle_classification == "Vehicle"`` is a concrete car (a VIN) and a
    **hard** binding; ``"Future"`` is a not-yet-built car and a **soft** binding.
    Both are capacity-1 supply with a ``sales_model`` and an ``eta_dealer`` date —
    the only difference is what it costs to move an allocation OFF each (DECIDE-3):
    hard is expensive-but-movable, soft is free to reshuffle. ``is_hard`` is the
    single bit that drives that, derived from the classification.
    """

    vehicle_id: str  # VehicleCode — the supply id the incumbent/plan keys on
    vehicle_classification: str  # "Vehicle" (real, hard) | "Future" (future, soft)
    sales_model: str  # ModelId.Code (model-level eligibility key)
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
    def from_dict(cls, d: dict) -> Unit:
        return cls(
            vehicle_id=str(d["vehicle_id"]),
            vehicle_classification=d["vehicle_classification"],
            sales_model=d["sales_model"],
            eta_dealer=parse_date(d["eta_dealer"]),
        )


@dataclass
class Snapshot:
    """Everything one solve consumes — the flattened, frozen pull."""

    orders: list[Order]  # VSO car lines
    units: list[Unit]  # the vehicle pool: real ∪ future
    incumbent: dict[str, str]  # order_key -> vehicle_id (current allocation)
    disruption: dict  # the delayed vehicles + who they touched
    now: date  # the pull date; the time fence reads this

    def order_by_key(self) -> dict[str, Order]:
        return {o.key: o for o in self.orders}

    def unit_by_id(self) -> dict[str, Unit]:
        return {u.vehicle_id: u for u in self.units}

    def as_dict(self) -> dict:
        return {
            "orders": [o.to_dict() for o in self.orders],
            "units": [u.to_dict() for u in self.units],
            "incumbent": self.incumbent,
            "disruption": self.disruption,
            "now": date_label(self.now),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot:
        return cls(
            orders=[Order.from_dict(o) for o in d["orders"]],
            units=[Unit.from_dict(u) for u in d["units"]],
            incumbent={str(k): str(v) for k, v in d["incumbent"].items()},
            disruption=d.get("disruption", {}),
            now=parse_date(d["now"]),
        )

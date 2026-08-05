"""The allocation snapshot the solver reads — date-based, XAS-shaped.

This is the *frozen* half of the core invariant:

    plan = pure_function(data_snapshot, skill, ledger)

The rich relational world (PDN → Vehicle, Customer → SO, allocation links) is
fabricated by the standalone `scenario_engine/` and flattened into the three
arrays here by `flatten.py`. This module owns only the flattened shape the
solver consumes and its JSON (de)serialization — no generation, no I/O beyond
parsing what `flatten` hands it.

Everything is keyed on **real dates** (`YYYY-MM-DD`), not ISO weeks; tardiness
is measured in **days**. `now` is the pull date, carried on the snapshot so the
time fence is a pure function of the data and never reads a wall clock (a
wall-clock read would break replay).

Vocabulary vs. the old week-based model:
  Unit.arrival_week      → Unit.planned_delivery_date   (the mutable field)
  Unit.state             → Unit.location_state          (pipeline stage)
  Unit.shipment          → Unit.pdn                     (supply provenance)
  Order.promised_week    → Order.promised_date          (commitment; tardiness vs it)
  Order.spec{...}        → Order.sales_model            (eligibility is equality)
  (new)                  → Order.eta_date               (originally-expected delivery)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

DATE_FMT = "%Y-%m-%d"


def parse_date(value: str | date) -> date:
    """'2026-08-24' -> date(2026, 8, 24). Idempotent on a date."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(value.strip())


def date_label(d: date) -> str:
    """date -> ISO 'YYYY-MM-DD' for display and serialization."""
    return d.isoformat()


def days_late(planned_delivery: date, promised: date) -> int:
    """Tardiness in whole days, floored at 0 (early is not negative-late)."""
    return max(0, (planned_delivery - promised).days)


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


@dataclass(frozen=True)
class Order:
    """One SO line — the demand side, the 'order' in the bipartite match."""

    order_id: str
    customer: str  # dealer display name
    customer_id: str  # stable id the override object carries
    sales_model: str  # the hard eligibility key
    priority: str  # "A" | "B" | "C"
    promised_date: date  # customer commitment; tardiness is measured against it
    eta_date: date  # originally-expected delivery, frozen at allocation
    price: float  # display-only (not a cost-model input, for now)
    n_prior_delays: int  # supply-chain delays before us (escalates weight, §2)
    days_backordered: int
    times_rescheduled: int = 0  # reschedules OUR repair loop caused — fairness (DECIDE-11)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "customer": self.customer,
            "customer_id": self.customer_id,
            "sales_model": self.sales_model,
            "priority": self.priority,
            "promised_date": date_label(self.promised_date),
            "eta_date": date_label(self.eta_date),
            "price": self.price,
            "n_prior_delays": self.n_prior_delays,
            "days_backordered": self.days_backordered,
            "times_rescheduled": self.times_rescheduled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Order:
        return cls(
            order_id=str(d["order_id"]),
            customer=d["customer"],
            customer_id=d["customer_id"],
            sales_model=d["sales_model"],
            priority=d["priority"],
            promised_date=parse_date(d["promised_date"]),
            eta_date=parse_date(d["eta_date"]),
            price=float(d.get("price", 0.0)),
            n_prior_delays=int(d.get("n_prior_delays", 0)),
            days_backordered=int(d.get("days_backordered", 0)),
            times_rescheduled=int(d.get("times_rescheduled", 0)),
        )


@dataclass(frozen=True)
class Unit:
    """One pool Vehicle — the supply side, the 'unit' in the bipartite match."""

    vehicle_id: str
    sales_model: str
    planned_delivery_date: date  # the ONE mutable field disruptions write
    location_state: str  # future|sea|port|transfer|bonded|pdi (DECIDE-3)
    pdn: str  # supply provenance; a PDN delay hits a whole batch
    committed: bool  # derived from location_state at flatten time

    def to_dict(self) -> dict:
        return {
            "vehicle_id": self.vehicle_id,
            "sales_model": self.sales_model,
            "planned_delivery_date": date_label(self.planned_delivery_date),
            "location_state": self.location_state,
            "pdn": self.pdn,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Unit:
        return cls(
            vehicle_id=str(d["vehicle_id"]),
            sales_model=d["sales_model"],
            planned_delivery_date=parse_date(d["planned_delivery_date"]),
            location_state=d["location_state"],
            pdn=d.get("pdn", ""),
            committed=bool(d["committed"]),
        )


@dataclass
class Snapshot:
    """Everything one solve consumes — the flattened, frozen pull."""

    orders: list[Order]
    units: list[Unit]
    incumbent: dict[str, str]  # order_id -> vehicle_id (current allocation)
    disruption: dict  # the delayed PDN + who it touched
    now: date  # the pull date; the time fence reads this

    def order_by_id(self) -> dict[str, Order]:
        return {o.order_id: o for o in self.orders}

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

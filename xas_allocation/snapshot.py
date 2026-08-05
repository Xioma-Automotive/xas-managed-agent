"""The allocation snapshot the solver reads — date-based, XAS-shaped.

This is the *frozen* half of the core invariant:

    plan = pure_function(data_snapshot, skill, ledger)

The rich relational world (PO → PDN → Vehicle, Customer → SO → vehicle order
rows, allocation links) is fabricated by the standalone `scenario_engine/` and
flattened into the three arrays here by `flatten.py`. This module owns only the
flattened shape the solver consumes and its JSON (de)serialization.

Grain (v2): the allocatable **order** is a **vehicle order row** — one car of
demand. A Sales Order groups several rows for one customer; the row carries its
own dates. Supply is a **union of two kinds**: a concrete Vehicle (a VIN) or a
PO-line slot (a future car, keyed PO-model-row, not yet built). The solver
matches rows ↔ supply and does not care which kind a unit is — both are
capacity-1 supply with a `sales_model` and an expected delivery date.

Everything is keyed on **real dates** (`YYYY-MM-DD`); tardiness is in **days**.
`now` is the pull date, carried on the snapshot so the fence is pure.
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
    """One vehicle order row — the demand side, the 'order' in the match."""

    order_id: str  # the row id, e.g. "SO-4000-1"
    so_id: str  # parent Sales Order
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
            "so_id": self.so_id,
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
            so_id=str(d.get("so_id", "")),
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
    """One supply item — a concrete Vehicle OR a PO-line slot (a future car)."""

    vehicle_id: str  # supply id: a VIN ("VEH-9000") or a slot ref ("PO-150-1-5")
    kind: str  # "vehicle" | "po_line"
    sales_model: str
    planned_delivery_date: date  # the ONE mutable field disruptions write
    location_state: str  # vehicle pipeline stage; "future" for a PO-line slot
    po_ref: str  # the PO-line this fulfils, e.g. "PO-150-1-5"
    pdn: str  # PDN batch for a vehicle; "" for a PO-line slot
    committed: bool  # derived from location_state at flatten time

    def to_dict(self) -> dict:
        return {
            "vehicle_id": self.vehicle_id,
            "kind": self.kind,
            "sales_model": self.sales_model,
            "planned_delivery_date": date_label(self.planned_delivery_date),
            "location_state": self.location_state,
            "po_ref": self.po_ref,
            "pdn": self.pdn,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Unit:
        return cls(
            vehicle_id=str(d["vehicle_id"]),
            kind=d.get("kind", "vehicle"),
            sales_model=d["sales_model"],
            planned_delivery_date=parse_date(d["planned_delivery_date"]),
            location_state=d["location_state"],
            po_ref=d.get("po_ref", ""),
            pdn=d.get("pdn", ""),
            committed=bool(d["committed"]),
        )


@dataclass
class Snapshot:
    """Everything one solve consumes — the flattened, frozen pull."""

    orders: list[Order]  # vehicle order rows
    units: list[Unit]  # supply: vehicles ∪ PO-line slots
    incumbent: dict[str, str]  # row_id -> supply_id (current allocation)
    disruption: dict  # the delayed PO + who it touched
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

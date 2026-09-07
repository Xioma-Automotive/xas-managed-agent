"""The allocation snapshot the solver reads — date-based, real-XAS-shaped.

This is the *frozen* half of the core invariant:

    plan = pure_function(data_snapshot, skill, override)

The export's two row streams (`orders.csv` + `vehicles.csv`, carved out of the
real XAS export by `scenario_engine/real_*.py`) are translated host-side by
`datasource.translate` and flattened into the three arrays here by `flatten.py`,
in the sandbox. This module owns only the flattened shape the solver consumes and
its JSON (de)serialization.

Grain: the allocatable **order** is one **wanted car**, and the "Allocation solve
contract" keys it ``DMSJCNum`` + ``LineNum`` — a job card and one line on it
(``900108-1``). One line is still one car: there is no ``Quantity`` and no "a line
asking for 3 cars is planned as 1" question. The card half is what makes an
instruction about a whole card resolvable (``solver.names_order``); the line half
is what keeps two lines of one card from collapsing into each other.

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

    Three priced fields, the two that make the key, and four labels — that is the
    whole of the demand side. Nothing below the priced three is read by the
    solver; they exist so a planner-facing table can name the order the way the
    DMS does and so a write-back has the handle it needs.

    What is NOT here:

    * **no priority.** The record's letter was never a planner's decision and the
      export has no such column, so priority is a per-turn LEVER on the override
      (``solver._combined_priority``).
    * **no customer DIMENSION.** ``customer`` below is a LABEL: it is carried so
      an instruction about a client can be resolved to order ids and so the plan
      can name who each order is for. Nothing reads it — not eligibility, not
      cost, not a filter (the ``customers`` filter dimension went on 2026-08-27
      and stays gone). Steering names order ids.
    * **no delay history and no price.** The three escalation fields were read
      only by weight terms deleted on 2026-08-26 and were zero on every real row;
      ``price`` was display-only and the export does not carry it.
    * **no ``AllocType`` PRICE.** ``alloc_type`` below is carried and shown, and
      ``solver.break_cost_of`` deliberately does not read it — re-splitting the
      break cost hard/soft is DECIDE-3's retired mechanism, and it needs its own
      decision and a validated pair of numbers.
    * **no job-card STATUS.** The contract carries ``JobStatus`` in the file
      because the DMS does; nothing here reads it, and every order in this export
      is an open card, so adding a field would be a column with one value in it.
    """

    job_card: str  # DMSJCNum — the card this line sits on, e.g. "900108"
    line: str  # LineNum — which line of it. Together with job_card, the key.
    sales_model: str  # the eligibility key (SalesModel), matched by equality
    delivery_date: date  # etaDealer on the ORDER — the promise. NOT the car's date.
    # Accounts.Owner.AccountName — display and lookup only, never priced.
    customer: str = ""
    # Accounts.Owner.AccountDMSCode — the account's real key. What a client
    # instruction is resolved THROUGH; the name is what it is resolved FROM.
    account_code: str = ""
    # AllocType — how firmly the customer is committed. Shown, never priced.
    alloc_type: str = ""
    # DMSJCEntry — the DMS's handle on the card, for a write-back to quote.
    entry: str = ""

    @property
    def key(self) -> str:
        """The unique order key: the card and the line, ``900108-1``.

        Built HERE and nowhere else. ``datasource.order_key`` builds the same
        string on the host side from the same two columns, because the host has
        rows and not ``Order``s; the two agreeing is what makes a key the planner
        saw in one turn still name the same line in the next."""
        return f"{self.job_card}-{self.line}"

    def to_dict(self) -> dict:
        return {
            "job_card": self.job_card,
            "line": self.line,
            "sales_model": self.sales_model,
            "delivery_date": date_label(self.delivery_date),
            "customer": self.customer,
            "account_code": self.account_code,
            "alloc_type": self.alloc_type,
            "entry": self.entry,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Order:
        return cls(
            job_card=str(d["job_card"]),
            line=str(d["line"]),
            sales_model=d["sales_model"],
            delivery_date=parse_date(d["delivery_date"]),
            customer=str(d.get("customer") or ""),
            account_code=str(d.get("account_code") or ""),
            alloc_type=str(d.get("alloc_type") or ""),
            entry=str(d.get("entry") or ""),
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

    orders: list[Order]  # one per wanted CAR — one line of one job card
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

    def orders_on_card(self, job_card: str) -> list[Order]:
        """Every line of one card, in line order. What "the whole card" means when
        a planner names a card number rather than a line."""
        return sorted(
            (o for o in self.orders if o.job_card == job_card), key=lambda o: (o.line, o.key)
        )

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

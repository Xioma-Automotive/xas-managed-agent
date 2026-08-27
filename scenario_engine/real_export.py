"""Shared plumbing for the two scenarios cut from the REAL XAS export.

The export (`data/vehicles.csv` + `data/orders.csv`) holds nothing to solve:
every one of its 1641 orders already has exactly one car, and a car's
`status.name` IS its allocation state — every "Dealer Order Confirmation" and
"Dealer Reservation" car is claimed by one order, every "Available For Sale" car
by none, with no contested cars. Each scenario script manufactures a decision out
of it in a different way:

  ``real_unallocated``  deletes allocations — orders that need a car at all.
  ``real_delayed``      keeps allocations and delays the cars — orders whose car
                        now lands past its promise, which the solver may repair.

Both take the same four knobs (how many orders to disturb, how many extra cars to
free, subset size, available share) and emit the export's own CSV shape. This
module holds what they agree on: the export's vocabulary, the CSV I/O, the pool
composition arithmetic and the feasibility report.
"""

from __future__ import annotations

import argparse
import csv
import random
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# The export's capture date. Fixed, not wall-clock: a car whose ``availableBy``
# has passed is already on the dealer's hands, and no scenario may move it.
CAPTURED = date(2026, 8, 25)

# A car is in the pool at all only in these three states — the rest are delivered,
# registered, in dispute or demo stock.
POOL_STATUSES = {"available for sale", "dealer order confirmation", "dealer reservation"}
AVAILABLE = "available for sale"

# What a freed car becomes. The trailing space is REAL — 86% of the export's
# available cars carry it — so the mock keeps the trap a mapper has to strip.
AVAILABLE_CODE, AVAILABLE_NAME = "2", "Available For Sale "

# Cleared on an order whose allocation is deleted. ``description`` opens with the
# vehicleCode ("1004326 - OMODA9 ..."), so it names the car, not the demand.
ALLOCATION_FIELDS = ("vehicleCode", "description")


# --- the export's vocabulary -------------------------------------------------


def day(stamp: str) -> date:
    """The export stamps everything as UTC midnight, noon or 22:00; only the day
    matters to eligibility, so compare days and keep the clock time untouched."""
    return date.fromisoformat(stamp[:10])


def restamp(stamp: str, new_day: date) -> str:
    """Move a timestamp's date, keeping the export's own time-of-day."""
    return f"{new_day.isoformat()}{stamp[10:]}"


def in_pool(vehicle: dict[str, str]) -> bool:
    return vehicle["status.name"].strip().lower() in POOL_STATUSES


def is_available(vehicle: dict[str, str]) -> bool:
    return vehicle["status.name"].strip().lower() == AVAILABLE


def slack_days(order: dict[str, str], vehicle: dict[str, str]) -> int:
    """Days between the car landing and the promise. Negative means late."""
    return (day(order["etaDealer"]) - day(vehicle["availableBy"])).days


def deallocate_order(order: dict[str, str]) -> dict[str, str]:
    """The order still wants a car; it no longer says which one."""
    return {**order, **{field: "" for field in ALLOCATION_FIELDS}}


def free_vehicle(vehicle: dict[str, str]) -> dict[str, str]:
    """Release the car. Its physical stage (``inv status label``) does not move:
    freeing a car in Sea Transit does not put it on the lot, and the export shows
    available cars in every stage."""
    return {**vehicle, "status.code": AVAILABLE_CODE, "status.name": AVAILABLE_NAME}


def delay_vehicle(vehicle: dict[str, str], lands_on: date) -> dict[str, str]:
    """Slip the car's arrival. Only ``availableBy`` moves — the allocation stands,
    and the physical stage is where the shipment still says it is."""
    return {**vehicle, "availableBy": restamp(vehicle["availableBy"], lands_on)}


# --- CSV I/O -----------------------------------------------------------------


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)


class Export:
    """The real export, loaded once, with its header rows kept for the output."""

    def __init__(self, orders_in: Path, vehicles_in: Path) -> None:
        self.order_fields, self.orders = read_csv(orders_in)
        self.vehicle_fields, self.vehicles = read_csv(vehicles_in)
        self.by_code = {v["vehicleCode"]: v for v in self.vehicles}

        missing = [o["OrderId"] for o in self.orders if not o["etaDealer"].strip()]
        if missing:
            raise SystemExit(
                f"{len(missing)} orders in {orders_in.name} have no etaDealer. Every scenario "
                f"here turns on a promised date — an order with no promise can be neither late "
                f"nor met — so fill them from each car's availableBy first."
            )
        self.allocated = [o for o in self.orders if o["vehicleCode"] in self.by_code]
        self.available = [v for v in self.vehicles if is_available(v) and in_pool(v)]
        print(
            f"{orders_in.name}: {len(self.orders)} orders ({len(self.allocated)} allocated)  "
            f"{vehicles_in.name}: {len(self.vehicles)} vehicles "
            f"({len(self.available)} available, {sum(1 for v in self.vehicles if in_pool(v))} in the pool)"
        )

    def car(self, order: dict[str, str]) -> dict[str, str]:
        return self.by_code[order["vehicleCode"]]

    def emit(self, out: Path, orders: list[dict[str, str]], vehicles: list[dict[str, str]]) -> None:
        write_csv(out / "orders.csv", self.order_fields, orders)
        write_csv(out / "vehicles.csv", self.vehicle_fields, vehicles)
        print(f"\nwrote {out / 'orders.csv'} and {out / 'vehicles.csv'}")


# --- pool composition --------------------------------------------------------


def pool_split(
    *,
    subset: int,
    available_pct: float,
    freed: int,
    already_available: int,
    allocated_orders: int,
) -> tuple[int, int]:
    """Split the subset into (cars padded in from the already-available stock,
    cars left allocated). ``freed`` is however many cars this scenario releases by
    deleting an allocation — they are the FIRST cars counted toward the available
    share, and the rest is padding."""
    free_target = round(subset * available_pct / 100)
    pad = free_target - freed
    keep_allocated = subset - free_target

    if pad < 0:
        raise SystemExit(
            f"this scenario frees {freed} cars, but {available_pct}% of {subset} is only "
            f"{free_target}. Raise the subset size or the available percentage."
        )
    if pad > already_available:
        raise SystemExit(
            f"reaching {available_pct}% of {subset} needs {pad} cars that are already "
            f"available, and only {already_available} exist. Lower the percentage, or free more."
        )
    if keep_allocated < 0:
        raise SystemExit(f"{available_pct}% is not a share of a subset.")
    if freed + keep_allocated > allocated_orders:
        raise SystemExit(
            f"this scenario needs {freed + keep_allocated} allocated orders "
            f"({freed} released + {keep_allocated} left alone), only {allocated_orders} exist."
        )
    return pad, keep_allocated


def delayable(data: Export) -> list[dict[str, str]]:
    """Allocated orders whose car is still INBOUND and currently ON TIME — the only
    ones a delay can turn late without rewriting the past. A car whose
    ``availableBy`` has passed is already on the dealer's hands."""
    return [
        o
        for o in data.allocated
        if day(data.car(o)["availableBy"]) > CAPTURED and slack_days(o, data.car(o)) >= 0
    ]


def focus(data: Export, models: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Narrow the draw to the ``models`` most-demanded sales models, or all of them
    at 0. Eligibility is sales-model equality, so a subset spread thin across all
    66 models gives most orders nothing but their own car back — 60 cars over 66
    models is 0.9 cars each. Concentrating is the only way a SMALL subset poses a
    choice; it makes the scenario less representative of the whole book, which is
    the trade."""
    if models <= 0:
        return data.allocated, data.available
    ranked = Counter(o["SalesModel"] for o in data.allocated).most_common(models)
    wanted = {model for model, _ in ranked}
    return (
        [o for o in data.allocated if o["SalesModel"] in wanted],
        [v for v in data.available if v["SalesModel"] in wanted],
    )


def carve(
    data: Export,
    *,
    empty: int,
    late: int,
    days_late: tuple[int, int],
    extra_free: int,
    subset: int,
    available_pct: float,
    models: int,
    seed: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Cut one scenario. ``empty`` orders lose their car, ``late`` orders keep
    theirs and have it slipped past the promise, ``extra_free`` more cars are
    released with their orders dropped from the book. Either disturbance may be 0,
    which is what makes the single-purpose scripts special cases of this one."""
    rng = random.Random(seed)
    allocated, available = focus(data, models)
    if models > 0:
        print(
            f"  narrowed to the {models} most-demanded sales models: "
            f"{len(allocated)} allocated orders, {len(available)} available cars"
        )
    pad, keep_allocated = pool_split(
        subset=subset,
        available_pct=available_pct,
        freed=empty + extra_free,
        already_available=len(available),
        allocated_orders=len(allocated),
    )
    if late > keep_allocated:
        raise SystemExit(
            f"cannot make {late} orders late: {subset} cars at {available_pct}% available leaves "
            f"only {keep_allocated} orders allocated to slip."
        )
    focused_ids = {o["OrderId"] for o in allocated}
    candidates = [o for o in delayable(data) if o["OrderId"] in focused_ids]
    if late > len(candidates):
        raise SystemExit(
            f"cannot make {late} orders late: only {len(candidates)} allocated orders hold a car "
            f"that is still inbound and currently on time."
        )

    # The delayed orders are drawn from the eligible candidates first; the rest of
    # the book then fills the subset around them.
    slipped = rng.sample(candidates, late)
    slipped_ids = {o["OrderId"] for o in slipped}
    rest = [o for o in allocated if o["OrderId"] not in slipped_ids]
    taken = rng.sample(rest, empty + extra_free + keep_allocated - late)
    emptied = taken[:empty]
    dropped = taken[empty : empty + extra_free]
    untouched = taken[empty + extra_free :]
    padding = rng.sample(available, pad)

    slips = {
        o["vehicleCode"]: day(o["etaDealer"]) + timedelta(days=rng.randint(*days_late))
        for o in slipped
    }
    freed_codes = {o["vehicleCode"] for o in emptied + dropped}
    kept_codes = (
        set(slips)
        | freed_codes
        | {v["vehicleCode"] for v in padding}
        | {o["vehicleCode"] for o in untouched}
    )
    emptied_ids = {o["OrderId"] for o in emptied}
    kept_ids = emptied_ids | slipped_ids | {o["OrderId"] for o in untouched}

    def shape(vehicle: dict[str, str]) -> dict[str, str]:
        code = vehicle["vehicleCode"]
        if code in slips:
            return delay_vehicle(vehicle, slips[code])
        return free_vehicle(vehicle) if code in freed_codes else vehicle

    # Input order preserved on both sides, so the output diffs against the export.
    vehicles = [shape(v) for v in data.vehicles if v["vehicleCode"] in kept_codes]
    orders = [
        deallocate_order(o) if o["OrderId"] in emptied_ids else o
        for o in data.orders
        if o["OrderId"] in kept_ids
    ]

    available = len(freed_codes) + pad
    print(
        f"\nvehicles {len(vehicles)}  available {available} "
        f"({100 * available / len(vehicles):.0f}%) = {empty} freed by emptying + {extra_free} extra "
        f"+ {pad} already available"
    )
    inherited = len(late_orders(orders, vehicles)) - late
    print(
        f"orders   {len(orders)}  unallocated {empty}  allocated {keep_allocated} "
        f"({late} delayed here, {inherited} already late in the export, "
        f"{keep_allocated - late - inherited} on time)  (dropped from the book {extra_free})"
    )
    return orders, vehicles


def run(data: Export, out: Path, **knobs: object) -> None:
    """Carve, write, and report what the planner would face."""
    orders, vehicles = carve(data, **knobs)  # type: ignore[arg-type]
    data.emit(out, orders, vehicles)

    late = late_orders(orders, vehicles)
    if late:
        by_code = {v["vehicleCode"]: v for v in vehicles}
        gaps = sorted(-slack_days(o, by_code[o["vehicleCode"]]) for o in late)
        print(f"days late: min {gaps[0]} median {gaps[len(gaps) // 2]} max {gaps[-1]}")
    repairability(
        [o for o in orders if not o["vehicleCode"].strip()], vehicles, "unallocated orders"
    )
    repairability(late, vehicles, "late orders")


def late_orders(
    orders: list[dict[str, str]], vehicles: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Orders holding a car that lands after the promise — measured on the output,
    not on what a scenario meant to do. The export ships 256 already-late orders,
    so any subset inherits some whether the scenario delayed anything or not."""
    by_code = {v["vehicleCode"]: v for v in vehicles}
    return [
        o
        for o in orders
        if o["vehicleCode"].strip() and slack_days(o, by_code[o["vehicleCode"]]) < 0
    ]


def repairability(
    orders: list[dict[str, str]],
    vehicles: list[dict[str, str]],
    label: str,
) -> None:
    """For each order in ``orders``, what the free pool could do for it: an
    eligible car is one of the SAME sales model, and it helps only if it lands by
    the promise. Eligibility is sales-model equality, exactly as the solver reads it."""
    if not orders:
        return
    by_model: dict[str, list[dict[str, str]]] = {}
    for car in vehicles:
        if is_available(car):
            by_model.setdefault(car["SalesModel"], []).append(car)

    counts, on_time = [], 0
    for order in orders:
        cars = by_model.get(order["SalesModel"], [])
        counts.append(len(cars))
        if any(day(c["availableBy"]) <= day(order["etaDealer"]) for c in cars):
            on_time += 1
    counts.sort()
    print(
        f"{label}: eligible free cars min {counts[0]} median {counts[len(counts) // 2]} "
        f"max {counts[-1]}; {on_time} of {len(orders)} have one that lands by the promise"
    )
    print(f"  with no eligible car at all: {sum(1 for c in counts if c == 0)}")


# --- the command line, shared ------------------------------------------------


def base_parser(description: str, default_out: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--orders-in", type=Path, default=DATA / "orders.csv")
    parser.add_argument("--vehicles-in", type=Path, default=DATA / "vehicles.csv")
    parser.add_argument("--out", type=Path, default=DATA / default_out)
    parser.add_argument(
        "--extra-free",
        type=int,
        help="further cars freed by deleting an allocation; their orders leave the book",
    )
    parser.add_argument("--subset", type=int, help="vehicles in the subset")
    parser.add_argument("--available-pct", type=float, help="available share of the subset")
    parser.add_argument(
        "--models",
        type=int,
        help="narrow the whole subset to the N most-demanded sales models (default: all 66). "
        "Flag only — it is not prompted, since a big subset rarely needs it.",
    )
    parser.add_argument("--seed", type=int, default=1)
    return parser


def ask_int(value: int | None, question: str, default: int) -> int:
    if value is not None:
        return value
    raw = input(f"{question} [{default}]: ").strip()
    return int(raw) if raw else default


def ask_float(value: float | None, question: str, default: float) -> float:
    if value is not None:
        return value
    raw = input(f"{question} [{default}]: ").strip()
    return float(raw) if raw else default


def ask_range(value: str | None, question: str, default: str) -> tuple[int, int]:
    """A span written "1-20", or a single number for a fixed one."""
    raw = value if value is not None else (input(f"{question} [{default}]: ").strip() or default)
    low, _, high = raw.partition("-")
    span = (int(low), int(high or low))
    if span[0] > span[1] or span[0] < 1:
        raise SystemExit(f"'{raw}' is not a span of at least one day.")
    return span

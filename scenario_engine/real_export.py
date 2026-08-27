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
  ``real_mixed``        both at once; the other two are this one with a count
                        pinned to zero.

Every book has THREE classes of order and the scenarios differ only in the mix:
unallocated, late, and allocated-and-on-time. The last is a share
(``--on-time-pct``, 20% by default) rather than a count, because it is the
control group: what a plan does NOT touch is only readable if some orders needed
nothing. The book's SIZE follows from the mix — 8 disturbed orders at a 20% share
is a 10-order book — and the car subset follows from the book, so neither is
asked for.

All three take the same knobs (how many orders to disturb, how far past the
promise, how many extra cars to free, the on-time share, the available share) and
emit the export's own CSV shape. This module holds what they agree on: the
export's vocabulary, the CSV I/O, the pool composition arithmetic and the
feasibility report.
"""

from __future__ import annotations

import argparse
import csv
import json
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
        # The pull date has no column, and it cannot be the clock: these are
        # static files, so "today" would make the same rows mean something
        # different tomorrow — an order late by 3 days becomes late by 4 with
        # nothing having changed. `datasource.scenario_now` reads this.
        (out / "scenario.json").write_text(
            json.dumps({"now": CAPTURED.isoformat()}, indent=2) + "\n"
        )
        print(f"\nwrote {out / 'orders.csv'}, {out / 'vehicles.csv'} and scenario.json")


# --- pool composition --------------------------------------------------------


def on_time_share(disturbed: int, on_time_pct: float) -> int:
    """How many orders ride in UNTOUCHED: allocated, on time, nothing done to them.

    ``on_time_pct`` is their share of the WHOLE book, so 20% on 8 disturbed orders
    is 2 untouched and a book of 10 — the book SIZE follows from the disturbance
    counts rather than being asked for separately.

    They are the scenario's control group. Without them every order in the book
    needs something, and a plan that moves everything cannot be told apart from
    one that moves only what it should.
    """
    if not 0 <= on_time_pct < 100:
        raise SystemExit(f"{on_time_pct}% is not a share of a book (0 <= pct < 100).")
    return round(disturbed * on_time_pct / (100 - on_time_pct))


def pool_split(
    *,
    held: int,
    freed: int,
    available_pct: float,
    already_available: int,
) -> tuple[int, int]:
    """The car side: (cars padded in from the already-available stock, cars in the
    subset in total).

    ``held`` cars belong to orders that keep them — the delayed ones plus the
    untouched ones. ``freed`` is however many cars this scenario releases by
    deleting an allocation; they are the FIRST cars counted toward the available
    share, and the rest is padding. The subset SIZE is derived, not asked for: a
    car no order in the book holds is in the subset only to be supply.
    """
    if not 0 <= available_pct < 100:
        raise SystemExit(f"{available_pct}% is not a share of a subset (0 <= pct < 100).")
    subset = round(held / (1 - available_pct / 100)) if held else freed
    free_target = subset - held
    pad = free_target - freed

    if pad < 0:
        raise SystemExit(
            f"this scenario frees {freed} cars, and {available_pct}% of the {subset}-car subset "
            f"its book implies is only {free_target}. Raise the available percentage, free "
            f"fewer, or keep more orders."
        )
    if pad > already_available:
        raise SystemExit(
            f"reaching {available_pct}% of {subset} needs {pad} cars that are already "
            f"available, and only {already_available} exist. Lower the percentage, or free more."
        )
    return pad, subset


def delayable(data: Export) -> list[dict[str, str]]:
    """Allocated orders whose car is still INBOUND and currently ON TIME — the only
    ones a delay can turn late without rewriting the past. A car whose
    ``availableBy`` has passed is already on the dealer's hands."""
    return [
        o
        for o in data.allocated
        if day(data.car(o)["availableBy"]) > CAPTURED and slack_days(o, data.car(o)) >= 0
    ]


def on_time_orders(data: Export) -> list[dict[str, str]]:
    """Allocated orders whose car lands BY the promise — the only ones the
    untouched draw may take. The export ships 256 already-late orders, so drawing
    the remainder at random used to fold some of them into the on-time share and
    the reported late count came out above the one that was asked for."""
    return [o for o in data.allocated if slack_days(o, data.car(o)) >= 0]


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
    on_time_pct: float,
    available_pct: float,
    models: int,
    seed: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Cut one scenario. ``empty`` orders lose their car, ``late`` orders keep
    theirs and have it slipped past the promise, ``on_time_pct`` of the book rides
    in untouched, and ``extra_free`` more cars are released with their orders
    dropped from the book. Either disturbance may be 0, which is what makes the
    single-purpose scripts special cases of this one.

    The book is three classes and nothing else — unallocated, late, on time — so
    its size is ``empty + late`` grossed up by the on-time share, and the car
    subset follows from that.
    """
    rng = random.Random(seed)
    allocated, available = focus(data, models)
    if models > 0:
        print(
            f"  narrowed to the {models} most-demanded sales models: "
            f"{len(allocated)} allocated orders, {len(available)} available cars"
        )
    keep = on_time_share(empty + late, on_time_pct)
    pad, subset = pool_split(
        held=late + keep,
        freed=empty + extra_free,
        available_pct=available_pct,
        already_available=len(available),
    )
    focused_ids = {o["OrderId"] for o in allocated}
    candidates = [o for o in delayable(data) if o["OrderId"] in focused_ids]
    if late > len(candidates):
        raise SystemExit(
            f"cannot make {late} orders late: only {len(candidates)} allocated orders hold a car "
            f"that is still inbound and currently on time."
        )

    # The delayed orders are drawn from the eligible candidates first, then the
    # on-time ones from what is left — and they are drawn from orders that are
    # ACTUALLY on time, so the share means what it says. The emptied and dropped
    # orders come from anywhere: they end up holding no car at all.
    slipped = rng.sample(candidates, late)
    slipped_ids = {o["OrderId"] for o in slipped}
    intact = [
        o
        for o in on_time_orders(data)
        if o["OrderId"] in focused_ids and o["OrderId"] not in slipped_ids
    ]
    if keep > len(intact):
        raise SystemExit(
            f"cannot keep {keep} orders on time ({on_time_pct}% of the book): only "
            f"{len(intact)} allocated orders are on time and not already delayed here."
        )
    untouched = rng.sample(intact, keep)
    spoken = slipped_ids | {o["OrderId"] for o in untouched}
    rest = [o for o in allocated if o["OrderId"] not in spoken]
    if empty + extra_free > len(rest):
        raise SystemExit(
            f"this scenario needs {empty + extra_free} more allocated orders to release "
            f"({empty} emptied + {extra_free} dropped from the book), only {len(rest)} are left."
        )
    taken = rng.sample(rest, empty + extra_free)
    emptied = taken[:empty]
    dropped = taken[empty:]
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
    if len(vehicles) != subset:
        # Every car is held by one order in the book, freed by this scenario, or
        # padding. A mismatch means two of those sets overlap.
        raise SystemExit(f"composed {len(vehicles)} cars, the mix implies {subset}.")
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
    print(
        f"orders   {len(orders)}  unallocated {empty}  late {late}  on time {keep} "
        f"({100 * keep / len(orders):.0f}% of the book)  (dropped from the book {extra_free})"
    )
    inherited = len(late_orders(orders, vehicles)) - late
    if inherited:
        # The on-time draw is on-time-only, so this cannot happen — and if it ever
        # does, the share above is a lie, which is worse than a crash.
        raise SystemExit(
            f"{inherited} orders in the book are late that this scenario did not delay; "
            f"the on-time share is not what it says."
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
    and `carve` keeps them out of the on-time draw, so this is the meter that says
    it worked: it must come back exactly the ``late`` that was asked for."""
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
    parser.add_argument(
        "--on-time-pct",
        type=float,
        help="share of the order book that rides in untouched — allocated and on time",
    )
    parser.add_argument("--available-pct", type=float, help="available share of the subset")
    parser.add_argument(
        "--models",
        type=int,
        help="narrow the whole subset to the N most-demanded sales models, 0 for all 66 "
        "(default: 2). Flag only — it is not prompted, but a ten-order book needs it: "
        "spread over 66 models, most orders see nothing but their own car back.",
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

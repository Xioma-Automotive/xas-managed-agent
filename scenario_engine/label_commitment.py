"""Label every order in the REAL export hard or soft — a one-off, re-runnable pass.

The export does not carry an `allocationType` column, but it carries the fact:
every one of its 1641 orders holds a car, and that car is either a **Dealer Order
Confirmation** (1380) or a **Dealer Reservation** (261). A confirmation is a firm
customer order; a reservation is a hold somebody pencilled against a car. So the
column is DERIVED from the vendor's own data, not invented — unlike
`customer.name`, which is assigned at random because the export has no such thing.

    Dealer Order Confirmation -> hard
    Dealer Reservation        -> soft

It is written onto the ORDER row, and that matters: hard/soft is a property of the
DEMAND — how firmly this customer is committed — so it must survive the allocation
being deleted. An unallocated hard order is still a hard order. It also has to be
frozen here, BEFORE any carve: `real_export.free_vehicle` rewrites a freed car's
`status.name` to 'Available For Sale ', which is the very field this reads.

Run it against the export, once, and again whenever a fresh export lands:

    uv run python -m scenario_engine.label_commitment

Idempotent — the column is rewritten from the cars each time, never accumulated.
The scenario scripts need no change to carry it: `carve` copies the header and
whole row dicts, and `deallocate_order` clears only the two allocation fields.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from scenario_engine.real_export import DATA, read_csv, write_csv

# The column the solver ends up pricing with, and the two values it holds.
COLUMN = "allocationType"
HARD, SOFT = "hard", "soft"

# The export's own status vocabulary, lowercased. Only the two ALLOCATED states
# appear here: an order never holds an 'Available For Sale' car, and the rest of
# the statuses (Delivered, Registered, In Dispute, Demo) are not in the pool at
# all.
COMMITMENT_BY_STATUS = {
    "dealer order confirmation": HARD,
    "dealer reservation": SOFT,
}


def commitment_of(order: dict[str, str], by_code: dict[str, dict[str, str]]) -> str:
    """This order's commitment, from the car it holds in the REAL export.

    Falls back to ``hard`` for an order holding no car, or one whose car is in a
    status this mapping does not know. That direction is deliberate: an
    unclassifiable promise is treated as FIRM, so nothing is cheap to break by
    accident. Every row of today's export hits the mapping; the fallback is for a
    refreshed export that grows an unallocated order."""
    vehicle = by_code.get(order["vehicleCode"].strip())
    if not vehicle:
        return HARD
    return COMMITMENT_BY_STATUS.get(vehicle["status.name"].strip().lower(), HARD)


def label(orders: list[dict[str, str]], vehicles: list[dict[str, str]]) -> Counter:
    """Write ``allocationType`` onto every order row in place; return the split."""
    by_code = {v["vehicleCode"]: v for v in vehicles}
    tally: Counter = Counter()
    for order in orders:
        order[COLUMN] = commitment_of(order, by_code)
        tally[order[COLUMN]] += 1
    return tally


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--orders", type=Path, default=DATA / "orders.csv")
    parser.add_argument("--vehicles", type=Path, default=DATA / "vehicles.csv")
    args = parser.parse_args()

    order_fields, orders = read_csv(args.orders)
    _, vehicles = read_csv(args.vehicles)
    tally = label(orders, vehicles)

    # Appended last, where `customer.name` went. Re-running must not add it twice.
    if COLUMN not in order_fields:
        order_fields.append(COLUMN)
    write_csv(args.orders, order_fields, orders)

    total = sum(tally.values())
    print(f"{args.orders}: {total} orders labelled")
    for value, n in sorted(tally.items()):
        print(f"  {value:5} {n:5}  ({100 * n / total:.0f}%)")


if __name__ == "__main__":
    main()

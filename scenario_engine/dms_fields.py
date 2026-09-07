"""Give the REAL export the DMS columns the solve contract requires — a one-off,
re-runnable pass, the sibling of `scenario_engine.label_commitment`.

The contract ("Allocation solve contract") names columns that a real
`VehiclePlanning/workspace` row carries and this export does not: a job-card
number and line number, the card's DMS entry id and status, the account code
beside the account name, and seven vehicle fields. This pass writes them onto
`data/orders.csv` + `data/vehicles.csv` so the carve — which copies whole rows —
carries them into every scenario for free, and `datasource.translate` stays a
projection with no invention in it.

    uv run python -m scenario_engine.label_commitment   # allocationType, first
    uv run python -m scenario_engine.dms_fields         # then this
    uv run python -m scenario_engine.real_mixed         # then re-carve

Idempotent: every column is recomputed from the export each run, never appended
to. Run it again whenever a fresh export lands.

**Three of these are DERIVED and four are BLANK, and the difference matters.**

Derived from the vendor's own data, so they mean something:

* `DMSJCNum` / `LineNum` / `DMSJCEntry` — **one card per account per promised
  month**. That is how a card is actually raised: one customer, one batch. It
  gives 172 cards over the export's 1641 orders, ~9 lines each, and — the reason
  the grouping is not something coarser — a ten-order carve then lands two or
  three lines of one card often enough for the card+line key to be exercised
  rather than merely declared. A card per ACCOUNT was rejected: it would make
  naming a card the same act as naming a client, quietly restoring the
  client-wide lever removed on 2026-08-27.
* `Accounts.Owner.AccountDMSCode` — one stable code per `customer.name`. It is
  the key a client instruction should resolve through; the name is the label.
* `InventoryEnteringDate` — the "In Stock" date. A car that has already landed
  (`availableBy` on or before the capture date) entered stock then; a car still
  inbound is not in stock and the field is EMPTY. Real, and about half the fleet.

Blank, because the export has no such thing and a fabricated value would be
indistinguishable from a real one:

* `JobStatus.Code` / `JobStatus.Label` — every order in this export is an open,
  in-process card, so this is written as that one real value rather than a
  vocabulary invented to look plural. A book where some cards were `Canceled`
  would be a book with cancelled demand in it, which is not what was carved.
* `SalesStatus`, `PurchaseStatus`, `RegulatoryStatus`, `OperationalStatus`,
  `TransferRequired`, `OpenDamage` — present and EMPTY. The export carries one
  status axis (`status.*`) and one physical stage (`inventoryStatus`); the other
  four axes and the two candidate-wrapper flags exist only in the live DMS. An
  empty column says "this export has no such thing" where a plausible value would
  say the opposite, and nothing prices them.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from scenario_engine.real_export import CAPTURED, DATA, read_csv, write_csv

# --- the columns this pass owns ----------------------------------------------
# Named as the CONTRACT names them, not in the export's own style: nothing in the
# export named them first, so a second spelling would only be a synonym to keep in
# step. The columns the export DOES have (`customer.name`, `etaDealer`,
# `availableBy`, `status.*`, `inventoryStatus`, `allocationType`) keep their own
# names and are renamed once, in `datasource.translate`.
CARD_NUM = "DMSJCNum"
LINE_NUM = "LineNum"
CARD_ENTRY = "DMSJCEntry"
JOB_STATUS_CODE = "JobStatus.Code"
JOB_STATUS_LABEL = "JobStatus.Label"
ACCOUNT_CODE = "Accounts.Owner.AccountDMSCode"

ORDER_COLUMNS = (CARD_NUM, LINE_NUM, CARD_ENTRY, JOB_STATUS_CODE, JOB_STATUS_LABEL, ACCOUNT_CODE)

ENTERED_STOCK = "InventoryEnteringDate"
VEHICLE_BLANKS = (
    "SalesStatus",
    "PurchaseStatus",
    "RegulatoryStatus",
    "OperationalStatus",
    "TransferRequired",
    "OpenDamage",
)
VEHICLE_COLUMNS = (ENTERED_STOCK, *VEHICLE_BLANKS)

# The one job-card status this export supports: every order in it is live demand
# against a car. See the module docstring for why it is not made plural.
OPEN_CARD = ("02", "In Process")

# Bases chosen to be six digits and to not collide with an OrderId (which run in
# the 5xxxxx range), so a card number is never mistaken for an order number in a
# planner-facing table.
CARD_BASE = 900001
ENTRY_BASE = 100001


def card_of(order: dict[str, str]) -> tuple[str, str]:
    """The card this order line belongs to: its account and its promised month.

    An order with no account name is its own card — it is grouped with the other
    nameless ones only if they share a month, which is as much as the data says.
    """
    return order.get("customer.name", "").strip(), order["etaDealer"][:7]


def account_codes(orders: list[dict[str, str]]) -> dict[str, str]:
    """One stable DMS code per account name, in name order. Deterministic, so a
    re-run of this pass does not renumber a customer that has not changed."""
    names = sorted({o.get("customer.name", "").strip() for o in orders} - {""})
    return {name: f"C{n:04d}" for n, name in enumerate(names, start=1)}


def number_cards(orders: list[dict[str, str]]) -> None:
    """Write the card number, line number, entry id, status and account code onto
    every order row, in place.

    Cards are numbered in (account, month) order and lines within a card in
    `OrderId` order, so the whole numbering is a pure function of the export —
    two runs agree, and a carve can take any subset of a card's lines without the
    surviving line numbers shifting under it.
    """
    codes = account_codes(orders)
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for order in orders:
        grouped[card_of(order)].append(order)

    for n, key in enumerate(sorted(grouped)):
        lines = sorted(grouped[key], key=lambda o: o["OrderId"])
        for line_no, order in enumerate(lines, start=1):
            order[CARD_NUM] = str(CARD_BASE + n)
            order[LINE_NUM] = str(line_no)
            order[CARD_ENTRY] = str(ENTRY_BASE + n)
            order[JOB_STATUS_CODE], order[JOB_STATUS_LABEL] = OPEN_CARD
            order[ACCOUNT_CODE] = codes.get(key[0], "")


def entered_stock(vehicle: dict[str, str], captured: str) -> str:
    """When this car entered stock, or "" while it is still inbound.

    A car whose arrival date has already passed on the capture date is on the
    dealer's hands, and that arrival IS the day it entered stock. One still on
    its way has not entered stock at all, and an empty cell says so — unlike a
    guessed date, which would put a car in stock that is at sea.
    """
    available = vehicle.get("availableBy", "").strip()
    if not available:
        return ""
    return available if available[:10] <= captured else ""


def add_vehicle_fields(vehicles: list[dict[str, str]], captured: str) -> Counter:
    """Write the stock date and the six blank contract columns; return how many
    cars are in stock, which is the only number here worth watching."""
    tally: Counter = Counter()
    for vehicle in vehicles:
        vehicle[ENTERED_STOCK] = entered_stock(vehicle, captured)
        for column in VEHICLE_BLANKS:
            vehicle[column] = ""
        tally["in stock" if vehicle[ENTERED_STOCK] else "still inbound"] += 1
    return tally


def add_columns(fields: list[str], columns: tuple[str, ...]) -> list[str]:
    """Append the columns this pass owns, once. Re-running must not duplicate a
    header, and must not reorder the vendor's own columns."""
    return fields + [c for c in columns if c not in fields]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--orders", type=Path, default=DATA / "orders.csv")
    parser.add_argument("--vehicles", type=Path, default=DATA / "vehicles.csv")
    args = parser.parse_args()

    order_fields, orders = read_csv(args.orders)
    vehicle_fields, vehicles = read_csv(args.vehicles)

    number_cards(orders)
    stock = add_vehicle_fields(vehicles, CAPTURED.isoformat())

    write_csv(args.orders, add_columns(order_fields, ORDER_COLUMNS), orders)
    write_csv(args.vehicles, add_columns(vehicle_fields, VEHICLE_COLUMNS), vehicles)

    cards = Counter(o[CARD_NUM] for o in orders)
    sizes = Counter(cards.values())
    print(f"{args.orders}: {len(orders)} lines on {len(cards)} cards")
    print(f"  lines per card: {min(sizes)}-{max(sizes)}, most common {sizes.most_common(1)[0][0]}")
    print(f"  accounts: {len(account_codes(orders))}")
    print(f"{args.vehicles}: {len(vehicles)} cars")
    for label, n in sorted(stock.items()):
        print(f"  {label:14} {n:5}")
    print(f"  in stock = availableBy on or before {CAPTURED}")


if __name__ == "__main__":
    main()

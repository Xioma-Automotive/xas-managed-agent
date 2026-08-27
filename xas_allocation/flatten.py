"""Flatten the mounted pull into the solver snapshot — pure, cheap code.

This runs IN THE SANDBOX, over the two files the host mounted: ``orders.json``
and ``vehicles.json``, written by ``datasource.translate`` out of the export's
``orders.csv`` + ``vehicles.csv``. It is the "flatten + freeze at pull time" step.

The invariant (``plan = pure_function(data_snapshot, …)``) REQUIRES it to be
deterministic code, not model reasoning: if the agent re-derived this mapping each
turn, that is the exact state-leak the whole design guards against. So it lives
here, is O(n), and makes zero model calls.

Input — the two mounted payloads::

    orders.json    {"now": "2026-08-25", "meta": {...}, "orders": [...]}
    vehicles.json  {"vehicles": [...]}

Output — an ``xas_allocation.snapshot.Snapshot``: the ``orders[] / vehicles[] /
allocations{}`` arrays the solver reads.

Field mapping (pull → solver):

  * ``OrderId``      → ``Order.order_id``; one row is one order for ONE car, so
    the id IS the key. No cards, no lines, no ``Quantity``.
  * ``DeliveryDate`` → ``Order.delivery_date`` — the promise, from the ORDER's own
    ``etaDealer`` column. The car's date is a different field entirely.
  * ``SalesModel``   → ``Order.sales_model`` / ``Vehicle.sales_model``;
    eligibility is exact equality between the two.
  * ``EtaDealer``    → ``Vehicle.eta_dealer`` — from the car's ``availableBy``,
    the one field a delay moves.
  * ``VehicleCode`` on an order → ``allocations[key]``, the car it holds today.
  * ``Customer``     → ``Order.customer`` — the client's name, from the order's
    ``customer.name``. A label: nothing in the solver reads it, and an order with
    no name still allocates.

Eligibility arcs are NOT built here — the solver computes them at solve time
(the sparse-arc rule), never stored.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .snapshot import Order, Snapshot, Vehicle, parse_date


def flatten(orders_doc: dict, vehicles_doc: dict) -> Snapshot:
    """The two mounted payloads -> a flattened Snapshot. Pure, deterministic.

    A row missing the field that makes it solvable — an order with no promised
    date, a car with no eligibility key or no arrival date — is SKIPPED and
    counted, never defaulted: a fabricated date would silently move the plan.
    ``datasource.translate`` filters these host-side already, so the counts here
    are a backstop, and they land in ``snapshot.meta`` beside the source's own so
    the reply can account for every row.
    """
    skips: Counter[str] = Counter()

    def skip(reason: str, n: int = 1) -> None:
        """Tally n dropped rows against a reason. n=0 is a no-op, so a caller
        computing a count inline needs no guard around it."""
        if n:
            skips[reason] += n

    orders: list[Order] = []
    allocations: dict[str, str] = {}
    for row in orders_doc["orders"]:
        order_id = str(row.get("OrderId") or "").strip()
        if not order_id:
            skip("order_without_an_id")
            continue
        if not str(row.get("SalesModel") or "").strip():
            skip("order_without_a_model")
            continue
        if not row.get("DeliveryDate"):
            skip("order_without_a_promised_date")
            continue
        order = Order(
            order_id=order_id,
            sales_model=str(row["SalesModel"]).strip(),
            delivery_date=parse_date(row["DeliveryDate"]),
            customer=str(row.get("Customer") or "").strip(),
        )
        orders.append(order)
        held = str(row.get("VehicleCode") or "").strip()
        if held:
            allocations[order.key] = held

    vehicles: list[Vehicle] = []
    for row in vehicles_doc["vehicles"]:
        if not str(row.get("SalesModel") or "").strip():
            skip("vehicle_without_a_model")
            continue
        if not row.get("EtaDealer"):
            skip("vehicle_without_an_arrival_date")
            continue
        vehicles.append(
            Vehicle(
                vehicle_id=str(row["VehicleCode"]),
                sales_model=str(row["SalesModel"]).strip(),
                eta_dealer=parse_date(row["EtaDealer"]),
            )
        )

    # An allocation pointing at a car that did not survive is no allocation.
    vehicle_ids = {u.vehicle_id for u in vehicles}
    for key in [k for k, vid in allocations.items() if vid not in vehicle_ids]:
        del allocations[key]
        skip("allocation_to_a_dropped_vehicle")

    # --- the disruption is DERIVED, and it has to be --------------------------
    # What actually slips is a CAR: a shipment runs late, so its cars arrive late.
    # An order is only affected THROUGH the car allocated to it, and the export
    # records no "this shipment slipped" manifest — the carve scripts bake the
    # slip into `availableBy` — so it is derived here rather than read. The pull
    # carries a copy of the same thing for the tool summary; this is the
    # authoritative version and replaces it, so the solver's free set is exactly
    # the orders whose own car is late.
    eta_of = {u.vehicle_id: u.eta_dealer for u in vehicles}
    promise_of = {o.key: o.delivery_date for o in orders}
    disruption = {
        "disrupted_orders": sorted(
            key for key, vid in allocations.items() if eta_of[vid] > promise_of[key]
        )
    }

    meta = dict(orders_doc.get("meta") or {})
    if skips:
        excluded = dict(meta.get("excluded") or {})
        excluded["flatten_skips"] = dict(sorted(skips.items()))
        meta["excluded"] = excluded

    return Snapshot(
        orders=orders,
        vehicles=vehicles,
        allocations=allocations,
        disruption=disruption,
        now=parse_date(orders_doc["now"]),
        meta=meta,
    )


def flatten_paths(orders_path: str | Path, vehicles_path: str | Path) -> Snapshot:
    """Flatten the two mounted files. These are the paths the host mounted the
    pull at — see ``alloc_tools.ORDERS_MOUNT_PATH`` / ``VEHICLES_MOUNT_PATH``."""
    return flatten(
        json.loads(Path(orders_path).read_text()),
        json.loads(Path(vehicles_path).read_text()),
    )


def main() -> None:
    """``python -m xas_allocation.flatten --orders … --vehicles …`` — flatten the
    mounted pull and write ``snapshot.json``. This is what the pull tool's command
    runs; it takes explicit paths because the platform decides where a mounted
    file lands (see ``alloc_tools.mount_candidates``)."""
    import argparse

    parser = argparse.ArgumentParser(description="Mounted pull -> snapshot.json")
    parser.add_argument("--orders", required=True, help="path to the mounted orders.json")
    parser.add_argument("--vehicles", required=True, help="path to the mounted vehicles.json")
    parser.add_argument("--out", default="snapshot.json")
    args = parser.parse_args()

    snap = flatten_paths(args.orders, args.vehicles)
    Path(args.out).write_text(json.dumps(snap.as_dict(), indent=2, sort_keys=True))
    print(
        f"wrote {args.out}: {len(snap.orders)} orders, {len(snap.vehicles)} vehicles, "
        f"{len(snap.allocations)} allocations; now={snap.now}"
    )
    print(f"to repair: {len(snap.disruption.get('disrupted_orders', []))} orders arrive late")


if __name__ == "__main__":
    main()

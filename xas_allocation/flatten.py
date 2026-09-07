"""Flatten the mounted pull into the solver snapshot — pure, cheap code.

This runs IN THE SANDBOX, over the ONE file the host mounted — the "Allocation
solve contract" document, written by ``datasource.translate`` out of the export's
``orders.csv`` + ``vehicles.csv``. It is the "flatten + freeze at pull time" step.

The invariant (``plan = pure_function(data_snapshot, …)``) REQUIRES it to be
deterministic code, not model reasoning: if the agent re-derived this mapping each
turn, that is the exact state-leak the whole design guards against. So it lives
here, is O(n), and makes zero model calls.

Input — the mounted document::

    dms_allocation.json  {"captured_at": "2026-08-25", "pull_id": "…",
                          "source": "…", "meta": {...},
                          "orders": [...], "vehicles": [...]}

Output — an ``xas_allocation.snapshot.Snapshot``: the ``orders[] / vehicles[] /
allocations{}`` arrays the solver reads.

Field mapping (document → solver):

  * ``DMSJCNum`` + ``LineNum`` → ``Order.job_card`` / ``Order.line``, and together
    ``Order.key`` — the contract's identity for one order line. One line is one
    order for ONE car; there is still no ``Quantity``.
  * ``DMSJCEntry``   → ``Order.entry`` — the DMS's own handle on the card, carried
    because a write-back has to quote it. Nothing here reads it.
  * ``DeliveryDate`` → ``Order.delivery_date`` — the promise, from the ORDER's own
    ``etaDealer`` column. The car's date is a different field entirely.
  * ``SalesModel``   → ``Order.sales_model`` / ``Vehicle.sales_model``;
    eligibility is exact equality between the two.
  * ``AvailableBy``  → ``Vehicle.eta_dealer`` — from the car's ``availableBy``,
    the one field a delay moves.
  * ``VehicleCode`` on an order → ``allocations[key]``, the car it holds today.
    ``null`` means it holds none.
  * ``Accounts.Owner.AccountName`` → ``Order.customer`` and
    ``…AccountDMSCode`` → ``Order.account_code`` — the client, as a label and as
    the account's real key. Nothing in the solver reads either, and an order with
    no account still allocates.
  * ``AllocType``    → ``Order.alloc_type`` — how firmly the customer is
    committed. Carried and shown, NOT priced (see ``solver.break_cost_of``).

Eligibility arcs are NOT built here — the solver computes them at solve time
(the sparse-arc rule), never stored.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .snapshot import Order, Snapshot, Vehicle, parse_date


def flatten(pull: dict) -> Snapshot:
    """The mounted document -> a flattened Snapshot. Pure, deterministic.

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
    for row in pull["orders"]:
        # The DMS sends LineNum as a number and a CSV sends it as a string; both
        # end up the same key because both go through str() here and in
        # `datasource.order_key`. Two spellings of one key match nothing and say
        # nothing about why.
        card = str(row.get("DMSJCNum") or "").strip()
        line = str(row.get("LineNum") or "").strip()
        if not (card and line):
            skip("order_without_a_card_or_line_number")
            continue
        if not str(row.get("SalesModel") or "").strip():
            skip("order_without_a_model")
            continue
        if not row.get("DeliveryDate"):
            skip("order_without_a_promised_date")
            continue
        order = Order(
            job_card=card,
            line=line,
            sales_model=str(row["SalesModel"]).strip(),
            delivery_date=parse_date(row["DeliveryDate"]),
            customer=str(row.get("Accounts.Owner.AccountName") or "").strip(),
            account_code=str(row.get("Accounts.Owner.AccountDMSCode") or "").strip(),
            alloc_type=str(row.get("AllocType") or "").strip(),
            entry=str(row.get("DMSJCEntry") or "").strip(),
        )
        orders.append(order)
        held = str(row.get("VehicleCode") or "").strip()
        if held:
            allocations[order.key] = held

    vehicles: list[Vehicle] = []
    for row in pull["vehicles"]:
        if not str(row.get("SalesModel") or "").strip():
            skip("vehicle_without_a_model")
            continue
        if not row.get("AvailableBy"):
            skip("vehicle_without_an_arrival_date")
            continue
        vehicles.append(
            Vehicle(
                vehicle_id=str(row["VehicleCode"]),
                sales_model=str(row["SalesModel"]).strip(),
                eta_dealer=parse_date(row["AvailableBy"]),
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

    meta = dict(pull.get("meta") or {})
    # The contract's three header fields ride into the snapshot so the sandbox can
    # say which pull an answer was built on without re-reading the file.
    for field_name in ("captured_at", "pull_id", "source"):
        if pull.get(field_name):
            meta[field_name] = pull[field_name]
    if skips:
        excluded = dict(meta.get("excluded") or {})
        excluded["flatten_skips"] = dict(sorted(skips.items()))
        meta["excluded"] = excluded

    return Snapshot(
        orders=orders,
        vehicles=vehicles,
        allocations=allocations,
        disruption=disruption,
        now=parse_date(pull["captured_at"]),
        meta=meta,
    )


def flatten_path(pull_path: str | Path) -> Snapshot:
    """Flatten the mounted document. This is the path the host mounted the pull
    at — see ``alloc_tools.PULL_MOUNT_PATH`` and the ``file`` the tool returns."""
    return flatten(json.loads(Path(pull_path).read_text()))


def main() -> None:
    """``python -m xas_allocation.flatten --pull …`` — flatten the mounted pull and
    write ``snapshot.json``. This is what the pull tool's command runs; it takes an
    explicit path because the platform decides where a mounted file lands (see
    ``alloc_tools.mount_candidates``)."""
    import argparse

    parser = argparse.ArgumentParser(description="Mounted pull -> snapshot.json")
    parser.add_argument("--pull", required=True, help="path to the mounted document")
    parser.add_argument("--out", default="snapshot.json")
    args = parser.parse_args()

    snap = flatten_path(args.pull)
    Path(args.out).write_text(json.dumps(snap.as_dict(), indent=2, sort_keys=True))
    print(
        f"wrote {args.out}: {len(snap.orders)} orders, {len(snap.vehicles)} vehicles, "
        f"{len(snap.allocations)} allocations; now={snap.now}"
    )
    print(f"to repair: {len(snap.disruption.get('disrupted_orders', []))} orders arrive late")


if __name__ == "__main__":
    main()

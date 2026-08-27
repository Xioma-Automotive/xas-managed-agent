"""Flatten the rich XAS pull into the solver snapshot — pure, cheap code.

This is the "flatten + freeze at pull time" step. The invariant
(`plan = pure_function(data_snapshot, …)`) REQUIRES it to be deterministic code,
not model reasoning: if the agent re-derived this mapping each turn, that is the
exact state-leak the whole design guards against. So it lives here, is O(n), and
makes zero model calls — the old fuzzy spec-match residual is gone.

Input: the rich XAS-shaped pull ``{meta, vsos, vehicles, disruption}`` the data
source returns (VSO jobcards + a flat vehicle pool + a disruption manifest).
Output: an ``xas_allocation.snapshot.Snapshot`` — the ``orders[] / units[] /
incumbent[]`` arrays the solver reads, in one shared vocabulary with the API.

Field mapping (real XAS → solver):
  * order = one car line, ONE CAR; key = ``{JobKey}-{LineNum}``. A jobitem's
    ``Quantity`` is not read — one car per line is the current assumption, since
    a line resolves to at most one vehicle code (see the module docstring in
    ``snapshot.py``).
  * ``DeliveryDate`` (VSO header) → ``Order.delivery_date`` (the promise).
  * ``JobPriority`` is NOT read. Priority is a per-turn planner lever now
    (``solver._combined_priority``), not a letter on the record — see the
    ``Order`` docstring in ``snapshot.py``.
  * ``AvailableBy``, else ``EtaDealer`` (vehicle) → ``Unit.eta_dealer`` (the
    mutable delivery date).
  * ``VehicleClassification`` (``Vehicle``/``Future``) → ``Unit.vehicle_classification``.
  * eligibility: jobitem ``SalesModelCode`` == vehicle ``SalesModel`` — the full
    trim/colour code (``T5040UECLMQ0009``), NOT ``ModelId.Code`` (``T5040``),
    which is the model and matches no order. ``ModelId.Code`` is kept only as a
    fallback for a vehicle that has no ``SalesModel``.
  * incumbent (current allocation): HARD via jobitem ``VehicleId.Code`` ↔
    ``VehicleCode``; SOFT via the jobitem's Alloc link to a Future vehicle (in the
    mock, resolved straight to that vehicle's code — see ``_incumbent_of``). It
    covers ONE car of the line: a line resolves to a single vehicle code, and one
    car cannot serve two orders. A line claiming ``AllocQty > 1`` therefore has
    committed cars this pull cannot identify — counted, never invented.

Eligibility arcs are NOT built here — the solver computes them at solve time
(the sparse-arc rule), never stored.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .snapshot import Order, Snapshot, Unit, parse_date

# The bundled dataset. Relative to this file so it resolves both in the repo
# (repo/data/pull.json) and in the skill bundle (<skill>/data/pull.json).
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "pull.json"


def _incumbent_of(item: dict) -> str | None:
    """The vehicle a VSO jobitem is currently allocated to, or None.

    HARD side: ``VehicleId.Code`` points at a concrete vehicle
    (``VehicleId.Code`` ↔ ``VehicleCode``); present iff the row is hard-allocated.
    SOFT side: the Alloc block points at a future vehicle. The real API resolves
    that through ``AllocSource*``; the mock links straight to the Future vehicle it
    stands for via ``AllocatedVehicleCode``."""
    vehicle_id = item.get("VehicleId") or {}
    if vehicle_id.get("Code"):
        return str(vehicle_id["Code"])
    if item.get("AllocatedVehicleCode"):
        return str(item["AllocatedVehicleCode"])
    return None


def _unit_model(vehicle: dict) -> str:
    """A vehicle's eligibility key: ``SalesModel``, else ``ModelId.Code``.

    An order names a full trim/colour code, which is what ``SalesModel`` holds;
    ``ModelId.Code`` is the model above it and matches no real order. The
    fallback exists only so a model-coded order can still find a car — it is the
    same hard equality either way, never a fuzzy match."""
    sales_model = vehicle.get("SalesModel") or ""
    return str(sales_model).strip() or str((vehicle.get("ModelId") or {}).get("Code") or "").strip()


def flatten(rich: dict) -> Snapshot:
    """Rich XAS pull -> flattened Snapshot. Pure, deterministic.

    Explodes each VSO into its jobitem car lines (the allocatable orders) and
    reads the flat vehicle pool into ``units``. Incumbent comes from each
    jobitem's current allocation link (hard ``VehicleId`` or soft Alloc).

    A row missing the field that makes it solvable — a VSO with no promised date,
    a vehicle with no eligibility key or no arrival date — is SKIPPED and counted,
    never defaulted: a fabricated date would silently move the plan. The real
    source filters these out host-side already (``datasource.map_response``), so
    the counts here are a backstop, and they land in ``snapshot.meta`` beside the
    source's own so the reply can account for every row.
    """
    skips: Counter[str] = Counter()

    def skip(reason: str, n: int = 1) -> None:
        """Tally n dropped rows against a reason. n=0 is a no-op, so a caller
        computing a count inline needs no guard around it."""
        if n:
            skips[reason] += n

    orders: list[Order] = []
    incumbent: dict[str, str] = {}
    for vso in rich["vsos"]:
        so_id = str(vso.get("JobKey") or vso.get("DMSJCEntry"))
        owner = (vso.get("Accounts") or {}).get("Owner") or {}
        customer = owner.get("AccountName", "")
        customer_id = owner.get("AccountUUID", "")
        if not vso.get("DeliveryDate"):
            skip("order_without_a_promised_date")
            continue
        delivery_date = parse_date(vso["DeliveryDate"])
        for item in vso.get("JobItems", []):
            if not str(item.get("SalesModelCode") or "").strip():
                skip("order_line_without_a_model")
                continue
            line = int(item["LineNum"])
            # ONE CAR PER LINE. The jobitem's `Quantity` is NOT read: a line
            # resolves to at most one vehicle code, so a car beyond the first
            # could never be linked to anything anyway. Assumed 2026-08-25,
            # pending a response-shape decision (one cap per line, or per-car
            # fields) — see `docs/mcp-response-schema.md`.
            price = sum(float(p.get("GrossTotal", 0.0)) for p in item.get("Prices", []))
            order = Order(
                so_id=so_id,
                line=line,
                customer=customer,
                customer_id=customer_id,
                sales_model=item["SalesModelCode"],
                delivery_date=delivery_date,
                price=price,
            )
            orders.append(order)
            inc = _incumbent_of(item)
            if inc:
                incumbent[order.key] = inc

    units: list[Unit] = []
    for v in rich["vehicles"]:
        model = _unit_model(v)
        if not model:
            skip("vehicle_without_a_model")
            continue
        if not v.get("EtaDealer"):
            skip("vehicle_without_an_arrival_date")
            continue
        units.append(
            Unit(
                vehicle_id=str(v["VehicleCode"]),
                vehicle_classification=v["VehicleClassification"],
                sales_model=model,
                eta_dealer=parse_date(v["EtaDealer"]),
            )
        )

    # An incumbent pointing at a vehicle that did not survive is no incumbent.
    unit_ids = {u.vehicle_id for u in units}
    for key in [k for k, uid in incumbent.items() if uid not in unit_ids]:
        del incumbent[key]
        skip("allocation_to_a_dropped_vehicle")

    # --- the disruption is DERIVED, and it has to be --------------------------
    # What actually slips is a VEHICLE: a shipment (VPO/VGR) runs late, so its
    # cars arrive late. An order is only affected THROUGH the vehicle allocated to
    # it, and XAS records no "this shipment slipped" manifest, so it is derived
    # here rather than read. The rich pull carries a preview of the same thing;
    # this is the authoritative version and replaces it, so the solver's free set
    # is exactly the orders whose own vehicle is late.
    eta_of = {u.vehicle_id: u.eta_dealer for u in units}
    promise_of = {o.key: o.delivery_date for o in orders}
    disruption = dict(rich.get("disruption") or {})
    disruption["disrupted_orders"] = sorted(
        key for key, uid in incumbent.items() if eta_of[uid] > promise_of[key]
    )

    meta = dict(rich.get("meta") or {})
    if skips:
        excluded = dict(meta.get("excluded") or {})
        excluded["flatten_skips"] = dict(sorted(skips.items()))
        meta["excluded"] = excluded

    return Snapshot(
        orders=orders,
        units=units,
        incumbent=incumbent,
        disruption=disruption,
        now=parse_date(rich["meta"]["now"]),
        meta=meta,
    )


def flatten_path(src: str | Path) -> Snapshot:
    """Flatten the rich pull at ``src`` — the path the host mounts the live pull
    into the sandbox at (see ``alloc_tools.flatten_command`` / ``MOUNT_PATH``)."""
    return flatten(json.loads(Path(src).read_text()))


def flatten_default() -> Snapshot:
    """Flatten the repo dataset — host-side dev/tests only. The live pull comes
    through ``flatten_path`` instead."""
    return flatten_path(DATA_PATH)


if __name__ == "__main__":
    snap = flatten_default()
    print(
        f"flattened: {len(snap.orders)} orders, {len(snap.units)} units, "
        f"{len(snap.incumbent)} allocations; now={snap.now}"
    )
    d = snap.disruption
    print(
        f"disruption: {d.get('delay_label') or str(d.get('delay_days')) + ' days'} on "
        f"{len(d.get('delayed_vehicles', []))} vehicles, "
        f"{len(d.get('disrupted_orders', []))} orders to repair"
    )

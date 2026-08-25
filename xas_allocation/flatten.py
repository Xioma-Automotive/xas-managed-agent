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
  * order = one wanted **car**; key = ``{JobKey}-{LineNum}-{n}``. A jobitem's
    ``Quantity`` is EXPANDED here: a line wanting 3 cars becomes 3 orders, each a
    capacity-1 demand node. ``AllocQty`` says how many of them the line's existing
    allocation already covers; the rest are unallocated demand.
  * ``DeliveryDate`` (VSO header) → ``Order.delivery_date`` (the promise).
  * ``EtaDealer`` (vehicle) → ``Unit.eta_dealer`` (the mutable delivery date).
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
        priority = (vso.get("JobPriority") or {}).get("Code", "C")
        if not vso.get("DeliveryDate"):
            skip("order_without_a_promised_date")
            continue
        delivery_date = parse_date(vso["DeliveryDate"])
        for item in vso.get("JobItems", []):
            if not str(item.get("SalesModelCode") or "").strip():
                skip("order_line_without_a_model")
                continue
            line = int(item["LineNum"])
            # QTY EXPANSION: a line wanting 3 cars is 3 orders. The solver models
            # one order as one capacity-1 demand node, so this is the only place
            # quantity has to be understood — but it MUST be, or two of those
            # three cars silently vanish from the plan.
            quantity = max(1, int(item.get("Quantity") or 1))
            # Line total / qty. Display-only either way (never a cost-model
            # input), but a per-car row showing the whole line's value would read
            # as three times the money.
            price = sum(float(p.get("GrossTotal", 0.0)) for p in item.get("Prices", [])) / quantity
            # How many of this line's cars already have a car. `AllocQty` SAYS how
            # many are committed — but a line resolves to at most ONE vehicle
            # code, and one car cannot satisfy two orders: handing it to k of them
            # would double-book it and trip the solver's self-check on its own
            # input. So coverage is 1, and any claim above that is recorded rather
            # than guessed at (`allocation_qty_not_resolvable_to_cars`) — it is the
            # same projection gap as the rest of Q1 in the schema doc, since the
            # remaining committed cars live on a VPO the pull never hops to. The
            # uncovered cars arrive unallocated, which `solver.partition` already
            # frees as demand needing a car.
            inc = _incumbent_of(item)
            claimed = max(0, int(item.get("AllocQty") or 1)) if inc else 0
            covered = min(1, claimed, quantity)
            skip("allocation_qty_not_resolvable_to_cars", min(claimed, quantity) - covered)
            for n in range(1, quantity + 1):
                order = Order(
                    so_id=so_id,
                    line=line,
                    qty_index=n,
                    customer=customer,
                    customer_id=customer_id,
                    sales_model=item["SalesModelCode"],
                    priority=priority,
                    delivery_date=delivery_date,
                    price=price,
                    # Solver escalation fields; the real-data derivation is a TODO
                    # (see scenario_engine). Absent => 0.
                    n_prior_delays=int(item.get("n_prior_delays", 0)),
                    days_backordered=int(item.get("days_backordered", 0)),
                    times_rescheduled=int(item.get("times_rescheduled", 0)),
                )
                orders.append(order)
                if inc and n <= covered:
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

    # --- the disruption is derived PER CAR, and it has to be ------------------
    # What actually slips is a VEHICLE: a shipment (VPO/VGR) runs late, so its
    # cars arrive late. A VSO line is only affected THROUGH the vehicle allocated
    # to it — and the cars of one line can be satisfied from different shipments,
    # so one of them slipping says nothing about the others. Deriving this at line
    # grain would free every car of the line whenever any one of its vehicles
    # slipped, handing the solver work it was never asked to do.
    #
    # The rich pull carries a line-grained preview of the same thing (it has not
    # expanded `Quantity` yet); this is the authoritative version and it replaces
    # it, so the solver's free set is exactly the cars whose own vehicle is late.
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
        f"disruption: +{d.get('delay_days')}d on {len(d.get('delayed_vehicles', []))} vehicles, "
        f"{len(d.get('disrupted_orders', []))} orders to repair"
    )

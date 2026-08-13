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
  * order = one VSO **jobitem** (car line); key = ``{JobKey}-{LineNum}``.
  * ``DeliveryDate`` (VSO header) → ``Order.delivery_date`` (the promise).
  * ``EtaDealer`` (vehicle) → ``Unit.eta_dealer`` (the mutable delivery date).
  * ``VehicleClassification`` (``Vehicle``/``Future``) → ``Unit.vehicle_classification``.
  * eligibility: jobitem ``SalesModelCode`` == vehicle ``ModelId.Code`` (model-level).
  * incumbent (current allocation): HARD via jobitem ``VehicleId.Code`` ↔
    ``VehicleCode``; SOFT via the jobitem's Alloc link to a Future vehicle (in the
    mock, resolved straight to that vehicle's code — see ``_incumbent_of``).

Eligibility arcs are NOT built here — the solver computes them at solve time
(the sparse-arc rule), never stored.
"""

from __future__ import annotations

import json
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


def flatten(rich: dict) -> Snapshot:
    """Rich XAS pull -> flattened Snapshot. Pure, deterministic.

    Explodes each VSO into its jobitem car lines (the allocatable orders) and
    reads the flat vehicle pool into ``units``. Incumbent comes from each
    jobitem's current allocation link (hard ``VehicleId`` or soft Alloc)."""
    orders: list[Order] = []
    incumbent: dict[str, str] = {}
    for vso in rich["vsos"]:
        so_id = str(vso.get("JobKey") or vso.get("DMSJCEntry"))
        owner = (vso.get("Accounts") or {}).get("Owner") or {}
        customer = owner.get("AccountName", "")
        customer_id = owner.get("AccountUUID", "")
        priority = (vso.get("JobPriority") or {}).get("Code", "C")
        delivery_date = parse_date(vso["DeliveryDate"])
        for item in vso.get("JobItems", []):
            line = int(item["LineNum"])
            price = sum(float(p.get("GrossTotal", 0.0)) for p in item.get("Prices", []))
            order = Order(
                so_id=so_id,
                line=line,
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
            inc = _incumbent_of(item)
            if inc:
                incumbent[order.key] = inc

    units = [
        Unit(
            vehicle_id=str(v["VehicleCode"]),
            vehicle_classification=v["VehicleClassification"],
            sales_model=(v.get("ModelId") or {})["Code"],
            eta_dealer=parse_date(v["EtaDealer"]),
        )
        for v in rich["vehicles"]
    ]

    return Snapshot(
        orders=orders,
        units=units,
        incumbent=incumbent,
        disruption=rich.get("disruption", {}),
        now=parse_date(rich["meta"]["now"]),
    )


def load_rich(path: str | Path = DATA_PATH) -> dict:
    return json.loads(Path(path).read_text())


def flatten_default() -> Snapshot:
    """Flatten the repo dataset — host-side dev/tests. The bundled data path is
    kept for offline use; the live pull is read via ``flatten_path`` instead."""
    return flatten(load_rich())


def flatten_path(src: str | Path) -> Snapshot:
    """Flatten the rich pull at ``src`` — the path the host mounts the live pull
    into the sandbox at (see ``alloc_tools.flatten_command`` / ``MOUNT_PATH``)."""
    return flatten(load_rich(src))


def flatten_file(out: str | Path, src: str | Path = DATA_PATH) -> Path:
    """Flatten ``src`` and write the snapshot to ``out`` (for inspection)."""
    out = Path(out)
    snap = flatten(load_rich(src))
    out.write_text(json.dumps(snap.as_dict(), indent=2, sort_keys=True))
    return out


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

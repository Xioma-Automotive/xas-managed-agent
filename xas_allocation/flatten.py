"""Flatten the rich relational pull into the solver snapshot — pure, cheap code.

This is the "flatten + freeze at pull time" step. The invariant
(`plan = pure_function(data_snapshot, …)`) REQUIRES it to be deterministic code,
not model reasoning: if the agent re-derived this mapping each turn, that is the
exact state-leak the whole design guards against. So it lives here, is O(n), and
makes zero model calls — the old fuzzy spec-match residual is gone.

Input: the rich dataset `scenario_engine/` emits (PDN / Vehicle / SO rows +
a disruption manifest). Output: an `xas_allocation.snapshot.Snapshot` — the
`orders[] / units[] / incumbent[]` arrays the solver reads.

Eligibility arcs are NOT built here — the solver computes them at solve time
(the sparse-arc rule), never stored.

The dataset ships INSIDE the skill bundle (like the solver package itself), so
the sandbox reads its own copy; ``flatten_default()`` locates it relative to
this file and the agent's pull hands back a one-liner that calls it.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import decisions as D
from .snapshot import Order, Snapshot, Unit, parse_date

# The bundled dataset. Relative to this file so it resolves both in the repo
# (repo/data/pull.json) and in the skill bundle (<skill>/data/pull.json).
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "pull.json"


def _committed(location_state: str) -> bool:
    """DECIDE-3: is a vehicle at this pipeline stage physically committed?"""
    return location_state in D.COMMIT_POINT_STATES


def flatten(rich: dict) -> Snapshot:
    """Rich relational pull -> flattened Snapshot. Pure, deterministic.

    Explodes each Sales Order into its vehicle order rows (the allocatable
    orders) and unions the supply (concrete vehicles + PO-line slots) into
    ``units``. Incumbent comes from each row's current allocation link."""
    orders: list[Order] = []
    incumbent: dict[str, str] = {}
    for so in rich["sos"]:
        for row in so["rows"]:
            orders.append(
                Order.from_dict(
                    {
                        "order_id": row["row_id"],
                        "so_id": so["so_id"],
                        "customer": so["customer"],
                        "customer_id": so["customer_id"],
                        "sales_model": row["sales_model"],
                        "priority": row["priority"],
                        "promised_date": row["promised_date"],
                        "eta_date": row["eta_date"],
                        "price": row.get("price", 0.0),
                        "n_prior_delays": row.get("n_prior_delays", 0),
                        "days_backordered": row.get("days_backordered", 0),
                        "times_rescheduled": row.get("times_rescheduled", 0),
                    }
                )
            )
            if row.get("current_supply_id"):
                incumbent[str(row["row_id"])] = str(row["current_supply_id"])

    units = [
        Unit(
            vehicle_id=str(s["supply_id"]),
            kind=s.get("kind", "vehicle"),
            sales_model=s["sales_model"],
            planned_delivery_date=parse_date(s["planned_delivery_date"]),
            location_state=s["location_state"],
            po_ref=s.get("po_ref", ""),
            pdn=s.get("pdn", ""),
            committed=_committed(s["location_state"]),
        )
        for s in rich["supply"]
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
        f"disruption: PO {d.get('po')} +{d.get('delay_days')}d, "
        f"{len(d.get('disrupted_orders', []))} orders to repair"
    )

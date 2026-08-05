"""Fabricate a rich allocation scenario: good world -> introduce a PO delay.

Deterministic: everything derives from one integer ``seed`` via ``random.Random``
and a FIXED base date — no wall-clock, no module-level randomness — so a given
seed regenerates a byte-identical dataset (the determinism the whole design
rests on, upheld on the supply side of the boundary now).

Model (v2, the proposed DECIDE-7 contract, minus explicit PDN tables):
  PO           orders cars from a supplier: po_id, sales_model, quantity
  Vehicle      a physical car (a VIN), from a PO via a PDN batch
  PO-line slot a *future* car of a PO line, keyed PO-model-row (e.g. PO-150-1-5)
  Sales Order  one customer, groups vehicle order ROWS
  vehicle row  the allocatable demand unit; allocated to a Vehicle OR a slot

Supply is the union {vehicles, PO-line slots}. A row is on-time in the good
world; the disruption delays one PO, slipping planned_delivery_date on every
supply item under it (slots and vehicles alike), which breaks the rows riding
them — the repair the agent performs.

Output (JSON, real dates ``YYYY-MM-DD``):
  data/baseline.json  — the good, on-time world (reference / diffing)
  data/pull.json      — the SAME world after one PO is delayed (the pull target)
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# The pull date / front of the horizon. A constant, never today() — determinism.
BASE_DATE = date(2026, 8, 3)  # a Monday
HORIZON_WEEKS = 13

SALES_MODELS = ("SM1", "SM2", "SM3", "SM4", "SM5")

# 30 customers: six named dealers (so "prefer Colmobil" has a real target) then
# generated ones. Priority cycles A,A,B,B,C,C — Colmobil/Delek land on A.
NAMED = ("Colmobil", "Delek Motors", "Champion", "Talcar", "Carasso", "Lubinski")
PRIORITY_CYCLE = ("A", "A", "B", "B", "C", "C")

# Vehicle pipeline stages (early -> late). bonded/pdi are "committed" (DECIDE-3).
COMMITTED_STATES = frozenset({"bonded", "pdi"})

PER_PO = 8  # supply items (rows) per purchase order
FIRST_PO = 150  # PO numbering starts here, so refs read like PO-150-1-5


def _iso(d: date) -> str:
    return d.isoformat()


def _customers(n: int) -> list[tuple[str, str, str]]:
    """(name, customer_id, priority) for n customers, deterministically."""
    out: list[tuple[str, str, str]] = []
    for i in range(n):
        name = NAMED[i] if i < len(NAMED) else f"Dealer {i + 1:02d}"
        out.append((name, f"CUST-{i + 1:03d}", PRIORITY_CYCLE[i % len(PRIORITY_CYCLE)]))
    return out


def _location_for(rng: random.Random, planned: date) -> str:
    """Warmer (committed) the nearer the delivery date; cooler further out."""
    days_out = (planned - BASE_DATE).days
    if days_out <= 7:
        return rng.choices(("pdi", "bonded", "transfer"), weights=[40, 40, 20])[0]
    if days_out <= 21:
        return rng.choices(("bonded", "transfer", "port"), weights=[20, 40, 40])[0]
    if days_out <= 42:
        return rng.choices(("port", "sea"), weights=[40, 60])[0]
    return rng.choices(("sea", "future"), weights=[40, 60])[0]


def _make_supply(rng, model, planned, po_id, row_in_po, uid_box, prefix="PO"):
    """Build one supply item for a demand slot: a concrete Vehicle or a PO-line
    slot. Returns (supply_dict, supply_id, po_id)."""
    po_ref = f"{po_id}-1-{row_in_po}"
    kind = rng.choices(("vehicle", "po_line"), weights=[60, 40])[0]
    if kind == "vehicle":
        uid_box[0] += 1
        supply_id = f"VEH-{uid_box[0]}"
        item = {
            "supply_id": supply_id,
            "kind": "vehicle",
            "sales_model": model,
            "planned_delivery_date": _iso(planned),
            "location_state": _location_for(rng, planned),
            "po_ref": po_ref,
            "pdn": f"PDN-{po_id.split('-')[-1]}",
        }
    else:
        supply_id = po_ref  # a slot is identified by its PO line
        item = {
            "supply_id": supply_id,
            "kind": "po_line",
            "sales_model": model,
            "planned_delivery_date": _iso(planned),
            "location_state": "future",
            "po_ref": po_ref,
            "pdn": "",
        }
    return item, supply_id


def generate(
    seed: int = 20,
    n_customers: int = 30,
    n_orders: int = 40,
    spare_ratio: float = 0.4,
    delay_days: int = 21,
) -> dict[str, dict]:
    """Build the good world and its disrupted twin. Returns {'baseline', 'pull'}."""
    rng = random.Random(seed)
    weeks = [BASE_DATE + timedelta(weeks=k) for k in range(HORIZON_WEEKS)]
    promise_window = weeks[: HORIZON_WEEKS - 2]  # leave slack so lateness is possible
    customers = _customers(n_customers)

    sos: list[dict] = []
    supply: list[dict] = []
    po_members: dict[str, list[str]] = {}
    incumbent: list[tuple[str, str]] = []  # (row_id, supply_id) for disruption logic
    uid_box = [9000]
    slot_index = 0
    so_num = 4000

    def next_po_slot() -> tuple[str, int]:
        nonlocal slot_index
        po_id = f"PO-{FIRST_PO + slot_index // PER_PO}"
        row_in_po = slot_index % PER_PO + 1
        slot_index += 1
        return po_id, row_in_po

    # --- Demand: Sales Orders, each with 1-3 vehicle order rows; one on-time
    #     supply item built per row (Vehicle or PO-line slot). ----------------
    n_rows = 0
    while n_rows < n_orders:
        name, cid, prio = rng.choice(customers)
        so_id = f"SO-{so_num}"
        so_num += 1
        rows: list[dict] = []
        for r in range(1, rng.choices([1, 2, 3], weights=[55, 30, 15])[0] + 1):
            if n_rows >= n_orders:
                break
            model = rng.choice(SALES_MODELS)
            promised = rng.choice(promise_window)
            row_id = f"{so_id}-{r}"
            po_id, row_in_po = next_po_slot()
            item, supply_id = _make_supply(rng, model, promised, po_id, row_in_po, uid_box)
            supply.append(item)
            po_members.setdefault(po_id, []).append(supply_id)
            incumbent.append((row_id, supply_id))
            rows.append(
                {
                    "row_id": row_id,
                    "sales_model": model,
                    "priority": prio,
                    "promised_date": _iso(promised),
                    "eta_date": _iso(promised),  # good world: ETA == promise (on time)
                    "price": rng.choice([32000, 38000, 45000, 52000, 61000]),
                    "n_prior_delays": rng.choices([0, 1, 2, 3], weights=[70, 18, 8, 4])[0],
                    "days_backordered": rng.choices(
                        [0, 0, 0, 7, 14, 30], weights=[50, 15, 10, 12, 8, 5]
                    )[0],
                    # Reschedules our repair loop caused in prior cycles (DECIDE-11).
                    "times_rescheduled": rng.choices([0, 1, 2], weights=[75, 18, 7])[0],
                    "current_supply_id": supply_id,
                }
            )
            n_rows += 1
        sos.append({"so_id": so_id, "customer": name, "customer_id": cid, "rows": rows})

    incumbent_map = dict(incumbent)

    # --- Spare (unallocated) supply — the wiggle room a repair uses. ---------
    for _ in range(int(n_orders * spare_ratio)):
        model = rng.choice(SALES_MODELS)
        planned = rng.choice(weeks)
        po_id = f"PO-SPARE-{slot_index // PER_PO}"
        row_in_po = slot_index % PER_PO + 1
        slot_index += 1
        item, supply_id = _make_supply(rng, model, planned, po_id, row_in_po, uid_box)
        supply.append(item)
        po_members.setdefault(po_id, []).append(supply_id)

    supply_by_id = {s["supply_id"]: s for s in supply}

    def pos(delayed_po: str | None) -> list[dict]:
        recs = []
        for po_id, members in sorted(po_members.items()):
            recs.append(
                {
                    "po_id": po_id,
                    "sales_model": supply_by_id[members[0]]["sales_model"],
                    "quantity": len(members),
                    "delayed_days": delay_days if po_id == delayed_po else 0,
                }
            )
        return recs

    def dataset(state: str, delayed_po: str | None, sup: list[dict], disruption: dict) -> dict:
        return {
            "meta": {
                "seed": seed,
                "now": _iso(BASE_DATE),
                "state": state,
                "horizon_weeks": HORIZON_WEEKS,
                "sales_models": list(SALES_MODELS),
                "n_customers": n_customers,
            },
            "pos": pos(delayed_po),
            "supply": sup,
            "sos": sos,
            "disruption": disruption,
        }

    baseline = dataset("good", None, supply, {})

    # --- Disruption: delay the lowest-id incumbent-carrying PO whose items are
    #     all still movable (not committed), so the repair is meaningful. -----
    incumbent_pos = sorted(
        {po for po in po_members if any(m in incumbent_map.values() for m in po_members[po])}
    )
    delayed_po = None
    for po_id in incumbent_pos:
        if all(
            supply_by_id[m]["location_state"] not in COMMITTED_STATES for m in po_members[po_id]
        ):
            delayed_po = po_id
            break
    if delayed_po is None:  # fallback: any incumbent PO with a movable item
        for po_id in incumbent_pos:
            if any(
                supply_by_id[m]["location_state"] not in COMMITTED_STATES for m in po_members[po_id]
            ):
                delayed_po = po_id
                break

    delayed_ids = set(po_members[delayed_po])
    disrupted_supply: list[dict] = []
    for s in supply:
        if s["supply_id"] in delayed_ids:
            new_date = date.fromisoformat(s["planned_delivery_date"]) + timedelta(days=delay_days)
            disrupted_supply.append({**s, "planned_delivery_date": _iso(new_date)})
        else:
            disrupted_supply.append(dict(s))

    disrupted_orders = sorted(rid for rid, sid in incumbent if sid in delayed_ids)
    disruption = {
        "po": delayed_po,
        "delay_days": delay_days,
        "delayed_supply": sorted(delayed_ids),
        "disrupted_orders": disrupted_orders,
    }
    pull = dataset("disrupted", delayed_po, disrupted_supply, disruption)
    return {"baseline": baseline, "pull": pull}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fabricate an XAS allocation scenario.")
    ap.add_argument("--seed", type=int, default=20)
    ap.add_argument("--customers", type=int, default=30)
    ap.add_argument("--orders", type=int, default=40, help="vehicle order rows (demand)")
    ap.add_argument("--spare-ratio", type=float, default=0.4)
    ap.add_argument("--delay-days", type=int, default=21)
    ap.add_argument("--out", type=Path, default=DATA_DIR, help="output directory")
    args = ap.parse_args()

    result = generate(
        seed=args.seed,
        n_customers=args.customers,
        n_orders=args.orders,
        spare_ratio=args.spare_ratio,
        delay_days=args.delay_days,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in result.items():
        path = args.out / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
        d = data["disruption"]
        kinds = {}
        for s in data["supply"]:
            kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        tag = (
            f"disruption PO {d['po']} +{d['delay_days']}d, {len(d['disrupted_orders'])} rows freed"
            if d
            else "no disruption"
        )
        rows = sum(len(so["rows"]) for so in data["sos"])
        print(
            f"wrote {path}  ({len(data['sos'])} SOs / {rows} rows, "
            f"{kinds.get('vehicle', 0)} vehicles + {kinds.get('po_line', 0)} slots; {tag})"
        )


if __name__ == "__main__":
    main()

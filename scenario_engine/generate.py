"""Fabricate a rich allocation scenario: good world -> introduce a PDN delay.

Deterministic: everything derives from one integer ``seed`` via ``random.Random``
and a FIXED base date — no wall-clock, no module-level randomness — so a given
seed regenerates a byte-identical dataset (the determinism the whole design
rests on, upheld on the supply side of the boundary now).

Output (JSON, real dates ``YYYY-MM-DD``):
  data/baseline.json  — the good, on-time world (reference / diffing)
  data/pull.json      — the SAME world after one PDN is delayed (the pull target)

Emitted shape (the proposed DECIDE-7 contract, minus PO):
  pdn      : pdn_id, sales_model, quantity, delayed_days
  vehicle  : vehicle_id, pdn_id, sales_model, planned_delivery_date, location_state
  so line  : order_id, customer, customer_id, sales_model, priority,
             promised_date, eta_date, price, n_prior_delays, days_backordered,
             current_vehicle_id
  disruption: pdn, delay_days, delayed_vehicles[], disrupted_orders[]
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
# generated ones. Priority cycles A,A,B,B,C,C — Colmobil/Delek land on A, as in
# the earlier week-based data.
NAMED = ("Colmobil", "Delek Motors", "Champion", "Talcar", "Carasso", "Lubinski")
PRIORITY_CYCLE = ("A", "A", "B", "B", "C", "C")

# Vehicle pipeline stages (early -> late). bonded/pdi are "committed" (DECIDE-3).
LOCATION_PIPELINE = ("future", "sea", "port", "transfer", "bonded", "pdi")
COMMITTED_STATES = frozenset({"bonded", "pdi"})

PER_PDN = 8  # vehicles per delivery note


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

    # --- Demand: SO lines --------------------------------------------------
    sos: list[dict] = []
    order_specs: list[tuple[str, str, str, str, date]] = []  # id, cid, cust, model, promised
    for i in range(n_orders):
        name, cid, prio = rng.choice(customers)
        model = rng.choice(SALES_MODELS)
        promised = rng.choice(promise_window)
        order_id = f"SO-{4000 + i}"
        order_specs.append((order_id, cid, name, model, promised))
        sos.append(
            {
                "order_id": order_id,
                "customer": name,
                "customer_id": cid,
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
                # Mostly none; some dealers already bumped once or twice.
                "times_rescheduled": rng.choices([0, 1, 2], weights=[75, 18, 7])[0],
                "current_vehicle_id": "",  # filled after we build the incumbent vehicle
            }
        )

    # --- Supply: one on-time vehicle per SO, grouped into PDNs of ~8 --------
    vehicles: list[dict] = []
    pdn_members: dict[str, list[str]] = {}
    uid = 9000
    for idx, (order_id, _cid, _cust, model, promised) in enumerate(order_specs):
        vehicle_id = f"VEH-{uid}"
        pdn_id = f"PDN-{idx // PER_PDN:03d}"
        vehicles.append(
            {
                "vehicle_id": vehicle_id,
                "pdn_id": pdn_id,
                "sales_model": model,
                "planned_delivery_date": _iso(promised),
                "location_state": _location_for(rng, promised),
            }
        )
        pdn_members.setdefault(pdn_id, []).append(vehicle_id)
        sos[idx]["current_vehicle_id"] = vehicle_id
        uid += 1

    # --- Spare (unallocated) vehicles — the wiggle room a repair uses -------
    n_spares = int(n_orders * spare_ratio)
    for s in range(n_spares):
        model = rng.choice(SALES_MODELS)
        planned = rng.choice(weeks)
        pdn_id = f"PDN-SPARE-{s // PER_PDN:03d}"
        vehicles.append(
            {
                "vehicle_id": f"VEH-{uid}",
                "pdn_id": pdn_id,
                "sales_model": model,
                "planned_delivery_date": _iso(planned),
                "location_state": _location_for(rng, planned),
            }
        )
        pdn_members.setdefault(pdn_id, []).append(f"VEH-{uid}")
        uid += 1

    # PDN records (quantity by construction; delayed_days set on the disrupted one).
    veh_by_id = {v["vehicle_id"]: v for v in vehicles}

    def pdn_records(delayed_pdn: str | None) -> list[dict]:
        recs: list[dict] = []
        for pdn_id, members in sorted(pdn_members.items()):
            model = veh_by_id[members[0]]["sales_model"]
            recs.append(
                {
                    "pdn_id": pdn_id,
                    "sales_model": model,
                    "quantity": len(members),
                    "delayed_days": delay_days if pdn_id == delayed_pdn else 0,
                }
            )
        return recs

    def dataset(state: str, delayed_pdn: str | None, veh: list[dict], disruption: dict) -> dict:
        return {
            "meta": {
                "seed": seed,
                "now": _iso(BASE_DATE),
                "state": state,
                "horizon_weeks": HORIZON_WEEKS,
                "sales_models": list(SALES_MODELS),
                "n_customers": n_customers,
            },
            "pdns": pdn_records(delayed_pdn),
            "vehicles": veh,
            "sos": sos,
            "disruption": disruption,
        }

    baseline = dataset("good", None, vehicles, {})

    # --- Disruption: delay the lowest-id incumbent-carrying PDN whose vehicles
    #     are all still movable (not committed), so the repair is meaningful. ---
    incumbent_pdns = sorted({veh_by_id[so["current_vehicle_id"]]["pdn_id"] for so in sos})
    delayed_pdn = None
    for pdn_id in incumbent_pdns:
        members = pdn_members[pdn_id]
        if all(veh_by_id[m]["location_state"] not in COMMITTED_STATES for m in members):
            delayed_pdn = pdn_id
            break
    if delayed_pdn is None:  # fallback: any incumbent PDN with a movable vehicle
        for pdn_id in incumbent_pdns:
            if any(
                veh_by_id[m]["location_state"] not in COMMITTED_STATES for m in pdn_members[pdn_id]
            ):
                delayed_pdn = pdn_id
                break

    delayed_vehicle_ids = sorted(pdn_members[delayed_pdn])
    delayed_set = set(delayed_vehicle_ids)
    disrupted_vehicles: list[dict] = []
    for v in vehicles:
        if v["vehicle_id"] in delayed_set:
            new_date = date.fromisoformat(v["planned_delivery_date"]) + timedelta(days=delay_days)
            disrupted_vehicles.append({**v, "planned_delivery_date": _iso(new_date)})
        else:
            disrupted_vehicles.append(dict(v))

    disrupted_orders = sorted(
        so["order_id"] for so in sos if so["current_vehicle_id"] in delayed_set
    )
    disruption = {
        "pdn": delayed_pdn,
        "delay_days": delay_days,
        "delayed_vehicles": delayed_vehicle_ids,
        "disrupted_orders": disrupted_orders,
    }
    pull = dataset("disrupted", delayed_pdn, disrupted_vehicles, disruption)
    return {"baseline": baseline, "pull": pull}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fabricate an XAS allocation scenario.")
    ap.add_argument("--seed", type=int, default=20)
    ap.add_argument("--customers", type=int, default=30)
    ap.add_argument("--orders", type=int, default=40)
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
        tag = (
            f"disruption PDN {d['pdn']} +{d['delay_days']}d, "
            f"{len(d['disrupted_orders'])} orders freed"
            if d
            else "no disruption"
        )
        print(f"wrote {path}  ({len(data['sos'])} SOs, {len(data['vehicles'])} vehicles; {tag})")


if __name__ == "__main__":
    main()

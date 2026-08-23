"""Fabricate a rich XAS allocation scenario: good world -> introduce a delay.

Deterministic: everything derives from one integer ``seed`` via ``random.Random``
and a FIXED base date — no wall-clock, no module-level randomness — so a given
seed regenerates a byte-identical dataset (the determinism the whole design
rests on, upheld on the supply side of the boundary now).

Model (the real-XAS vocabulary, minus the jobcard types we don't need):
  VSO jobcard   what a customer ordered: a header (JobKey, DeliveryDate, priority,
                Owner) + JobItems, one per WANTED CAR (`JobItemType:"ModelItem"`,
                one `LineNum`). The allocatable order is one jobitem.
  Vehicle       a car in the pool, real or future, keyed by `VehicleCode`. Its
                `VehicleClassification` is `"Vehicle"` (real, a VIN — a HARD
                binding) or `"Future"` (not yet built — a SOFT binding).

Supply is ONE ``vehicles`` list (no VPO/VGR jobcards, no PO-line slots, no
qty-expansion). A jobitem is on-time in the good world; the disruption slips
``EtaDealer`` on a coherent batch of vehicles, which breaks the jobitems riding
them (their allocated car now arrives past the VSO's promised date) — the repair
the agent performs.

Emits the real-field subset, nested as the real API nests, so ``flatten.py``
reads it 1:1. Output (JSON, real dates ``YYYY-MM-DD``):
  data/baseline.json  — the good, on-time world (reference / diffing)
  data/pull.json      — the SAME world after the delay (the pull target)
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# The pull date / front of the horizon. A constant, never today() — determinism.
BASE_DATE = date(2026, 8, 3)  # a Monday
HORIZON_WEEKS = 13

# Sales-model codes. In real XAS a VSO's SalesModelCode is a full trim/colour
# code (T5040UECLMQ0009) and matches a vehicle's SalesModel byte-for-byte;
# ModelId.Code holds the model above it (T5040) and matches no order. The mock
# keeps SalesModel == ModelId.Code so both readings of the fake agree.
SALES_MODELS = ("SM1", "SM2", "SM3", "SM4", "SM5")
MODEL_NAMES = {
    "SM1": "Chery Tiggo 8",
    "SM2": "JAECOO 7",
    "SM3": "Omoda 5",
    "SM4": "BYD Atto 3",
    "SM5": "Geely Coolray",
}

# 30 customers: six named dealers (so "prefer Colmobil" has a real target) then
# generated ones. Priority cycles A,A,B,B,C,C — Colmobil/Delek land on A.
NAMED = ("Colmobil", "Delek Motors", "Champion", "Talcar", "Carasso", "Lubinski")
PRIORITY_CYCLE = ("A", "A", "B", "B", "C", "C")

# VehicleClassification values (real XAS): "Vehicle" = real/hard, "Future" = soft.
HARD, SOFT = "Vehicle", "Future"

FIRST_VSO = 4000  # VSO JobKey numbering, so keys read like VSO-4000-1
FIRST_VEH = 9000  # VehicleCode numbering


def _iso(d: date) -> str:
    return d.isoformat()


def _customers(n: int) -> list[tuple[str, str, str]]:
    """(name, customer_id, priority) for n customers, deterministically."""
    out: list[tuple[str, str, str]] = []
    for i in range(n):
        name = NAMED[i] if i < len(NAMED) else f"Dealer {i + 1:02d}"
        out.append((name, f"CUST-{i + 1:03d}", PRIORITY_CYCLE[i % len(PRIORITY_CYCLE)]))
    return out


def _make_vehicle(rng: random.Random, model: str, eta: date, code: int) -> dict:
    """One vehicle record in the pool, real (`Vehicle`) or future (`Future`).

    Emits the real-field subset the API nests; ``flatten`` reads
    ``VehicleCode / VehicleClassification / SalesModel / EtaDealer``. The rest
    (Vin, Make, Status, InventoryStatus, IsReserved, Owner) is realistic
    passthrough. ``ExpectedCustomerDeliveryDate`` is emitted equal to
    ``EtaDealer`` (the read field is EtaDealer; a one-line switch if that flips).

    ``SalesModel`` is the real eligibility key and carries the same value as
    ``ModelId.Code`` here, so the fake stays substitutable for the real source
    (which is what makes ``tests/test_invariant.py`` mean anything). On real data
    the two differ — ``SalesModel`` is the trim/colour code an order names,
    ``ModelId.Code`` the model above it — and only ``SalesModel`` ever matches."""
    classification = rng.choices((HARD, SOFT), weights=[60, 40])[0]
    is_real = classification == HARD
    return {
        "VehicleCode": f"VEH-{code}",
        # A future car has no VIN yet; a real one does.
        "Vin": f"VIN{code:08d}" if is_real else "",
        "SalesModel": model,
        "ModelId": {"Code": model, "Name": MODEL_NAMES.get(model, model)},
        "Make": "Chery",
        "VehicleClassification": classification,
        # The tenant's real vehicle-status codes (skills/xas-reporting/index.md):
        # 03 = In Stock, 01 = Ordered. Descriptive only — the future/real split
        # the solver uses comes from VehicleClassification above.
        "Status": (
            {"Code": "03", "Name": "In Stock"} if is_real else {"Code": "01", "Name": "Ordered"}
        ),
        "InventoryStatus": "Available" if is_real else "Future",
        "EtaDealer": _iso(eta),
        "ExpectedCustomerDeliveryDate": _iso(eta),
        "IsReserved": False,
        "Owner": "",
    }


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

    vehicles: list[dict] = []
    veh_box = [FIRST_VEH]

    def new_vehicle(model: str, eta: date) -> dict:
        veh_box[0] += 1
        v = _make_vehicle(rng, model, eta, veh_box[0])
        vehicles.append(v)
        return v

    # --- Demand: VSO jobcards, each with 1-3 car lines; one on-time vehicle
    #     built per car (the incumbent), real or future. -----------------------
    vsos: list[dict] = []
    # order_key -> VehicleCode, for the disruption logic below.
    incumbent: dict[str, str] = {}
    n_rows = 0
    vso_num = FIRST_VSO
    while n_rows < n_orders:
        name, cid, prio = rng.choice(customers)
        job_key = f"VSO-{vso_num}"
        vso_num += 1
        # DeliveryDate is a VSO-header promise shared by its car lines.
        delivery = rng.choice(promise_window)
        items: list[dict] = []
        n_lines = rng.choices([1, 2, 3], weights=[55, 30, 15])[0]
        for line in range(1, n_lines + 1):
            if n_rows >= n_orders:
                break
            model = rng.choice(SALES_MODELS)
            # The incumbent car is on time in the good world: EtaDealer == promise.
            veh = new_vehicle(model, delivery)
            order_key = f"{job_key}-{line}"
            incumbent[order_key] = veh["VehicleCode"]

            item = {
                "JobItemType": "ModelItem",
                "LineNum": line,
                "SalesModelCode": model,
                "Label": MODEL_NAMES.get(model, model),
                "Quantity": 1,
                "Prices": [{"GrossTotal": rng.choice([32000, 38000, 45000, 52000, 61000])}],
                # Solver escalation fields; real-data derivation is a TODO (there
                # is no direct XAS field — Weight vs customer tier vs delay history).
                "n_prior_delays": rng.choices([0, 1, 2, 3], weights=[70, 18, 8, 4])[0],
                "days_backordered": rng.choices(
                    [0, 0, 0, 7, 14, 30], weights=[50, 15, 10, 12, 8, 5]
                )[0],
                "times_rescheduled": rng.choices([0, 1, 2], weights=[75, 18, 7])[0],
            }
            # Incumbent link: HARD rows carry VehicleId.Code (== VehicleCode);
            # SOFT rows carry an Alloc link to their Future vehicle (the real API
            # resolves that through AllocSource*; the mock links straight to it).
            if veh["VehicleClassification"] == HARD:
                item["VehicleId"] = {"Code": veh["VehicleCode"]}
                item["AllocSourceClassification"] = "VGR"
            else:
                item["AllocSourceClassification"] = "VPO"
                item["AllocatedVehicleCode"] = veh["VehicleCode"]
            items.append(item)
            n_rows += 1

        vsos.append(
            {
                "JobKey": job_key,
                "DMSJCEntry": str(vso_num - 1),
                "DeliveryDate": _iso(delivery),
                "JobPriority": {"Code": prio},
                "JobStatus": "Open",
                "Accounts": {
                    "Owner": {
                        "AccountName": name,
                        "AccountUUID": cid,
                        "AccountDMSCode": cid.replace("CUST-", "D"),
                    }
                },
                # Header model codes (model-level); jobitems carry their own.
                "ModelCode": items[0]["SalesModelCode"] if items else "",
                "SalesModelCode": items[0]["SalesModelCode"] if items else "",
                "JobItems": items,
            }
        )

    # --- Spare (unallocated) supply — the wiggle room a repair uses. ---------
    for _ in range(int(n_orders * spare_ratio)):
        model = rng.choice(SALES_MODELS)
        eta = rng.choice(weeks)
        new_vehicle(model, eta)

    def dataset(state: str, vehs: list[dict], disruption: dict) -> dict:
        return {
            "meta": {
                "seed": seed,
                "now": _iso(BASE_DATE),
                "state": state,
                "horizon_weeks": HORIZON_WEEKS,
                "sales_models": list(SALES_MODELS),
                "n_customers": n_customers,
            },
            "vsos": vsos,
            "vehicles": vehs,
            "disruption": disruption,
        }

    baseline = dataset("good", vehicles, {})

    # --- Disruption: delay a COHERENT batch — every incumbent-carrying vehicle
    #     of ONE model (a "model X shipment slipped") — so the repair is
    #     meaningful and the disrupted orders are guaranteed late (their
    #     incumbent was on time, EtaDealer == promise, so +delay_days runs past
    #     the promise). Pick the model with the most incumbent cars; tie by code.
    incumbent_codes = set(incumbent.values())
    veh_by_code = {v["VehicleCode"]: v for v in vehicles}
    by_model: dict[str, list[str]] = defaultdict(list)
    for code in incumbent_codes:
        by_model[veh_by_code[code]["ModelId"]["Code"]].append(code)
    delayed_model = min(by_model, key=lambda m: (-len(by_model[m]), m))
    delayed_codes = set(by_model[delayed_model])

    disrupted_vehicles: list[dict] = []
    for v in vehicles:
        if v["VehicleCode"] in delayed_codes:
            new_eta = date.fromisoformat(v["EtaDealer"]) + timedelta(days=delay_days)
            disrupted_vehicles.append(
                {
                    **v,
                    "EtaDealer": _iso(new_eta),
                    "ExpectedCustomerDeliveryDate": _iso(new_eta),
                }
            )
        else:
            disrupted_vehicles.append(dict(v))

    disrupted_orders = sorted(k for k, code in incumbent.items() if code in delayed_codes)
    disruption = {
        "now": _iso(BASE_DATE),
        "delay_days": delay_days,
        "delayed_model": delayed_model,
        "delayed_vehicles": sorted(delayed_codes),
        "disrupted_orders": disrupted_orders,
    }
    pull = dataset("disrupted", disrupted_vehicles, disruption)
    return {"baseline": baseline, "pull": pull}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fabricate an XAS allocation scenario.")
    ap.add_argument("--seed", type=int, default=20)
    ap.add_argument("--customers", type=int, default=30)
    ap.add_argument("--orders", type=int, default=40, help="VSO car lines (demand)")
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
        by_class: dict[str, int] = {}
        for v in data["vehicles"]:
            c = v["VehicleClassification"]
            by_class[c] = by_class.get(c, 0) + 1
        tag = (
            f"disruption {d['delayed_model']} +{d['delay_days']}d on "
            f"{len(d['delayed_vehicles'])} vehicles, {len(d['disrupted_orders'])} orders freed"
            if d
            else "no disruption"
        )
        rows = sum(len(vso["JobItems"]) for vso in data["vsos"])
        print(
            f"wrote {path}  ({len(data['vsos'])} VSOs / {rows} car lines, "
            f"{by_class.get(HARD, 0)} real + {by_class.get(SOFT, 0)} future; {tag})"
        )


if __name__ == "__main__":
    main()

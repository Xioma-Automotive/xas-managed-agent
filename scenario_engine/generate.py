"""Fabricate an XAS allocation scenario in the app MCP's OWN response shapes.

Deterministic: everything derives from one integer ``seed`` via ``random.Random``
and a FIXED base date — no wall-clock, no module-level randomness — so a given
seed regenerates a byte-identical dataset (the determinism the whole design
rests on, upheld on the supply side of the boundary now).

Model (the real-XAS vocabulary, minus the jobcard types we don't need):
  VSO jobcard   what a customer ordered: a card (``JobKey``, ``DueDateTime``,
                ``JobPriority``, ``Accounts.Owner``) plus ``jobitems`` — one
                ``ModelItem`` line per WANTED CAR, and one trailing
                ``Configuration`` line, as real cards carry, so the type filter
                has something to reject. The allocatable order is one
                ``ModelItem`` line, keyed ``{JobKey}-{LineNum}``.
  Vehicle       a car in the pool, keyed by ``VehicleCode``. Hard vs soft comes
                from its ``Status.Name`` — "In Stock" is on the lot, "Ordered" is
                still inbound. NOT from ``VehicleClassification``, which is XAS's
                own pool axis (Truck/Vehicle/InventoryVehicles) and is emitted
                here UNCORRELATED with the status on purpose: anything that
                buckets on it instead breaks loudly against this fake.

Supply is ONE ``vehicles`` list (no VPO/VGR jobcards, no PO-line slots, no
qty-expansion). Every car line is on-time in the good world; the disruption slips
``EtaDealer`` on a coherent batch of vehicles — one model's worth of INBOUND
allocated cars — which breaks the lines riding them (their allocated car now
arrives past the VSO's promised date). That is the repair the agent performs.
Only inbound cars can slip: a car already on the lot has ``eta == now`` by
definition (``datasource.vehicle_eta``), so delaying it could never make it late.

Emits the app MCP's own response shapes (`docs/mcp-response-schema.md`):

  data/mcp-jobcards.json  {totalCount, list: [card, ... each with jobitems[]]}
  data/mcp-vehicles.json  {total, records: [...]}

and the MAPPED result, which is what ``flatten`` reads offline and what
``web.py`` mounts:

  data/baseline.json  — the good, on-time world (reference / diffing)
  data/pull.json      — the SAME world after the delay (the pull target)

The mapping is not duplicated here: this module fabricates responses and hands
them to ``datasource.map_world`` exactly as ``ScenarioEngineSource`` does, which
is the same ``map_response`` the live pull runs through. So `data/pull.json` is
DERIVED, not authored — a field this generator emits that the mapper ignores is
dead, which is the point: the fake cannot drift into supplying something the real
MCP would not. Fields the mapper never reads are therefore left out, including
the ``AllocType`` / ``AllocQty`` / ``AllocSourceJobNum`` block — resolving a
live allocation through it is still an open question (`docs/mcp-response-schema.md`).
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# Host-side only, and the whole point: ONE mapping, shared with the live pull.
# `datasource` imports this module lazily (inside `pull`), so there is no cycle.
import datasource

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# The pull date / front of the horizon. A constant, never today() — determinism.
BASE_DATE = date(2026, 8, 3)  # a Monday
HORIZON_WEEKS = 13

# Sales-model codes. In real XAS a VSO line's JobItemCode is a full trim/colour
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

# The two vehicle statuses the fake uses, spelled exactly as XAS spells them —
# `datasource.status_bucket` reads the NAME, never the code, so these strings are
# load-bearing. "real" = on the lot (a hard binding), "future" = still inbound.
STATUS_BY_BUCKET = {
    "real": {"Code": "03", "Name": "In Stock", "Color": "In Stock"},
    "future": {"Code": "01", "Name": "Ordered", "Color": "Ordered"},
}

# XAS's own `VehicleClassification` — a pool axis, NOT the solver's binding. Dev
# data carries "Truck" on cars of every status, so the fake picks it independently
# of the bucket: correlating them would let a reader bucket on the wrong field and
# still get the right answer here.
XAS_CLASSIFICATIONS = ("Vehicle", "Truck", "InventoryVehicles")

# How often an allocated car is still inbound. Deliberately future-heavy: a
# forward order book allocates against supply that has not landed yet, and only
# an inbound car can be made late (see the module docstring).
INBOUND_INCUMBENT_WEIGHTS = {"future": 70, "real": 30}
# The spare pool is mixed — cars on the lot are the wiggle room a repair uses.
SPARE_WEIGHTS = {"future": 45, "real": 55}

# The slips the disruption applies, in days. Delayed cars are split evenly across
# these, so a mixed disruption — one shipment a week late, another a month — is
# the default rather than a special case. A single tier still works.
DELAY_TIERS = (7, 21, 30)

CONFIG_ITEM_TYPE = "Configuration"  # a non-car line, for the type filter to drop
BRANCH = "69f07fdaf930e4ee6d524dc1"  # opaque id, passthrough

FIRST_VSO = 4000  # VSO JobKey numbering, so keys read like VSO-4000-1
FIRST_VEH = 9000  # VehicleCode numbering
FIRST_ENTRY = 502381  # JobEntryNum — an int on the wire


def _iso(d: date) -> str:
    return d.isoformat()


def _instant(d: date) -> str:
    """A date as XAS sends it: an ISO instant. Only the date part is ever read
    (``datasource._date_part``), so the time is a fixed filler, not a signal."""
    return f"{d.isoformat()}T06:00:00Z"


def _customers(n: int) -> list[tuple[str, str, str]]:
    """(name, customer_id, priority) for n customers, deterministically."""
    out: list[tuple[str, str, str]] = []
    for i in range(n):
        name = NAMED[i] if i < len(NAMED) else f"Dealer {i + 1:02d}"
        out.append((name, f"CUST-{i + 1:03d}", PRIORITY_CYCLE[i % len(PRIORITY_CYCLE)]))
    return out


def _pick_bucket(rng: random.Random, weights: dict[str, int]) -> str:
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


def _make_vehicle(rng: random.Random, model: str, eta: date, code: int, bucket: str) -> dict:
    """One vehicle record in the ``get_vehicles`` shape.

    ``bucket`` is "real" (on the lot) or "future" (inbound) and drives only the
    ``Status`` block, because that is the sole field the mapper buckets on.
    ``SalesModel`` is the eligibility key and carries the same value as
    ``ModelId.Code`` here, so both readings of the fake agree; on real data the
    two differ and only ``SalesModel`` ever matches. ``AvailableBy`` is emitted
    equal to ``EtaDealer`` — the tenant fills the former, the schema prefers the
    latter, and ``datasource.vehicle_eta`` reads EtaDealer first.
    """
    is_real = bucket == "real"
    return {
        "VehicleCode": f"VEH-{code}",
        # A car on the lot has a VIN; one still on order does not yet.
        "Vin": f"VIN{code:08d}" if is_real else "",
        "SalesModel": model,
        "ModelId": {"Code": model, "Name": MODEL_NAMES.get(model, model)},
        "Make": {"Code": "LVV-J", "Name": "Chery"},
        "Description": MODEL_NAMES.get(model, model),
        "Status": dict(STATUS_BY_BUCKET[bucket]),
        "VehicleClassification": rng.choice(XAS_CLASSIFICATIONS),
        "EtaDealer": _iso(eta),
        "AvailableBy": _iso(eta),
        "LicenseNumber": "",
        "IsReserved": False,
        "PortLocation": None,
    }


def _car_line(
    rng: random.Random,
    line: int,
    model: str,
    vehicle: dict,
    bucket: str,
) -> dict:
    """One ``ModelItem`` jobitem: ONE wanted car, and the car allocated to it.

    ``Quantity`` and ``AllocQty`` are both emitted as 1. One car per line is the
    current assumption (2026-08-25) — a line resolves to at most one vehicle code,
    so a second car on it could never be linked to anything — and the fake matches
    it rather than fabricating demand the pull cannot represent.

    The allocation link is where the fake is deliberately kinder than dev: a car
    on the lot is a hard binding the line states outright (``VehicleId.Code``),
    and an inbound one gets ``AllocatedVehicleCode`` — the fake's direct stand-in
    for the Alloc block whose VPO hop the real pull cannot make yet
    (`docs/mcp-response-schema.md` Q1). ``datasource._line_vehicle`` reads both.
    """
    item = {
        "LineNum": line,
        "JobItemCode": model,  # the eligibility key == vehicle.SalesModel
        "SalesModelCode": model,  # same value on ModelItem lines
        "JobItemType": datasource.CAR_ITEM_TYPE,
        "JobItemStatus": "Open",
        "Label": MODEL_NAMES.get(model, model),
        "Quantity": 1,
        "Prices": [{"GrossTotal": rng.choice([32000, 38000, 45000, 52000, 61000])}],
    }
    item["AllocQty"] = 1
    if bucket == "real":
        item["VehicleId"] = {"Code": vehicle["VehicleCode"]}
    else:
        item["AllocatedVehicleCode"] = vehicle["VehicleCode"]
    return item


def _config_line(line: int) -> dict:
    """A non-car line. It exists so `line_drops["not_a_car_line"]` is non-zero —
    the type filter is the only thing separating a car from a config row."""
    return {
        "LineNum": line,
        "JobItemCode": "CCO",
        "JobItemType": CONFIG_ITEM_TYPE,
        "JobItemStatus": "Open",
        "Label": "Customer configuration",
        "Quantity": 1,
    }


def _payloads(cards: list[dict], vehicles: list[dict], disruption: dict) -> dict:
    """The two response payloads, plus the fake-only ``_scenario`` sidecar.

    ``now`` and the delay manifest have no place in an MCP response — XAS records
    neither — but the committed files have to carry them or the fake could not be
    read back deterministically. They ride under one underscore-prefixed key so
    nothing mistakes them for part of the real shape; ``datasource.map_world`` is
    the only reader.
    """
    return {
        "jobcards": {
            "totalCount": len(cards),
            "list": cards,
            "_scenario": {"now": _iso(BASE_DATE), "disruption": disruption},
        },
        "vehicles": {"total": len(vehicles), "records": vehicles},
    }


def generate(
    seed: int = 20,
    n_customers: int = 30,
    n_vsos: int = 20,
    unallocated_share: float = 0.4,
    delay_tiers: tuple[int, ...] = DELAY_TIERS,
) -> dict[str, dict]:
    """Build the good world and its disrupted twin, both in MCP response shapes.

    ``n_vsos`` is the number of job cards. Each carries 1-3 car lines and each
    line is one order for one car, so the order count follows from it.

    ``unallocated_share`` is the fraction of the CAR POOL left unallocated — the
    wiggle room a repair has. 0.4 means 40% of the cars are free, so the spare
    count is derived from demand rather than set directly.

    ``delay_tiers`` are the slips the disruption applies, in days. The delayed
    cars are split evenly across the tiers, so one shipment slipping a week and
    another a month is the normal case rather than a special one.

    Returns ``{'baseline': world, 'pull': world}`` where a world is
    ``{jobcards, vehicles}`` — what ``datasource.map_world`` consumes.
    """
    rng = random.Random(seed)
    weeks = [BASE_DATE + timedelta(weeks=k) for k in range(HORIZON_WEEKS)]
    promise_window = weeks[: HORIZON_WEEKS - 2]  # leave slack so lateness is possible
    customers = _customers(n_customers)

    vehicles: list[dict] = []
    bucket_by_code: dict[str, str] = {}
    veh_box = [FIRST_VEH]

    def new_vehicle(model: str, eta: date, bucket: str) -> dict:
        veh_box[0] += 1
        v = _make_vehicle(rng, model, eta, veh_box[0], bucket)
        # The fake must speak a status the mapper knows, or the car is silently
        # dropped as out-of-scope and the scenario quietly thins out.
        assert datasource.status_bucket(v) == bucket, v["Status"]
        vehicles.append(v)
        bucket_by_code[v["VehicleCode"]] = bucket
        return v

    # --- Demand: VSO jobcards, each with 1-3 car lines; one on-time vehicle
    #     built per car (the allocation), on the lot or inbound. ---------------
    cards: list[dict] = []
    # order_key -> VehicleCode, for the disruption logic below.
    allocations: dict[str, str] = {}
    n_rows = 0
    vso_num = FIRST_VSO
    for _ in range(n_vsos):
        name, cid, prio = rng.choice(customers)
        job_key = f"VSO-{vso_num}"
        entry_num = FIRST_ENTRY + (vso_num - FIRST_VSO)
        vso_num += 1
        # DueDateTime is a VSO-header promise shared by its car lines.
        delivery = rng.choice(promise_window)
        # EntryDateTime is display/provenance only. The escalation counters this
        # used to feed were deleted with the weight terms on 2026-08-26.
        entered = BASE_DATE - timedelta(days=rng.randint(5, 60))
        items: list[dict] = []
        n_lines = rng.choices([1, 2, 3], weights=[55, 30, 15])[0]
        for line in range(1, n_lines + 1):
            model = rng.choice(SALES_MODELS)
            bucket = _pick_bucket(rng, INBOUND_INCUMBENT_WEIGHTS)
            # ONE car per line, ONE vehicle per line. The allocated car is on time
            # in the good world: EtaDealer == promise.
            veh = new_vehicle(model, delivery, bucket)
            allocations[f"{job_key}-{line}"] = veh["VehicleCode"]
            items.append(_car_line(rng, line, model, veh, bucket))
            n_rows += 1
        # Car lines keep 1..n so an order key reads VSO-4000-1; the config line
        # trails them (real cards put it anywhere — LineNum order is not meaning).
        items.append(_config_line(len(items) + 1))

        cards.append(
            {
                "JobKey": job_key,
                "JobEntryNum": entry_num,
                "DMSJCEntry": str(entry_num),
                "DMSJCNum": str(entry_num),
                "DueDateTime": _instant(delivery),
                "EntryDateTime": _instant(entered),
                "JobStatus": {"Code": "23", "Label": "Order"},
                "JobPriority": {"Code": prio},
                "isCanceled": False,
                "Branch": BRANCH,
                "Accounts": {
                    "Owner": {
                        "AccountName": name,
                        "AccountUUID": cid,
                        "AccountDMSCode": cid.replace("CUST-", "D"),
                    }
                },
                "jobitems": items,
            }
        )

    # --- Spare (unallocated) supply — the wiggle room a repair uses. ---------
    # Every order already has one car, so demand == allocated cars. To leave
    # `unallocated_share` of the FINAL pool free, the spare count is
    # demand * f/(1-f) rather than demand * f.
    f = min(max(unallocated_share, 0.0), 0.95)
    n_spare = round(n_rows * f / (1 - f)) if f else 0
    for _ in range(n_spare):
        model = rng.choice(SALES_MODELS)
        eta = rng.choice(weeks)
        new_vehicle(model, eta, _pick_bucket(rng, SPARE_WEIGHTS))

    baseline = _payloads(cards, vehicles, {})

    # --- Disruption: slip the INBOUND cars that orders are counting on, by
    #     several different amounts. Only inbound cars are eligible — a car
    #     already on the lot has its eta pinned to `now`, so moving its EtaDealer
    #     changes nothing. Their cars were on time (eta == promise), so any
    #     slip puts them past it and the repair is meaningful.
    #
    #     Cars are dealt round-robin into the tiers after a deterministic
    #     shuffle, so each tier gets a spread of models and customers rather than
    #     one shipment's worth. That matters: a disruption where every 30-day slip
    #     lands on one model is a much easier problem than a mixed one.
    candidates = sorted(
        code for code in set(allocations.values()) if bucket_by_code[code] == "future"
    )
    rng.shuffle(candidates)
    tiers = tuple(delay_tiers) or (0,)
    slip_by_code = {code: tiers[i % len(tiers)] for i, code in enumerate(candidates)}

    disrupted_vehicles: list[dict] = []
    for v in vehicles:
        slip = slip_by_code.get(v["VehicleCode"])
        if slip:
            new_eta = date.fromisoformat(v["EtaDealer"]) + timedelta(days=slip)
            # Both, because the tenant fills AvailableBy and the schema prefers
            # EtaDealer — a slip that moved only one would be incoherent.
            disrupted_vehicles.append(
                {**v, "EtaDealer": _iso(new_eta), "AvailableBy": _iso(new_eta)}
            )
        else:
            disrupted_vehicles.append(dict(v))

    by_tier: dict[str, list[str]] = defaultdict(list)
    for code, slip in sorted(slip_by_code.items()):
        by_tier[str(slip)].append(code)

    # `disrupted_orders` is NOT declared here: map_response derives it from the
    # etas, and that derivation is what the real pull has to rely on.
    pull = _payloads(
        cards,
        disrupted_vehicles,
        {
            # The worst slip, for a one-line summary; `delay_tiers` has the split.
            "delay_days": max(tiers),
            "delay_label": "/".join(str(d) for d in sorted(tiers)) + " days",
            "delay_tiers": {k: v for k, v in sorted(by_tier.items(), key=lambda kv: int(kv[0]))},
            "delayed_vehicles": sorted(slip_by_code),
        },
    )
    return {"baseline": baseline, "pull": pull}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fabricate an XAS allocation scenario.")
    ap.add_argument("--seed", type=int, default=20)
    ap.add_argument("--customers", type=int, default=30)
    ap.add_argument("--vsos", type=int, default=20, help="job cards; 1-3 car lines each")
    ap.add_argument(
        "--unallocated",
        type=float,
        default=0.4,
        help="fraction of the car pool left free (0.4 = 40%% unallocated)",
    )
    ap.add_argument(
        "--delay-days",
        type=int,
        nargs="+",
        default=list(DELAY_TIERS),
        metavar="D",
        help="the slips to apply, in days; delayed cars are split across them",
    )
    ap.add_argument("--out", type=Path, default=DATA_DIR, help="output directory")
    args = ap.parse_args()

    result = generate(
        seed=args.seed,
        n_customers=args.customers,
        n_vsos=args.vsos,
        unallocated_share=args.unallocated,
        delay_tiers=tuple(args.delay_days),
    )
    args.out.mkdir(parents=True, exist_ok=True)

    # The MCP-shaped payloads: what `ScenarioEngineSource` reads by default. Only
    # the disrupted world is committed — it IS the pull; the baseline is written
    # mapped, for reference and diffing.
    for name, payload in (
        ("mcp-jobcards", result["pull"]["jobcards"]),
        ("mcp-vehicles", result["pull"]["vehicles"]),
    ):
        path = args.out / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"wrote {path}")

    for name, world in result.items():
        rich = datasource.map_world(world)
        path = args.out / f"{name}.json"
        path.write_text(json.dumps(rich, indent=2, sort_keys=True))
        excluded = rich["meta"]["excluded"]
        d = rich["disruption"]
        by_bucket: dict[str, int] = {}
        for v in rich["vehicles"]:
            c = v["VehicleClassification"]
            by_bucket[c] = by_bucket.get(c, 0) + 1
        delayed = d.get("delayed_vehicles") or []
        if delayed:
            split = ", ".join(
                f"{len(codes)}x +{days}d" for days, codes in (d.get("delay_tiers") or {}).items()
            )
            tag = f"disruption {split} on {len(delayed)} cars, {len(d['disrupted_orders'])} orders freed"
        else:
            tag = "no disruption"
        print(
            f"wrote {path}  ({excluded['orders_kept']} VSOs / "
            f"{excluded['lines_kept']} car lines, "
            f"{by_bucket.get('Vehicle', 0)} on the lot + "
            f"{by_bucket.get('Future', 0)} inbound; {tag})"
        )


if __name__ == "__main__":
    main()

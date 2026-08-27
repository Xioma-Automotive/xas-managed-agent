"""Where the allocation pull comes from — the scenario CSVs, translated HOST-SIDE.

This is DECIDE-7 made concrete, and since 2026-08-27 there is only one kind of
source: a **scenario directory** holding the two CSVs the real XAS export speaks,
`orders.csv` + `vehicles.csv`. `scenario_engine/real_*.py` carve those out of the
real export; `translate()` maps them into the two JSON payloads `web.py` mounts
into the sandbox, and `xas_allocation.flatten` turns those into the solver's
snapshot THERE.

    data/scenario-mixed/orders.csv    ─┐
    data/scenario-mixed/vehicles.csv  ─┴→ translate() → orders.json  ─┐ mounted
                                                     → vehicles.json ─┘

**The app MCP is no longer a source here.** It used to answer the pull through
`get_job_cards` / `get_vehicles`, and that path is gone: no job cards, no
`jobitems`, no projection to widen. The MCP tools the AGENT holds remain the
REPORTING lane's, which is a different thing entirely — see the hard rule in
`setup_agent.py`'s prompt.

**One mapping.** `translate()` is it. Filter and translate in one pass, because
the filter's *reasons* are part of the output: a plan over 1 of 25 orders that
does not say so reads as the whole book. Everything dropped is counted by reason
into `meta.excluded`, and `alloc_tools.summarize` carries that into the agent's
context.

**This module runs on our host, never in the sandbox** — same rule that keeps
`scenario_engine/`'s code out of the agent. The agent only ever receives the
already-translated JSON.

Grain: **one order row is one order for one car**, keyed by its own `OrderId`.
There are no job cards and no lines in this export, so the two-level
`{JobKey}-{LineNum}` key and the "a line asking for 3 cars" question are both
gone with the MCP.
"""

from __future__ import annotations

import collections
import csv
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
ORDERS_FILE = "orders.csv"
VEHICLES_FILE = "vehicles.csv"
SCENARIO_FILE = "scenario.json"  # optional sidecar; carries the pull date
SCENARIO_GLOB = "scenario-*"

# The export's capture date, and the default pull date. FIXED, not wall-clock: the
# scenarios are static files, so a clock would make the same data mean something
# different tomorrow — an order late by 3 days today is late by 4 with no row
# having changed, and the tests drift a day at a time. Matches
# `scenario_engine.real_export.CAPTURED`; a scenario may override it in its
# sidecar, and `XAS_PULL_NOW` overrides both.
DEFAULT_NOW = date(2026, 8, 25)

# A car is supply at all only in these three states — the rest are delivered,
# registered, in dispute or demo stock. In this export a car's status IS its
# allocation state, so the split below is not about hard vs soft (there is no such
# distinction any more): it is about whether the car is free or held by an order.
POOL_STATUSES = frozenset({"available for sale", "dealer order confirmation", "dealer reservation"})
AVAILABLE_STATUS = "available for sale"

# Columns the pull cannot do without. A CSV column either is in the header or it
# is not, so this is checked once at read time and raises naming what is missing —
# unlike the MCP's projection, there is no "absent from some rows" case to tell
# apart from "absent from every row".
REQUIRED_ORDER_COLUMNS = ("OrderId", "SalesModel", "etaDealer", "vehicleCode")
REQUIRED_VEHICLE_COLUMNS = ("vehicleCode", "SalesModel", "availableBy", "status.name")


@runtime_checkable
class DataSource(Protocol):
    """A callable pull. ``scope`` is a future fetch-filter (which models / month /
    orders to read) — accepted now, ignored until the pull grows a parameter."""

    def pull(self, scope: dict | None = None) -> dict: ...


# --- the export's vocabulary -------------------------------------------------


def _text(value: object) -> str:
    """A trimmed string, or "". The export sends "", "  " and a REAL trailing
    space (``'Available For Sale '`` on 86% of its available cars) for values that
    are meant to be the same, so nothing is compared unstripped."""
    return str(value).strip() if value not in (None, "") else ""


def _day(value: object) -> str:
    """'2026-09-27T00:00:00.000Z' -> '2026-09-27'. "" when there is no date.

    The export stamps everything at UTC midnight, noon or 22:00 and only the day
    matters to a promise, so the clock time is dropped rather than reasoned about.
    """
    text = _text(value)
    return text[:10] if len(text) >= 10 and text[4] == "-" else ""


def is_available(vehicle: dict[str, str]) -> bool:
    """Free supply: no order holds this car. In this export the car's own status
    says so — there is no separate allocation table."""
    return _text(vehicle.get("status.name")).lower() == AVAILABLE_STATUS


def in_pool(vehicle: dict[str, str]) -> bool:
    return _text(vehicle.get("status.name")).lower() in POOL_STATUSES


# --- the one mapping ---------------------------------------------------------


def translate(
    order_rows: list[dict[str, str]],
    vehicle_rows: list[dict[str, str]],
    now: date,
    source: str = "scenario",
) -> dict:
    """The export's two row streams -> the pull. PURE: no clock, no filesystem.

    Returns ``{now, meta, orders, vehicles, disruption}``. ``web.py`` splits that
    into the two mounted payloads (``orders_payload`` / ``vehicles_payload``) and
    `flatten` reads them back in the sandbox.

    Field mapping (export -> pull -> solver):

    * ``OrderId``    -> ``OrderId``    -> ``Order.order_id`` — the whole key.
    * ``etaDealer``  -> ``DeliveryDate`` -> ``Order.delivery_date``, THE PROMISE.
      It is the ORDER's column; the car's arrival is ``availableBy``. Confusing
      the two compares a date with itself and nothing is ever late.
    * ``SalesModel`` -> ``SalesModel``  -> eligibility, exact equality on both
      sides. ``modelId.code`` is the model ABOVE it (``T71604NXXMH0031`` against
      the order's ``T71604NCLMH0031``) and matches nothing, so it is not a
      fallback.
    * ``vehicleCode`` on the order -> ``VehicleCode`` -> the car it holds today;
      blank means it holds none, which is real unfilled demand.
    * ``availableBy`` -> ``EtaDealer`` -> ``Vehicle.eta_dealer``, the one field a
      delay moves.
    * ``customer.name`` -> ``Customer`` -> ``Order.customer`` — a LABEL, never a
      key and never priced. Carried so the planner can say "prioritise Delek
      Motors" and the agent can resolve that to the order ids it then names;
      steering itself stays id-shaped. Optional: blank when the column is absent,
      which is why it is not in ``REQUIRED_ORDER_COLUMNS``.
    """
    order_drops: collections.Counter = collections.Counter()
    vehicle_drops: collections.Counter = collections.Counter()
    link_drops: collections.Counter = collections.Counter()

    # --- vehicles: in the pool, with a join key and a date it can be counted on -
    vehicles: list[dict] = []
    for row in vehicle_rows:
        if not in_pool(row):
            vehicle_drops["out_of_pool_status"] += 1
            continue
        model = _text(row.get("SalesModel"))
        if not model:
            vehicle_drops["no_model"] += 1
            continue
        eta = _day(row.get("availableBy"))
        if not eta:
            vehicle_drops["no_arrival_date"] += 1
            continue
        vehicles.append(
            {
                "VehicleCode": _text(row.get("vehicleCode")),
                "SalesModel": model,
                "EtaDealer": eta,
                "Status": {
                    "Code": _text(row.get("status.code")),
                    # Stripped on purpose: 'Available For Sale ' and
                    # 'Available For Sale' both occur in one file.
                    "Name": _text(row.get("status.name")),
                },
                # The physical stage (Sea Transit / Bonded / PDI / Future
                # Vehicle). Carried for the planner to read, never priced: what a
                # car costs to move is the same whatever stage it is in.
                "Stage": _text(row.get("inv status label")),
                "Vin": _text(row.get("vin")),
                "Label": _text(row.get("modelId.name")),
            }
        )

    # --- orders: a promise to be late against, and a model to match on ---------
    kept: list[dict] = []
    for row in order_rows:
        order_id = _text(row.get("OrderId"))
        if not order_id:
            order_drops["no_order_id"] += 1
            continue
        model = _text(row.get("SalesModel"))
        if not model:
            order_drops["no_model"] += 1
            continue
        promise = _day(row.get("etaDealer"))
        if not promise:
            order_drops["no_promised_date"] += 1
            continue
        kept.append(
            {
                "OrderId": order_id,
                "SalesModel": model,
                "DeliveryDate": promise,
                "VehicleCode": _text(row.get("vehicleCode")),
                "Label": _text(row.get("modelId.name")),
                "Customer": _text(row.get("customer.name")),
            }
        )

    duplicates = sorted(
        k for k, n in collections.Counter(o["OrderId"] for o in kept).items() if n > 1
    )
    if duplicates:
        # Not a drop: two rows with one id is demand this pull cannot represent,
        # and `Snapshot.order_by_key` would raise on it later anyway. Fail here,
        # where the file that caused it is still in hand.
        raise ValueError(f"duplicate OrderId in the export — demand would be lost: {duplicates}")

    # --- prune to the reachable sub-problem ----------------------------------
    # A car no surviving order wants can never be allocated (eligibility is hard
    # equality), so dropping it is lossless and keeps the mounted file small.
    # If eligibility ever stops being equality, this pruning has to go.
    wanted = {o["SalesModel"] for o in kept}
    reachable = [v for v in vehicles if v["SalesModel"] in wanted]
    vehicle_drops["no_order_wants_this_model"] = len(vehicles) - len(reachable)
    have = {v["SalesModel"] for v in reachable}
    # NOT a drop: an order with no matching car is real unfilled demand, and the
    # solver surfaces it as an order left with no car. Named so the reply can say
    # which.
    unmatched = sorted(o["OrderId"] for o in kept if o["SalesModel"] not in have)

    # --- allocation conflicts, over EVERY row, not just the kept ones ----------
    # A car claimed by two orders is not a valid matching and would trip the
    # solver's self-check on its INPUT, so a contested car yields no allocation
    # for anyone; those orders become unallocated demand, which is what they
    # effectively are. The clash is a finding a planner wants, so it rides in meta
    # rather than being swallowed.
    claims: dict[str, list[str]] = collections.defaultdict(list)
    for row in order_rows:
        code = _text(row.get("vehicleCode"))
        if code:
            claims[code].append(_text(row.get("OrderId")))
    conflicts = [
        {"vehicle": code, "orders": sorted(ids)}
        for code, ids in sorted(claims.items())
        if len(ids) > 1
    ]
    contested = {c["vehicle"] for c in conflicts}
    vehicle_ids = {v["VehicleCode"] for v in reachable}

    for order in kept:
        code = order["VehicleCode"]
        if not code:
            continue
        if code in contested:
            order["VehicleCode"] = ""
            link_drops["double_booked_vehicle"] += 1
        elif code not in vehicle_ids:
            order["VehicleCode"] = ""
            link_drops["vehicle_not_in_the_file"] += 1

    # --- the disruption is DERIVED, not declared ------------------------------
    # What slips is a CAR — a shipment runs late, so its cars do. The export
    # records no "this shipment slipped 21 days" manifest (the carve scripts bake
    # the slip into `availableBy`), so the affected DEMAND is derived: an
    # allocated order whose car now lands past its promise. An order with no car
    # needs no manifest — `partition` already frees anything unassigned.
    #
    # `flatten` re-derives this authoritatively; this copy is what
    # `alloc_tools.summarize` shows the agent at pull time, and the two must agree.
    eta_by_id = {v["VehicleCode"]: v["EtaDealer"] for v in reachable}
    disrupted = sorted(
        o["OrderId"]
        for o in kept
        if o["VehicleCode"] and eta_by_id.get(o["VehicleCode"], "") > o["DeliveryDate"]
    )

    return {
        "now": now.isoformat(),
        "meta": {
            "now": now.isoformat(),
            "source": source,
            "sales_models": sorted(wanted),
            "excluded": {
                "orders_seen": len(order_rows),
                "orders_kept": len(kept),
                "order_drops": dict(sorted(order_drops.items())),
                "vehicles_seen": len(vehicle_rows),
                "vehicles_kept": len(reachable),
                "vehicle_drops": {k: v for k, v in sorted(vehicle_drops.items()) if v},
                "link_drops": dict(sorted(link_drops.items())),
                "orders_with_no_eligible_car": unmatched,
            },
            "conflicts": conflicts,
        },
        "orders": kept,
        "vehicles": reachable,
        "disruption": {"disrupted_orders": disrupted},
    }


def orders_payload(pull: dict) -> dict:
    """The body of the mounted ``orders.json``: the demand, the pull date and the
    provenance the turn-1 reply has to account for."""
    return {"now": pull["now"], "meta": pull["meta"], "orders": pull["orders"]}


def vehicles_payload(pull: dict) -> dict:
    """The body of the mounted ``vehicles.json``: the supply, and nothing else."""
    return {"vehicles": pull["vehicles"]}


# --- reading a scenario directory --------------------------------------------


def read_rows(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    """One CSV as row dicts, with its header checked first.

    The header check is the whole reason this is not a one-liner: a renamed column
    produces an empty funnel that looks exactly like empty data, and the file that
    caused it is only in hand here.
    """
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames or [])
        missing = [name for name in required if name not in columns]
        if missing:
            raise ValueError(f"{path} is missing column(s) {', '.join(missing)}; header: {columns}")
        return list(reader)


def scenario_now(directory: Path) -> date:
    """This scenario's pull date: ``XAS_PULL_NOW``, else the directory's
    ``scenario.json``, else the export's capture date. Never the clock."""
    override = _text(os.environ.get("XAS_PULL_NOW"))
    if override:
        return date.fromisoformat(override)
    sidecar = directory / SCENARIO_FILE
    if sidecar.is_file():
        stamped = _text((json.loads(sidecar.read_text()) or {}).get("now"))
        if stamped:
            return date.fromisoformat(stamped)
    return DEFAULT_NOW


@dataclass
class ScenarioSource:
    """One scenario directory: ``orders.csv`` + ``vehicles.csv``, translated.

    Every source is this one now. The directories are carved out of the real
    export by `scenario_engine/real_*.py`, so the pull speaks the export's own
    vocabulary and there is no fabricated world to keep in step with a real one.
    """

    directory: Path = DATA_DIR / "scenario-mixed"

    def pull(self, scope: dict | None = None) -> dict:
        orders = read_rows(self.directory / ORDERS_FILE, REQUIRED_ORDER_COLUMNS)
        vehicles = read_rows(self.directory / VEHICLES_FILE, REQUIRED_VEHICLE_COLUMNS)
        return translate(
            orders, vehicles, now=scenario_now(self.directory), source=self.directory.name
        )


def scenarios(data_dir: Path = DATA_DIR) -> list[str]:
    """Every scenario the web picker may offer: a ``scenario-*`` directory holding
    both CSVs. Sorted, so the list is stable between restarts."""
    return sorted(
        path.name
        for path in data_dir.glob(SCENARIO_GLOB)
        if (path / ORDERS_FILE).is_file() and (path / VEHICLES_FILE).is_file()
    )


def default_scenario(data_dir: Path = DATA_DIR) -> str:
    """What the picker starts on: ``XAS_SCENARIO`` if set, else the mixed
    scenario (both disturbances at once), else whatever exists."""
    named = _text(os.environ.get("XAS_SCENARIO"))
    available = scenarios(data_dir)
    if named:
        if named not in available:
            raise RuntimeError(f"XAS_SCENARIO={named!r} is not one of {available}")
        return named
    preferred = "scenario-mixed"
    if preferred in available:
        return preferred
    if not available:
        raise RuntimeError(f"no scenario directories under {data_dir} (looked for {SCENARIO_GLOB})")
    return available[0]


def get_source(scenario: str | None = None, data_dir: Path = DATA_DIR) -> DataSource:
    """The source for one session. ``scenario`` is the directory name the planner
    picked in the web form; unset falls back to `default_scenario`."""
    name = _text(scenario) or default_scenario(data_dir)
    directory = data_dir / name
    if not (directory / ORDERS_FILE).is_file():
        raise RuntimeError(f"{directory} has no {ORDERS_FILE}; available: {scenarios(data_dir)}")
    return ScenarioSource(directory=directory)


def census(pull: dict) -> str:
    """The funnel, as text — what the scenario held and what survived.

    The fastest way to see WHY a plan covers three orders out of twenty-five, and
    the meter to watch while a scenario is being re-carved.
    """
    meta = pull.get("meta", {})
    ex = meta.get("excluded", {})
    seen_o = ex.get("orders_seen", len(pull.get("orders", [])))
    kept_o = ex.get("orders_kept", len(pull.get("orders", [])))
    seen_v = ex.get("vehicles_seen", len(pull.get("vehicles", [])))
    kept_v = ex.get("vehicles_kept", len(pull.get("vehicles", [])))
    lines = [f"pull date {meta.get('now')}  source={meta.get('source', 'scenario')}"]
    lines.append(f"orders   {seen_o} read  ->  {kept_o} usable")
    for reason, n in (ex.get("order_drops") or {}).items():
        lines.append(f"           -{n:<5} {reason}")
    lines.append(f"vehicles {seen_v} read  ->  {kept_v} usable")
    for reason, n in (ex.get("vehicle_drops") or {}).items():
        lines.append(f"           -{n:<5} {reason}")
    for reason, n in (ex.get("link_drops") or {}).items():
        lines.append(f"links      -{n:<5} {reason}")
    unmatched = ex.get("orders_with_no_eligible_car") or []
    if unmatched:
        lines.append(f"no car for {len(unmatched)}: {', '.join(unmatched[:8])}")
    for c in meta.get("conflicts") or []:
        lines.append(f"CONFLICT   vehicle {c['vehicle']} claimed by {', '.join(c['orders'])}")
    free = sum(1 for o in pull.get("orders", []) if not o["VehicleCode"])
    lines.append(
        f"models in play: {len(meta.get('sales_models') or [])}"
        f"  |  holding no car: {free}"
        f"  |  already late: {len((pull.get('disruption') or {}).get('disrupted_orders') or [])}"
    )
    return "\n".join(lines)


def main() -> None:
    """``uv run python -m datasource --census`` — read the configured scenario and
    print the funnel. Read-only."""
    import argparse

    parser = argparse.ArgumentParser(description="Inspect a scenario's allocation pull")
    parser.add_argument("--scenario", help=f"directory under data/ (default: {SCENARIO_GLOB})")
    parser.add_argument("--census", action="store_true", help="print the read/filter funnel")
    parser.add_argument("--json", action="store_true", help="dump the whole pull")
    parser.add_argument("--list", action="store_true", help="list the scenarios available")
    args = parser.parse_args()

    if args.list:
        for name in scenarios():
            print(f"{name}{'  (default)' if name == default_scenario() else ''}")
        return

    pull = get_source(args.scenario).pull()
    if args.json:
        print(json.dumps(pull, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(census(pull))


if __name__ == "__main__":
    main()

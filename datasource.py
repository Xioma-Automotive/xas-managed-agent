"""Where the allocation pull comes from — a callable source, resolved HOST-SIDE.

This is DECIDE-7 made concrete. The rich pull the agent repairs used to ship
frozen inside the skill bundle (`data/pull.json`); now the host fetches it from a
**data source** at session start and mounts it into the sandbox as a file (see
`web.py`). The source is chosen by config:

  - ``ScenarioEngineSource`` — the fabricated dataset (the FAKE). Default, offline,
    byte-for-byte with the tests. This is what keeps the whole suite runnable with
    no network until a real XAS exists.
  - ``AppMcpSource`` — the real pull, read through the app MCP's own tools
    (``get_job_cards`` + ``get_vehicles``), then filtered and mapped into the
    contract by ``map_response``. ONE data seam for both lanes: the reporting lane
    already answers questions over these tools.

**The MCP projects.** Its tools return an allowlisted subset of each record, so
every field the solver needs has to be on that list — see
``docs/mcp-field-spec.md``. ``missing_projection()`` reports any that are absent,
because a field the MCP omits and a field the tenant never filled in produce the
same empty funnel and need opposite fixes.

**The agent does not make these calls.** The pull must be ONE frozen snapshot for
the invariant to hold, so it is fetched here before the session exists and mounted
as a file. The MCP tools the AGENT holds stay the reporting lane's.

**This module runs on our host, never in the sandbox** — same rule that keeps
`scenario_engine/`'s code out of the agent. HTTP and credentials live here, where
the sandbox can't see them; the agent only ever receives an already-fetched file.

The contract every source returns is the rich pull shape `flatten()` and
`alloc_tools.summarize()` already consume::

    {meta, vsos, vehicles, disruption}

Select with the ``XAS_DATA_SOURCE`` env var (``scenario`` | ``xas``).

This serves the ALLOCATION lane only. The REPORTING lane used to mount a
fabricated ``jobcards.json`` from here as well; it reads the live system through
the `xas-app-mcp` tools now, so nothing host-side fetches records.

The tenant TAXONOMY used to be mounted from here too. It now ships inside the
`xas-reporting` skill bundle instead (see `setup_agent.reporting_bundle`) — one tenant, so a
static file beats a per-session upload. That collapses the caller's choice of
dealership: DECIDE-16 in `xas_allocation.decisions` is the note to undo this the
day a second tenant exists.
"""

from __future__ import annotations

import collections
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import httpx

import appmcp_auth

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
DATASET_PATH = DATA_DIR / "pull.json"  # the derived rich pull: flatten's offline default
# What the fake fabricates: the app MCP's two response shapes, mapped by the same
# `map_response` the live pull uses. See docs/mcp-response-schema.md.
MCP_JOBCARDS_PATH = DATA_DIR / "mcp-jobcards.json"
MCP_VEHICLES_PATH = DATA_DIR / "mcp-vehicles.json"


@runtime_checkable
class DataSource(Protocol):
    """A callable pull. ``scope`` is a future fetch-filter (which customers /
    month / POs to fetch) — accepted now, ignored until the pull grows a
    parameter; see the plan's non-goals."""

    def pull(self, scope: dict | None = None) -> dict: ...


@dataclass
class ScenarioEngineSource:
    """The fake: the fabricated dataset, in the app MCP's own response shapes.

    ``scenario_engine`` writes ``data/mcp-jobcards.json`` and
    ``data/mcp-vehicles.json`` — the two payloads of
    `docs/mcp-response-schema.md` — and this source maps them with the SAME
    ``map_response`` the live pull uses. That is the point: the fake is
    substitutable for the MCP because it goes through one mapping, not a second
    one maintained alongside it (which is what `tests/test_invariant.py` rests
    on). ``regenerate`` re-fabricates in memory instead of reading the committed
    files."""

    jobcards_path: Path = MCP_JOBCARDS_PATH
    vehicles_path: Path = MCP_VEHICLES_PATH
    regenerate: bool = False
    seed: int = 20

    def pull(self, scope: dict | None = None) -> dict:
        if self.regenerate:
            from scenario_engine.generate import generate

            world = generate(seed=self.seed)["pull"]
        else:
            world = {
                "jobcards": json.loads(self.jobcards_path.read_text()),
                "vehicles": json.loads(self.vehicles_path.read_text()),
            }
        return map_world(world)


# --- the real source: collect (via the app MCP) -> filter -> translate --------
#
# The rows come from the app MCP's read tools, NOT from the gateway REST API: one
# data seam for both lanes. The MCP applies its own projection, so the fields
# below have to be on its allowlist — `docs/mcp-field-spec.md` is the list, and
# `missing_projection()` names any that are absent so a not-yet-widened MCP reads
# as a deployment gap rather than as empty data.

APPMCP_TOOL_ORDERS = "get_job_cards"
APPMCP_TOOL_VEHICLES = "get_vehicles"
PAGE_SIZE = 200  # the tools' own cap; a larger count returns empty, no error
ORDER_CLASSIFICATION = "VSO"

# Fields the pull cannot do without, per side. Checked against what actually came
# back: a name absent from EVERY row means the MCP is not returning it, which is a
# different problem from a tenant that has not filled it in.
# `jobitems` is in here on purpose. Its absence is the one gap the LINE-level
# check below cannot see: `missing_projection` reads nothing out of an empty list,
# so a projection that returns no jobitems at all reports no line gap while every
# card drops for `no_car_line` — "0 usable orders" with no reason attached, the
# exact confusion this function exists to prevent.
REQUIRED_CARD_FIELDS = ("DueDateTime", "EntryDateTime", "jobitems")
REQUIRED_LINE_FIELDS = ("JobItemCode", "JobItemType", "LineNum")
# `AvailableBy`, not `EtaDealer`: it is the arrival date `vehicle_eta` reads first,
# so it is the one whose absence empties the vehicle pool.
REQUIRED_VEHICLE_FIELDS = ("SalesModel", "AvailableBy")

# The jobitem type that is a CAR. A VSO's lines also carry configuration and
# parts rows, and the line's own type is the only thing that separates them —
# `Prices[].JobItemType` reads "SpareParts" on every row, cars included.
CAR_ITEM_TYPE = "ModelItem"

# Line statuses that are still live demand. "not closed" is the tool's own
# server-side filter; this is the backstop for a row it let through.
DEAD_LINE_STATUSES = frozenset({"Closed", "Cancelled", "Canceled"})

# The pull date is the DEALERSHIP's date, not UTC. `meta.now` drives the time
# fence, which is measured in whole days — so a UTC date rolling over three hours
# early would freeze or thaw an order a day out of step with the planner's own
# calendar. The gateway's own timestamps carry +03:00.
TENANT_TZ = ZoneInfo("Asia/Jerusalem")

# Which vehicles are even fetched. Statuses outside this set are committed
# (Customer, Reserved-*), historical (Used, Disabled) or not for sale (Demo), and
# a vehicle with NO status is skipped by the filter itself (DECIDE-3).
IN_SCOPE_STATUS_CODES = ("01", "02", "03")

# The future/real split, BY NAME — never by code alone. Code `02` is "On The Way"
# on 218 vehicles and 'Available For Sale ' (trailing space, hence .strip()) on
# 106 more, so the code merges a future car with a car on the lot: exactly the
# distinction allocation turns on. See skills/xas-reporting/index.md.
FUTURE_STATUS_NAMES = frozenset({"Ordered", "On The Way"})
REAL_STATUS_NAMES = frozenset({"In Stock", "Available For Sale"})


def _text(value: object) -> str:
    """A trimmed string, or "" — XAS sends absent, null and "  " for the same thing."""
    return str(value).strip() if value not in (None, "") else ""


def _date_part(value: object) -> str:
    """'2026-08-19T14:54:00Z' -> '2026-08-19'. "" when there is no date."""
    text = _text(value)
    return text[:10] if len(text) >= 10 and text[4] == "-" else ""


def status_bucket(vehicle: dict) -> str | None:
    """'future' | 'real' | None, from the status NAME (see FUTURE_STATUS_NAMES)."""
    name = _text((vehicle.get("Status") or {}).get("Name"))
    if name in FUTURE_STATUS_NAMES:
        return "future"
    if name in REAL_STATUS_NAMES:
        return "real"
    return None


def join_key(vehicle: dict) -> str:
    """The vehicle's side of the eligibility equality.

    ``SalesModel`` is the real join key: a VSO's ``SalesModelCode`` is a full
    trim/colour code (``T5040UECLMQ0009``) and matches it byte-for-byte, while
    ``ModelId.Code`` holds the model (``T5040``) and matches nothing. The
    fallback is kept only so a model-coded order can still find a car — it is
    the same hard equality either way.
    """
    return _text(vehicle.get("SalesModel")) or _text((vehicle.get("ModelId") or {}).get("Code"))


def vehicle_eta(vehicle: dict, now: date, bucket: str) -> str:
    """When this car is available to hand over.

    A real car is on the lot, so its eta is ``now``. A future car needs a real
    arrival date, and ``AvailableBy`` is the PRIMARY source: it is the field the
    tenant actually fills (19 vehicles fleet-wide vs 3 for ``EtaDealer``), so
    reading the schema's nominal field first left almost every future car
    undateable and therefore dropped. ``EtaDealer`` stays as the fallback — it
    is the field a delay is expected to move, so a car that carries it is read
    from it when ``AvailableBy`` is blank. No date at all means the car cannot be
    scheduled, so the vehicle is dropped rather than guessed at — a fabricated eta
    would move the plan.
    """
    if bucket == "real":
        return now.isoformat()
    return _date_part(vehicle.get("AvailableBy")) or _date_part(vehicle.get("EtaDealer"))


def card_lines(card: dict) -> list[dict]:
    """A card's jobitems, whatever the shape called them."""
    return list(card.get("jobitems") or card.get("JobItems") or [])


def _card_key(card: dict) -> str:
    """The order id. `JobKey` ("VSO-16") is what `flatten` keys on; the numeric
    ids are fallbacks, and `JobEntryNum` is an int on the wire."""
    return (
        _text(card.get("JobKey"))
        or _text(card.get("DMSJCNum"))
        or _text(card.get("JobEntryNum"))
        or _text(card.get("DMSJCEntry"))
    )


def _line_vehicle(card: dict, line: dict, n_lines: int) -> str | None:
    """The vehicle this line is currently allocated to, or None.

    Three places it can come from, most specific first:

    * the LINE's own ``VehicleId.Code`` — a hard binding to a real car;
    * the line's ``AllocatedVehicleCode`` — the fake's direct link to the future
      car its soft Alloc block stands for;
    * the CARD's ``VehicleDMSCode`` — the only candidate the live MCP offers,
      and it is one field for the whole card. Applied ONLY when the card has a
      single car line: with two, one header field cannot say which line it
      belongs to, and guessing would invent an allocation. That is the open
      question in `docs/mcp-response-schema.md` — resolving a line's allocation
      to a vehicle needs the VPO hop the pull does not make.
    """
    code = _text((line.get("VehicleId") or {}).get("Code"))
    if code:
        return code
    code = _text(line.get("AllocatedVehicleCode"))
    if code:
        return code
    if n_lines == 1:
        return _text(card.get("VehicleDMSCode")) or None
    return None


def car_lines_kept(card: dict) -> tuple[list[tuple[int, str, dict]], list[str]]:
    """A card's live CAR lines as ``(LineNum, model_code, line)``, plus one drop
    reason per rejected line.

    The single definition of "this line holds a car", because two places need it
    and a disagreement between them is silent: the conflict scan below and the
    translate step in ``map_response``. They must agree, and they must agree on
    the COUNT too — ``_line_vehicle`` falls back to the card's one
    ``VehicleDMSCode`` only for a single-car card, so counting a Configuration
    row as a car line would suppress that fallback in one place and not the
    other. The result: a double-booked vehicle invisible to ``meta.conflicts``
    but still linked as an allocation, which trips the solver's self-check on its
    own input.
    """
    kept: list[tuple[int, str, dict]] = []
    drops: list[str] = []
    for line in card_lines(card):
        if _text(line.get("JobItemType")) != CAR_ITEM_TYPE:
            drops.append("not_a_car_line")
            continue
        if line.get("IsDeleted"):
            drops.append("deleted_line")
            continue
        if _text(line.get("JobItemStatus")) in DEAD_LINE_STATUSES:
            drops.append("closed_line")
            continue
        # The LINE's model code. The card's own `SalesModelCode` is deliberately
        # not consulted: it disagrees with the line on real data, and the detail
        # shape does not carry it at all.
        code = _text(line.get("JobItemCode")) or _text(line.get("SalesModelCode"))
        if not code:
            drops.append("no_model_on_the_line")
            continue
        kept.append((int(line.get("LineNum") or 0), code, line))
    return kept, drops


def _claimed_vehicles(card: dict) -> list[tuple[int, str]]:
    """(LineNum, VehicleCode) for every allocation this card asserts — used to
    find a car two orders both claim, which is not a valid allocation."""
    lines, _ = car_lines_kept(card)
    out: list[tuple[int, str]] = []
    for num, _code, line in lines:
        code = _line_vehicle(card, line, len(lines))
        if code:
            out.append((num, code))
    return out


def map_response(
    orders: list[dict], vehicle_rows: list[dict], now: date, disruption: dict | None = None
) -> dict:
    """Raw XAS rows -> the rich pull contract. PURE: no network, no clock.

    ``orders`` is a list of VSO **job cards**, each carrying its own list of
    ``jobitems``; ``vehicle_rows`` is the flat supply pool. Both shapes are the app
    MCP's, and the fake fabricates the same shapes, so this is the ONE mapping
    both sources run through (`docs/mcp-response-schema.md`).

    Filter and translate in one pass, because the filter's *reasons* are part of
    the output: a plan over 1 of 25 sales orders that does not say so reads as
    the whole book. Everything dropped is counted by reason into
    ``meta.excluded``, and ``alloc_tools.summarize`` carries that into the
    agent's context.

    Grain: **one jobitem is one order**, keyed ``{JobKey}-{LineNum}``. The card
    supplies the promise and the customer; the LINE supplies the
    eligibility key (``JobItemCode``). The header's own ``SalesModelCode`` is not
    read — it disagrees with the line on real data, and the detail shape does not
    carry it at all.

    ``disruption`` is the fake's manifest of what it slipped. XAS records no such
    thing, so the real path passes nothing and only ``disrupted_orders`` — which
    IS derivable — comes back populated.
    """
    order_drops: collections.Counter = collections.Counter()
    line_drops: collections.Counter = collections.Counter()
    vehicle_drops: collections.Counter = collections.Counter()
    link_drops: collections.Counter = collections.Counter()

    # --- vehicles: in-scope status, a join key, and a date it can be counted on -
    vehicles: list[dict] = []
    for vehicle in vehicle_rows:
        bucket = status_bucket(vehicle)
        if bucket is None:
            vehicle_drops["out_of_scope_status"] += 1
            continue
        key = join_key(vehicle)
        if not key:
            vehicle_drops["no_model"] += 1
            continue
        eta = vehicle_eta(vehicle, now, bucket)
        if not eta:
            vehicle_drops["no_arrival_date"] += 1
            continue
        vehicles.append(
            {
                "VehicleCode": _text(vehicle.get("VehicleCode")),
                # The solver's binding, NOT the XAS field of the same name:
                # XAS `VehicleClassification` is Truck/Vehicle/InventoryVehicles,
                # a different axis. Hard vs soft comes from the status bucket.
                "VehicleClassification": "Vehicle" if bucket == "real" else "Future",
                "SalesModel": key,
                "ModelId": dict(vehicle.get("ModelId") or {}),
                "EtaDealer": eta,
                "Status": dict(vehicle.get("Status") or {}),
                "Vin": _text(vehicle.get("Vin")),
                "Description": _text(vehicle.get("Description")),
                "Make": (vehicle.get("Make") or {}).get("Name", "")
                if isinstance(vehicle.get("Make"), dict)
                else _text(vehicle.get("Make")),
                # Kept for provenance: which XAS pool the car came out of.
                "XasVehicleClassification": _text(vehicle.get("VehicleClassification")),
            }
        )

    # --- cards: a promise to be late against, and at least one live car line ---
    # (card, promise, [(line_num, code, line), ...])
    kept: list[tuple[dict, str, list[tuple[int, str, dict]]]] = []
    for card in orders:
        if card.get("isCanceled"):
            order_drops["cancelled"] += 1
            continue
        promise = _date_part(card.get("DueDateTime"))
        if not promise:
            order_drops["no_promised_date"] += 1
            continue
        lines, dropped = car_lines_kept(card)
        line_drops.update(dropped)
        if not lines:
            order_drops["no_car_line"] += 1
            continue
        kept.append((card, promise, lines))

    # --- prune to the reachable sub-problem ----------------------------------
    # A car no surviving order wants can never be allocated (eligibility is hard
    # equality), so dropping it is lossless and keeps the mounted file small.
    # If eligibility ever stops being equality, this pruning has to go.
    wanted = {code for _, _, lines in kept for _, code, _ in lines}
    reachable = [u for u in vehicles if u["SalesModel"] in wanted]
    vehicle_drops["no_order_wants_this_model"] = len(vehicles) - len(reachable)
    have = {u["SalesModel"] for u in reachable}
    # NOT a drop: an order with no matching car is real unfilled demand, and the
    # solver surfaces it as a backorder. Named so the agent can say which.
    unmatched = sorted(
        f"{_card_key(card)}-{num}"
        for card, _, lines in kept
        for num, code, _ in lines
        if code not in have
    )

    # --- allocation conflicts, over EVERY card, not just the kept ones --------
    # A vehicle claimed by two orders is not a valid matching and would trip the
    # solver's self-check on its INPUT, so a contested vehicle yields no
    # allocation for anyone; those orders become unallocated demand, which is what
    # they effectively are. The conflict is a finding a planner wants, so it
    # rides in meta rather than being swallowed.
    claims: dict[str, list[str]] = collections.defaultdict(list)
    for card in orders:
        for num, code in _claimed_vehicles(card):
            claims[code].append(f"{_card_key(card)}-{num}")
    conflicts = [
        {"vehicle": code, "orders": sorted(ids)}
        for code, ids in sorted(claims.items())
        if len(ids) > 1
    ]
    contested = {c["vehicle"] for c in conflicts}
    vehicle_ids = {u["VehicleCode"] for u in reachable}
    real_ids = {u["VehicleCode"] for u in reachable if u["VehicleClassification"] == "Vehicle"}

    # --- translate ------------------------------------------------------------
    vsos: list[dict] = []
    for card, promise, lines in kept:
        owner = (card.get("Accounts") or {}).get("Owner") or {}
        items: list[dict] = []
        for num, code, line in lines:
            link = _line_vehicle(card, line, len(lines))
            if link and link in contested:
                link = None
                link_drops["double_booked_vehicle"] += 1
            elif link and link not in vehicle_ids:
                link = None
                link_drops["vehicle_out_of_scope"] += 1
            # A received car is a hard binding (VGR); one still on order is soft (VPO).
            alloc_source = ("VGR" if link in real_ids else "VPO") if link else ""
            item = {
                "LineNum": num,
                "SalesModelCode": code,
                "Label": _text(line.get("Label")) or code,
                "JobItemType": CAR_ITEM_TYPE,
                "Prices": list(line.get("Prices") or []),
            }
            if link:
                item["VehicleId"] = {"Code": link}
                item["AllocSourceClassification"] = alloc_source
            items.append(item)
        vsos.append(
            {
                "JobKey": _card_key(card),
                "DMSJCEntry": _text(card.get("DMSJCEntry")),
                "DeliveryDate": promise,
                "EntryDate": _date_part(card.get("EntryDateTime")),
                "JobStatus": _text((card.get("JobStatus") or {}).get("Label")),
                "Accounts": {
                    "Owner": {
                        "AccountName": _text(owner.get("AccountName")),
                        "AccountUUID": _text(owner.get("AccountUUID")),
                        "AccountDMSCode": _text(owner.get("AccountDMSCode")),
                    }
                },
                "JobItems": items,
            }
        )

    # --- the disruption is DERIVED, not declared ------------------------------
    # What slips is a VEHICLE — a VPO/VGR shipment runs late, so its cars do. XAS
    # records no "shipment slipped 21 days" manifest, so the affected DEMAND is
    # derived: an allocated line whose car now lands past its promise. An order
    # with no car needs no help — partition already frees anything unassigned.
    #
    # Named at ORDER grain — one car line, one car. `flatten` re-derives this
    # authoritatively; the two must agree, and this copy is what
    # `alloc_tools.summarize` shows the agent at pull time.
    eta_by_id = {u["VehicleCode"]: u["EtaDealer"] for u in reachable}
    disrupted = sorted(
        f"{v['JobKey']}-{item['LineNum']}"
        for v in vsos
        for item in v["JobItems"]
        if (item.get("VehicleId") or {}).get("Code")
        and eta_by_id.get(item["VehicleId"]["Code"], "") > v["DeliveryDate"]
    )
    manifest = dict(disruption or {})

    return {
        "meta": {
            "now": now.isoformat(),
            "source": "xas",
            "sales_models": sorted(wanted),
            "excluded": {
                "orders_seen": len(orders),
                "orders_kept": len(vsos),
                "lines_kept": sum(len(v["JobItems"]) for v in vsos),
                # The real demand: a line wanting 3 cars is 3 orders once
                # `flatten` expands it. Counting lines here and cars there is how
                # a qty-heavy book silently under-reports what it owes.
                "order_drops": dict(sorted(order_drops.items())),
                "line_drops": dict(sorted(line_drops.items())),
                "vehicles_seen": len(vehicle_rows),
                "vehicles_kept": len(reachable),
                "vehicle_drops": {k: v for k, v in sorted(vehicle_drops.items()) if v},
                "link_drops": dict(sorted(link_drops.items())),
                "orders_with_no_eligible_car": unmatched,
            },
            "conflicts": conflicts,
        },
        "vsos": vsos,
        "vehicles": reachable,
        "disruption": {
            # `delay_days` is the WORST slip; `delay_tiers` is the split by size,
            # because one disruption is normally several shipments slipping by
            # different amounts. `delay_label` is the phrase for a one-liner.
            "delay_days": manifest.get("delay_days", 0),
            "delay_label": manifest.get("delay_label", ""),
            "delay_tiers": manifest.get("delay_tiers", {}),
            "delayed_vehicles": manifest.get("delayed_vehicles", []),
            "disrupted_orders": disrupted,
        },
    }


def map_world(world: dict) -> dict:
    """The FAKE's two payloads -> the rich pull. The one reader of ``_scenario``.

    ``world`` is ``{"jobcards": {...}, "vehicles": {...}}`` — the two response
    shapes `scenario_engine` fabricates, read either off the committed files or
    straight out of the generator. Everything real goes through ``map_response``
    unchanged; the only fake-specific part is the ``_scenario`` sidecar, which
    carries the two things an MCP response cannot: the frozen pull date (the fake
    is deterministic, so it can never be ``today()``) and the manifest of what
    the scenario slipped. Keeping that key here means the payloads themselves
    stay honest to `docs/mcp-response-schema.md`.
    """
    cards = world["jobcards"]
    scenario = cards.get("_scenario") or {}
    return map_response(
        cards["list"],
        world["vehicles"]["records"],
        date.fromisoformat(scenario["now"]),
        disruption=scenario.get("disruption"),
    )


def missing_projection(rows: list[dict], required: tuple[str, ...]) -> list[str]:
    """Required names that appear on NO row — i.e. the MCP is not returning them.

    The distinction this draws is the whole reason it exists: a field the tenant
    has not filled in shows up on *some* rows and is a data-entry problem, while a
    field the MCP's allowlist omits shows up on *none* and is a deployment
    problem. Both look identical in the funnel ("0 usable orders") without this.
    """
    if not rows:
        return []
    return [name for name in required if not any(name in row for row in rows)]


@dataclass
class AppMcpSource:
    """The real pull, through the app MCP's read tools.

    One data seam for both lanes: the reporting lane already answers questions
    over these tools, so allocation reads the same surface rather than a second,
    private path into the gateway.

    Two things to know. The MCP **projects** — it returns an allowlisted subset of
    each record — so every field the solver needs must be on that allowlist;
    `docs/mcp-field-spec.md` is the list, and `pull` reports any that are absent
    instead of silently producing an empty plan. And the bearer is the same JWE
    the reporting lane uses (`appmcp_auth.mint` over a gateway login), minted
    here per pull; the outer token lasts a week, the inner one 30 minutes, so a
    freshly minted pair is always inside both.

    This runs on our host, never in the sandbox. The agent does not make these
    calls — the pull has to be ONE frozen snapshot for
    `plan = pure_function(snapshot, skill, override)` to hold, so it is fetched
    before the session exists and mounted as a file. The agent's own MCP tools
    stay the reporting lane's.
    """

    url: str = appmcp_auth.APPMCP_URL
    timeout: float = 120.0

    def pull(self, scope: dict | None = None) -> dict:
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as http:
            bearer = appmcp_auth.mint(self._login(http))
            orders = self._collect(
                http,
                bearer,
                APPMCP_TOOL_ORDERS,
                "jobCards",
                {"JobClassification": ORDER_CLASSIFICATION},
            )
            vehicles = self._collect(
                http,
                bearer,
                APPMCP_TOOL_VEHICLES,
                "vehicles",
                {"status.code": {"$in": list(IN_SCOPE_STATUS_CODES)}},
            )

        lines = [line for card in orders for line in card_lines(card)]
        gaps = {
            APPMCP_TOOL_ORDERS: missing_projection(orders, REQUIRED_CARD_FIELDS),
            f"{APPMCP_TOOL_ORDERS}.jobitems": missing_projection(lines, REQUIRED_LINE_FIELDS),
            APPMCP_TOOL_VEHICLES: missing_projection(vehicles, REQUIRED_VEHICLE_FIELDS),
        }
        rich = map_response(orders, vehicles, datetime.now(tz=TENANT_TZ).date())
        if any(gaps.values()):
            # Not an exception: a partial pull is still worth mounting, and the
            # planner-facing note explains itself. But it must be impossible to
            # mistake for "the tenant has no data".
            rich["meta"]["projection_gaps"] = {k: v for k, v in gaps.items() if v}
        return rich

    def _login(self, http: httpx.Client) -> str:
        """The inner `__DMS_app_token`, from the gateway login shared with the
        MCP minter. `forceLogin` invalidates other sessions for this user."""
        url, body = appmcp_auth.login_request()
        response = http.post(url, json=body)
        response.raise_for_status()
        token = response.cookies.get(appmcp_auth.USER_TOKEN_COOKIE)
        if not token:
            raise RuntimeError(
                f"gateway login returned {response.status_code} without a "
                f"{appmcp_auth.USER_TOKEN_COOKIE} cookie"
            )
        return token

    def _collect(
        self, http: httpx.Client, bearer: str, tool: str, key: str, filt: dict
    ) -> list[dict]:
        """Every page of one read tool. `count` caps at 200 — asking for more
        returns an empty result with no error, so page rather than guess high."""
        rows: list[dict] = []
        page = 1
        while True:
            result = self._call(
                http,
                bearer,
                tool,
                {
                    "filter": filt,
                    "paging": {"page": page, "count": PAGE_SIZE},
                },
            )
            batch = list(result.get(key) or [])
            rows += batch
            total = result.get("totalCount")
            if len(batch) < PAGE_SIZE or (total is not None and len(rows) >= total):
                return rows
            page += 1

    def _call(self, http: httpx.Client, bearer: str, tool: str, arguments: dict) -> dict:
        """One JSON-RPC tools/call. The transport may answer as a single JSON body
        or as an SSE frame, and the payload is a JSON document inside a text
        content block — so unwrap all three layers before returning."""
        response = http.post(
            self.url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {bearer}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        )
        response.raise_for_status()
        body = response.text
        if "data: " in body:
            body = next(ln[6:] for ln in body.splitlines() if ln.startswith("data: "))
        payload = json.loads(body)
        if "error" in payload:
            raise RuntimeError(f"app MCP {tool} failed: {payload['error']}")
        result = payload.get("result") or {}
        text = "".join(part.get("text", "") for part in result.get("content") or [])
        if result.get("isError"):
            # A 200 carrying isError is the INNER token having expired, which
            # reads nothing like a 401. Say which layer failed.
            raise RuntimeError(f"app MCP {tool} returned an error result: {text[:200]}")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise RuntimeError(f"app MCP {tool} returned non-JSON: {text[:200]}") from exc


def get_source() -> DataSource:
    """Pick the data source from the environment. Defaults to the fabricated
    dataset so dev and CI run with no network and no credentials."""
    kind = (os.environ.get("XAS_DATA_SOURCE") or "scenario").strip().lower()
    if kind == "xas":
        # The gateway authenticates with a session cookie from its own login, so
        # the credential is the login — the same three vars the app-MCP minter
        # uses. There is no static XAS_API_TOKEN to hold.
        missing = [name for name in appmcp_auth.REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                "XAS_DATA_SOURCE=xas reads the live system through the app MCP, which "
                "needs the same host-side config as the reporting lane (never shipped to "
                f"the sandbox); missing: {', '.join(missing)}"
            )
        return AppMcpSource(url=os.environ.get("APPMCP_URL") or appmcp_auth.APPMCP_URL)
    if kind == "scenario":
        return ScenarioEngineSource()
    raise RuntimeError(f"unknown XAS_DATA_SOURCE {kind!r} (expected 'scenario' or 'xas')")


def census(rich: dict) -> str:
    """The funnel, as text — what the real source collected and what survived.

    The tool for watching the pilot data fill in: run it after editing VSOs in
    the app and the kept counts should climb. Also the fastest way to see WHY a
    plan covers three orders out of twenty-five.
    """
    meta = rich.get("meta", {})
    ex = meta.get("excluded", {})
    seen_o = ex.get("orders_seen", len(rich.get("vsos", [])))
    kept_o = ex.get("orders_kept", len(rich.get("vsos", [])))
    seen_u = ex.get("vehicles_seen", len(rich.get("vehicles", [])))
    kept_u = ex.get("vehicles_kept", len(rich.get("vehicles", [])))
    lines = [f"pull date {meta.get('now')}  source={meta.get('source', 'scenario')}"]
    for tool, names in (meta.get("projection_gaps") or {}).items():
        lines.append(f"!! {tool} does not return: {', '.join(names)}  (widen the MCP)")
    lines.append(f"orders   {seen_o} collected  ->  {kept_o} usable")
    for reason, n in (ex.get("order_drops") or {}).items():
        lines.append(f"           -{n:<5} {reason}")
    lines.append(f"vehicles {seen_u} collected  ->  {kept_u} usable")
    for reason, n in (ex.get("vehicle_drops") or {}).items():
        lines.append(f"           -{n:<5} {reason}")
    for reason, n in (ex.get("link_drops") or {}).items():
        lines.append(f"links      -{n:<5} {reason}")
    unmatched = ex.get("orders_with_no_eligible_car") or []
    if unmatched:
        lines.append(f"no car for {len(unmatched)}: {', '.join(unmatched[:8])}")
    for c in meta.get("conflicts") or []:
        lines.append(f"CONFLICT   vehicle {c['vehicle']} claimed by {', '.join(c['orders'])}")
    lines.append(
        f"models in play: {len(meta.get('sales_models') or [])}"
        f"  |  already late: {len((rich.get('disruption') or {}).get('disrupted_orders') or [])}"
    )
    return "\n".join(lines)


def main() -> None:
    """``uv run python -m datasource --census`` — pull from the configured source
    and print the funnel. Read-only; the pull is a GET."""
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the configured allocation pull")
    parser.add_argument("--census", action="store_true", help="print the collect/filter funnel")
    parser.add_argument("--json", action="store_true", help="dump the whole rich pull")
    args = parser.parse_args()

    rich = get_source().pull()
    if args.json:
        print(json.dumps(rich, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(census(rich))


if __name__ == "__main__":
    main()

"""The allocation agent's data tool: pull a snapshot to repair.

ONE contract, ONE definition. ``PULL_TOOL`` is the ``custom`` tool
``setup_agent.py`` declares on the agent; ``pull_allocation_snapshot``
is the implementation ``web.py`` registers to answer it. Both are derived from
the constants below, so the declaration and the implementation cannot drift into
the failure this arrangement invites: a custom tool call whose name nothing
answers parks the session on a ``requires_action`` idle, which never times out.

**Cloud sandbox.** The tool runs on *our* host; the agent runs in Anthropic's.
Everything this returns crosses into the agent's context.

**Pull ships files, not rows.** The pull is a scenario directory's two CSVs
(``datasource.get_source()``), translated HOST-SIDE into two JSON payloads.
``web.py`` mounts them into the sandbox at ``ORDERS_MOUNT_PATH`` and
``VEHICLES_MOUNT_PATH``; this tool returns only a summary plus a self-locating
``flatten`` command that reads those two files and writes ``snapshot.json``. The
rows travel as mounted files, never through the context window.

DECIDE-7: the source of the rows (bundled file → app MCP → the export's CSVs) is
the only thing that has ever changed here; the summary + flatten contract is not.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from anthropic.lib.tools import beta_async_tool


def parse_date(value: str) -> date:
    """'2026-09-20' -> date. Local to this module so the summary needs no import
    from the solver package, which lives on the other side of the mount."""
    return date.fromisoformat(str(value)[:10])


REPO_ROOT = Path(__file__).resolve().parent

SNAPSHOT_FILENAME = "snapshot.json"

# Where web.py mounts the pull inside the sandbox — two files, because the export
# has two row streams and translating them into one nested document would only
# make `flatten` take it apart again. The flatten command reads both from here.
ORDERS_MOUNT_PATH = "/workspace/orders.json"
VEHICLES_MOUNT_PATH = "/workspace/vehicles.json"
MOUNT_PATHS = (ORDERS_MOUNT_PATH, VEHICLES_MOUNT_PATH)

# Where the platform ACTUALLY materializes a mounted resource. Observed
# 2026-08-18: a resource requested at /workspace/pull.json appeared at
# /mnt/session/uploads/workspace/pull.json, and /workspace held only `skills`.
# The docs say mount_path is absolute, so treat this as a location we resolve
# rather than one we assume — a wrong guess fails the pull, and the agent then
# either improvises (copying files around, as it did) or gives up. Both break
# "same snapshot every turn".
UPLOAD_PREFIX = "/mnt/session/uploads"


def mount_candidates(mount_path: str) -> list[str]:
    """Every place a resource mounted at ``mount_path`` might really be, in order."""
    return [mount_path, f"{UPLOAD_PREFIX}{mount_path}"]


TOOL_NAME = "pull_allocation_snapshot"

TOOL_DESCRIPTION = (
    "Pull the current allocation snapshot: sales orders, the pool of planned "
    "vehicles, the complete current allocation, and the disruption to repair. "
    "Returns a summary plus the exact `flatten` command to materialize the full "
    "snapshot as snapshot.json in your sandbox — run that command before solving, "
    "then read the file from your solver code, never into this conversation. Call "
    "once at the start of a repair cycle; the same pull backs every turn, so "
    "re-applying the same combined override is what makes a turn reproducible. "
    "Prototype (DECIDE-7): the real XAS pull does not exist yet, so the rows come "
    "from the scenario-engine fake by default; either way the host mounts them as "
    "a file the flatten command reads."
)

# The pull takes no parameters: the scenario is pre-fabricated and bundled, so
# there is nothing for the agent to tune. (A future multi-scenario build would
# add a 'scenario' selector here.)
PULL_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

# What the agent declares. Kept next to the implementation on purpose.
PULL_TOOL: dict[str, Any] = {
    "type": "custom",
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "input_schema": PULL_TOOL_INPUT_SCHEMA,
}


def flatten_command(
    orders_path: str = ORDERS_MOUNT_PATH, vehicles_path: str = VEHICLES_MOUNT_PATH
) -> str:
    """The one-liner that flattens the two mounted files inside the sandbox.

    Two kinds of location, both handled explicitly:
      * the **solver package** ships in the skill bundle, landing wherever the
        platform materializes skills — so we self-locate ``xas_allocation/
        flatten.py``. Search bases are **explicit and never** ``/`` (an unbounded
        ``rglob`` from ``/`` once swept the whole container and killed the shell).
      * the **two payloads** are mounted by the host at paths WE choose — but the
        platform may materialize them under ``/mnt/session/uploads``, so each is
        resolved against ``mount_candidates`` rather than assumed. Still no
        searching: the lists are short, explicit and bounded.

    The command fails fast, naming the file it could not find — a silent miss
    would let the sandbox solve against half the data, which is worse than not
    solving. ``snapshot.json`` is written in the working directory; single line,
    single quoting for a clean paste.
    """
    return (
        'python -c "'
        "import sys, json, pathlib; "
        "root = pathlib.Path('/'); "
        "bases = [p for p in (pathlib.Path.cwd(), pathlib.Path('/workspace')) "
        "if p.is_dir() and p != root]; "
        "hit = next((h for b in bases for h in b.rglob('xas_allocation/flatten.py')), None); "
        "sys.exit('xas_allocation not found under ' + str(bases)) if hit is None else None; "
        "sys.path.insert(0, str(hit.parent.parent)); "
        "from xas_allocation.flatten import flatten_paths; "
        # `next(gen, sys.exit(...))` would NOT work: a default argument is
        # evaluated eagerly, so the exit fires before the lookup runs. Pick, then
        # check — and name which of the two files is missing.
        "pick = lambda names: next((n for n in names if pathlib.Path(n).is_file()), ''); "
        f"cands = ({mount_candidates(orders_path)!r}, {mount_candidates(vehicles_path)!r}); "
        "orders, vehicles = pick(cands[0]), pick(cands[1]); "
        "missing = [c[0] for c, got in zip(cands, (orders, vehicles)) if not got]; "
        "sys.exit('pull data not mounted at ' + str(missing)) if missing else None; "
        f"out = pathlib.Path.cwd() / '{SNAPSHOT_FILENAME}'; "
        "json.dump(flatten_paths(orders, vehicles).as_dict(), open(out,'w'), "
        "indent=2, sort_keys=True); "
        "print('wrote ' + str(out))"
        '"'
    )


def summarize(pull: dict) -> dict[str, Any]:
    """The part of the pull that crosses into the agent's context.

    Counts, provenance and the flatten command — never rows. What the source
    filtered OUT is in here on purpose: the turn-1 reply has to account for the
    orders that are not in the plan, and a plan over 1 of 25 that doesn't say so
    reads as the whole book.
    """
    meta = pull.get("meta", {})
    orders = pull.get("orders", [])
    vehicles = pull.get("vehicles", [])
    late = list((pull.get("disruption") or {}).get("disrupted_orders") or [])

    held = {o["VehicleCode"] for o in orders if o.get("VehicleCode")}
    eta_of = {v["VehicleCode"]: v["EtaDealer"] for v in vehicles}
    # The spread of how late things are. This REPLACED the fake's delay manifest
    # ("30 days on 25 vehicles"): the export records no such thing, so the only
    # honest summary of a disruption is what the dates now say.
    gaps = sorted(
        (parse_date(eta_of[o["VehicleCode"]]) - parse_date(o["DeliveryDate"])).days
        for o in orders
        if o["OrderId"] in set(late) and o.get("VehicleCode") in eta_of
    )

    return {
        "flatten": flatten_command(),
        "snapshot_path": SNAPSHOT_FILENAME,
        "now": meta.get("now"),
        "scenario": meta.get("source"),
        "excluded": meta.get("excluded", {}),
        "conflicts": meta.get("conflicts", []),
        "orders": len(orders),  # one order row is one wanted CAR
        "orders_holding_a_car": len(held),
        "orders_holding_no_car": sum(1 for o in orders if not o.get("VehicleCode")),
        "supply": len(vehicles),  # the car pool: free ∪ currently allocated
        "free_supply": sum(1 for v in vehicles if v["VehicleCode"] not in held),
        "sales_models": meta.get("sales_models", []),
        "late_orders": len(late),
        "late_order_ids": late,
        "days_late": (
            {"min": gaps[0], "median": gaps[len(gaps) // 2], "max": gaps[-1]} if gaps else {}
        ),
    }


RichProvider = Callable[[], dict | Awaitable[dict]]


def make_pull_tool(get_rich: RichProvider):
    """Build the pull tool over a per-session data provider.

    ``get_rich`` returns the rich pull for this session (sync or async) — web.py
    closes it over the session's fetched-and-mounted data. The tool answers with
    only the summary + flatten command; the rows never cross the transcript.

    The name / description / schema come from the module constants, so the
    declared ``PULL_TOOL`` and this implementation stay ONE contract — a drift
    there is a custom tool nothing answers, which parks the session forever.
    """

    async def pull_allocation_snapshot() -> str:
        rich = get_rich()
        if inspect.isawaitable(rich):
            rich = await rich
        return json.dumps(summarize(rich), indent=2)

    return beta_async_tool(
        pull_allocation_snapshot,
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        input_schema=PULL_TOOL_INPUT_SCHEMA,
    )


def _default_rich() -> dict:
    """Host-side default provider: the default scenario directory. Used by the
    module-level tool for tests and local runs; web.py builds per-session tools
    over the scenario the planner picked, via ``make_pull_tool``."""
    import datasource

    return datasource.get_source().pull()


# A ready-to-use instance over the default source, for host-side tests/local runs.
pull_allocation_snapshot = make_pull_tool(_default_rich)

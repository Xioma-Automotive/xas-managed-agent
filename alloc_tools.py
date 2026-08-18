"""The allocation agent's data tool: pull a snapshot to repair.

ONE contract, ONE definition. ``PULL_TOOL`` is the ``custom`` tool
``setup_allocation_agent.py`` declares on the agent; ``pull_allocation_snapshot``
is the implementation ``web.py`` registers to answer it. Both are derived from
the constants below, so the declaration and the implementation cannot drift into
the failure this arrangement invites: a custom tool call whose name nothing
answers parks the session on a ``requires_action`` idle, which never times out.

**Cloud sandbox.** The tool runs on *our* host; the agent runs in Anthropic's.
Everything this returns crosses into the agent's context.

**Pull ships a file, not the rows.** The rich pull comes from a callable data
source (``datasource.get_source()`` — the scenario-engine fake, or the real XAS
endpoint), resolved HOST-SIDE. ``web.py`` fetches it at session start and mounts
it into the sandbox as a file at ``MOUNT_PATH``; this tool returns only a summary
plus a self-locating ``flatten`` command that reads that mounted file. The agent
runs the command to flatten the rich data into ``snapshot.json``. The rows travel
as a mounted file, never through the context window.

DECIDE-7: the source of the rows (bundled file → callable endpoint) is the only
thing that changed; the summary + flatten contract is unchanged.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from anthropic.lib.tools import beta_async_tool

REPO_ROOT = Path(__file__).resolve().parent

SNAPSHOT_FILENAME = "snapshot.json"

# Where web.py mounts the live pull inside the sandbox. The flatten command reads
# the rich data from here; web.py mounts the uploaded pull at the same path.
MOUNT_PATH = "/workspace/pull.json"

TOOL_NAME = "pull_allocation_snapshot"

TOOL_DESCRIPTION = (
    "Pull the current allocation snapshot: sales orders, the pool of planned "
    "vehicles, the complete incumbent allocation, and the disruption to repair. "
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


def flatten_command(pull_path: str = MOUNT_PATH) -> str:
    """The one-liner that flattens the mounted pull inside the sandbox.

    Two locations, both handled explicitly:
      * the **solver package** ships in the skill bundle, landing wherever the
        platform materializes skills — so we self-locate ``xas_allocation/
        flatten.py``. Search bases are **explicit and never** ``/`` (an unbounded
        ``rglob`` from ``/`` once swept the whole container and killed the shell).
      * the **rich pull** is mounted by the host at ``pull_path`` (a path WE
        choose), so we read it directly rather than searching for it.

    The command fails fast with a message if either is missing — a silent miss
    would let the sandbox solve against the wrong (or no) data. ``snapshot.json``
    is written in the working directory; single line, single quoting for a clean
    paste.
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
        "from xas_allocation.flatten import flatten_path; "
        f"src = pathlib.Path('{pull_path}'); "
        "sys.exit('pull data not found at ' + str(src)) if not src.exists() else None; "
        f"out = pathlib.Path.cwd() / '{SNAPSHOT_FILENAME}'; "
        "json.dump(flatten_path(src).as_dict(), open(out,'w'), indent=2, sort_keys=True); "
        "print('wrote ' + str(out))"
        '"'
    )


def _incumbent_count(item: dict) -> bool:
    """Whether a VSO jobitem is currently allocated (hard VehicleId or soft Alloc)."""
    return bool((item.get("VehicleId") or {}).get("Code") or item.get("AllocatedVehicleCode"))


def summarize(rich: dict) -> dict[str, Any]:
    """The part of the pull that crosses into the agent's context."""
    meta = rich.get("meta", {})
    vehicles = rich.get("vehicles", [])
    vsos = rich.get("vsos", [])
    disruption = rich.get("disruption", {}) or {}

    rows = [item for vso in vsos for item in vso.get("JobItems", [])]
    by_class: dict[str, int] = {}
    for v in vehicles:
        c = v.get("VehicleClassification", "Vehicle")
        by_class[c] = by_class.get(c, 0) + 1

    # §6 steering contract: resolve a dealer name in the planner's instruction to
    # the customer_id the override object carries. Built from the VSOs in play.
    customers: dict[str, dict] = {}
    for vso in vsos:
        owner = (vso.get("Accounts") or {}).get("Owner") or {}
        name = owner.get("AccountName", "")
        cid = owner.get("AccountUUID", "")
        prio = (vso.get("JobPriority") or {}).get("Code", "?")
        if name:
            customers.setdefault(name, {"customer_id": cid, "priority": prio})

    return {
        "flatten": flatten_command(),
        "snapshot_path": SNAPSHOT_FILENAME,
        "now": meta.get("now"),
        "sales_orders": len(vsos),
        "orders": len(rows),  # VSO car lines — the allocatable grain
        "supply": len(vehicles),  # the vehicle pool: real ∪ future
        "supply_by_classification": dict(sorted(by_class.items())),
        "incumbent_assignments": sum(1 for r in rows if _incumbent_count(r)),
        "sales_models": meta.get("sales_models", []),
        "disruption": {
            "delayed_model": disruption.get("delayed_model"),
            "delay_days": disruption.get("delay_days"),
            "delayed_vehicles": len(disruption.get("delayed_vehicles", [])),
        },
        "disrupted_orders": len(disruption.get("disrupted_orders", [])),
        "disrupted_order_ids": disruption.get("disrupted_orders", []),
        "customers": dict(sorted(customers.items())),
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
    """Host-side default provider: the configured data source (the scenario-engine
    fake unless XAS_DATA_SOURCE=xas). Used by the module-level tool for tests and
    local runs; web.py builds per-session tools via ``make_pull_tool``."""
    import datasource

    return datasource.get_source().pull()


# A ready-to-use instance over the default source, for host-side tests/local runs.
pull_allocation_snapshot = make_pull_tool(_default_rich)

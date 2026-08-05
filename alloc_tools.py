"""The allocation agent's data tool: pull a snapshot to repair.

ONE contract, ONE definition. ``PULL_TOOL`` is the ``custom`` tool
``setup_allocation_agent.py`` declares on the agent; ``pull_allocation_snapshot``
is the implementation ``web.py`` registers to answer it. Both are derived from
the constants below, so the declaration and the implementation cannot drift into
the failure this arrangement invites: a custom tool call whose name nothing
answers parks the session on a ``requires_action`` idle, which never times out.

**Cloud sandbox.** The tool runs on *our* host; the agent runs in Anthropic's.
Everything this returns crosses into the agent's context.

**Pull ships data, not a seed.** The rich dataset is fabricated by the standalone
``scenario_engine/`` (outside the agent), whose code does NOT live in the
sandbox — so the agent cannot regenerate it from a seed. Instead the dataset file
is bundled INTO the skill (like the solver package), and the tool returns a
summary plus a self-locating ``flatten`` command. The agent runs that command to
flatten the bundled rich data into ``snapshot.json`` — the same transport shape
as before, transforming rich→snapshot instead of seed→snapshot. The rows never
pass through the context window.

DECIDE-7: when the real XAS pull exists this reads it instead of a bundled file;
the summary + flatten contract stays, only the source of the rows changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anthropic.lib.tools import beta_async_tool

REPO_ROOT = Path(__file__).resolve().parent
DATASET_PATH = REPO_ROOT / "data" / "pull.json"

SNAPSHOT_FILENAME = "snapshot.json"

TOOL_NAME = "pull_allocation_snapshot"

TOOL_DESCRIPTION = (
    "Pull the current allocation snapshot: sales orders, the pool of planned "
    "vehicles, the complete incumbent allocation, and the disruption to repair. "
    "Returns a summary plus the exact `flatten` command to materialize the full "
    "snapshot as snapshot.json in your sandbox — run that command before solving, "
    "then read the file from your solver code, never into this conversation. Call "
    "once at the start of a repair cycle; the same bundled dataset backs every "
    "turn, so the ledger replay is what makes a turn reproducible. Prototype "
    "(DECIDE-7): the real XAS pull does not exist yet, so the rows are fabricated "
    "by the scenario engine and shipped inside the skill."
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


def flatten_command() -> str:
    """The one-liner that flattens the bundled dataset inside the sandbox.

    Self-locating on purpose, exactly like the old materialize command: the
    solver + dataset ship in the skill bundle, so they land wherever the platform
    materializes skills — not a path we can name from here. The search bases are
    **explicit and never** ``/`` (an unbounded ``rglob`` from ``/`` once swept the
    whole container and killed the shell). We locate ``xas_allocation/flatten.py``,
    put its skill dir on the path, and call ``flatten_default()`` — which finds the
    bundled ``data/pull.json`` relative to its own location.

    ``snapshot.json`` is written in the working directory and the command prints
    its absolute path. Single line, single quoting level for a clean paste.
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
        "from xas_allocation.flatten import flatten_default; "
        f"out = pathlib.Path.cwd() / '{SNAPSHOT_FILENAME}'; "
        "json.dump(flatten_default().as_dict(), open(out,'w'), indent=2, sort_keys=True); "
        "print('wrote ' + str(out))"
        '"'
    )


def summarize(rich: dict) -> dict[str, Any]:
    """The part of the pull that crosses into the agent's context."""
    meta = rich.get("meta", {})
    supply = rich.get("supply", [])
    sos = rich.get("sos", [])
    disruption = rich.get("disruption", {}) or {}

    rows = [row for so in sos for row in so.get("rows", [])]
    by_kind: dict[str, int] = {}
    for s in supply:
        by_kind[s.get("kind", "vehicle")] = by_kind.get(s.get("kind", "vehicle"), 0) + 1

    # §6 steering contract: resolve a dealer name in the planner's instruction to
    # the customer_id the override object carries. Built from the SOs in play.
    customers: dict[str, dict] = {}
    for so in sos:
        prio = so["rows"][0]["priority"] if so.get("rows") else "?"
        customers.setdefault(so["customer"], {"customer_id": so["customer_id"], "priority": prio})

    return {
        "flatten": flatten_command(),
        "snapshot_path": SNAPSHOT_FILENAME,
        "now": meta.get("now"),
        "sales_orders": len(sos),
        "orders": len(rows),  # vehicle order rows — the allocatable grain
        "supply": len(supply),
        "supply_by_kind": dict(sorted(by_kind.items())),
        "incumbent_assignments": sum(1 for r in rows if r.get("current_supply_id")),
        "sales_models": meta.get("sales_models", []),
        "disruption": {
            "po": disruption.get("po"),
            "delay_days": disruption.get("delay_days"),
            "delayed_supply": len(disruption.get("delayed_supply", [])),
        },
        "disrupted_orders": len(disruption.get("disrupted_orders", [])),
        "disrupted_order_ids": disruption.get("disrupted_orders", []),
        "customers": dict(sorted(customers.items())),
    }


def load_dataset(path: Path = DATASET_PATH) -> dict:
    return json.loads(path.read_text())


@beta_async_tool(
    name=TOOL_NAME,
    description=TOOL_DESCRIPTION,
    input_schema=PULL_TOOL_INPUT_SCHEMA,
)
async def pull_allocation_snapshot() -> str:
    """Read the bundled rich dataset here, return the summary + flatten command.

    Async because the session tool runner is async-only.
    """
    return json.dumps(summarize(load_dataset()), indent=2)

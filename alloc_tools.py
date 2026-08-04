"""The allocation agent's data tool: pull a snapshot to repair.

ONE contract, ONE definition. ``PULL_TOOL`` is the ``custom`` tool
``setup_allocation_agent.py`` declares on the agent; ``pull_allocation_snapshot``
is the implementation ``web.py`` registers to answer it. Both are derived from
the constants below, so the declaration and the implementation cannot drift into
the failure this arrangement invites: a custom tool call whose name nothing
answers parks the session on a ``requires_action`` idle, which never times out.

**Cloud sandbox.** The tool runs on *our* host; the agent runs in Anthropic's.
Nothing this function writes to disk is visible to the agent, and everything it
returns crosses into the agent's context. A 120-order snapshot is ~100 KB of
JSON — perhaps 30k tokens per pull, most of it rows the agent never reads
directly because the solver reads them.

So the tool returns the *seed*, not the rows. The data is a seeded generator, so
regenerating it inside the sandbox is byte-identical to generating it here — the
determinism argument that makes this sound is the same one the core invariant
rests on. The tool remains the pull interface: it decides the parameters, it is
where a real XAS API plugs in, and it returns the summary the agent needs to
reason. Only the transport differs.

DECIDE-7: when the real XAS pull exists the rows are no longer reproducible from
a seed, and the tool result has to carry them — at which point the payload
question comes back and wants a real answer (paging, a file resource, or a
sandbox-side fetch through a credentialed proxy).
"""

from __future__ import annotations

import json
from typing import Any

from anthropic.lib.tools import beta_async_tool

from xas_allocation.synth_data import (
    CUSTOMERS,
    HORIZON_START_WEEK,
    HORIZON_WEEKS,
    YEAR,
    generate_snapshot,
    week_label,
)

SNAPSHOT_FILENAME = "snapshot.json"

TOOL_NAME = "pull_allocation_snapshot"

TOOL_DESCRIPTION = (
    "Pull the current allocation snapshot: open orders, inbound units, the "
    "complete incumbent plan, and the disruption to repair. Returns a summary "
    "plus the exact command to materialize the full snapshot as snapshot.json in "
    "your sandbox — run that command before solving. Call this once at the start "
    "of a repair cycle and reuse the same seed for every turn of that cycle; the "
    "seed identifies the data snapshot, and a replay against a different seed is "
    "not a replay. Prototype (DECIDE-7): the real XAS pull does not exist yet, so "
    "this is a seeded synthetic generator shaped like XAS bins."
)

PULL_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "seed": {
            "type": "integer",
            "description": (
                "Seed for the synthetic pull. Identifies the data snapshot: "
                "reuse the same seed for every turn of one repair cycle."
            ),
            "default": 42,
        },
        "n_orders": {
            "type": "integer",
            "description": "Number of open orders to generate.",
            "minimum": 1,
            "maximum": 2000,
            "default": 120,
        },
        "spare_ratio": {
            "type": "number",
            "description": (
                "Unassigned inbound units as a fraction of orders. Higher means "
                "more slack for a repair to use."
            ),
            "minimum": 0,
            "maximum": 5,
            "default": 0.6,
        },
        "delay_weeks": {
            "type": "integer",
            "description": "Weeks by which the disrupted shipment is delayed.",
            "minimum": 0,
            "maximum": 12,
            "default": 3,
        },
    },
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


def materialize_command(seed: int, n_orders: int, spare_ratio: float, delay_weeks: int) -> str:
    """The one-liner that reproduces this exact snapshot inside the sandbox.

    Self-locating on purpose. The solver ships in the skill bundle, so it lands
    wherever the platform materializes skills — not in the working directory,
    and not at a path we can predict from here. The command therefore walks the
    working directory for the package and puts its parent on ``sys.path``.

    ``rglob`` from ``.`` and not from ``/``: bounded to the sandbox's own tree.
    An unbounded search is what blew the 120s bash timeout on the self-hosted
    build and took the agent's shell with it.

    Single line, single quoting level, so the agent can paste it into bash
    verbatim.
    """
    call = (
        f"generate_snapshot(seed={seed}, n_orders={n_orders}, "
        f"spare_ratio={spare_ratio}, delay_weeks={delay_weeks})"
    )
    return (
        'python -c "'
        "import sys, json, pathlib; "
        "hit = next(pathlib.Path('.').rglob('xas_allocation/synth_data.py'), None); "
        "sys.exit('xas_allocation not found under the working directory') if hit is None else None; "
        "sys.path.insert(0, str(hit.parent.parent)); "
        "from xas_allocation.synth_data import generate_snapshot; "
        f"json.dump({call}.as_dict(), open('{SNAPSHOT_FILENAME}','w'), indent=2, sort_keys=True); "
        f"print('wrote {SNAPSHOT_FILENAME}')"
        '"'
    )


def summarize(snapshot: Any, params: dict[str, Any]) -> dict[str, Any]:
    """The part of the pull that crosses into the agent's context."""
    disruption = snapshot.disruption
    units_by_state: dict[str, int] = {}
    for unit in snapshot.units:
        units_by_state[unit.state] = units_by_state.get(unit.state, 0) + 1

    return {
        "materialize": materialize_command(**params),
        "snapshot_path": SNAPSHOT_FILENAME,
        "seed": snapshot.seed,
        "orders": len(snapshot.orders),
        "units": len(snapshot.units),
        "incumbent_assignments": len(snapshot.incumbent),
        "units_by_state": dict(sorted(units_by_state.items())),
        "horizon": {
            "year": YEAR,
            "start_week": HORIZON_START_WEEK,
            "weeks": HORIZON_WEEKS,
            "first": week_label(HORIZON_START_WEEK),
            "last": week_label(HORIZON_START_WEEK + HORIZON_WEEKS - 1),
        },
        "disruption": {
            "shipment": disruption["shipment"],
            "delay_weeks": disruption["delay_weeks"],
            "delayed_units": len(disruption["delayed_units"]),
        },
        "disrupted_orders": len(disruption["disrupted_orders"]),
        "disrupted_order_ids": disruption["disrupted_orders"],
        # The §6 steering contract needs this to resolve a dealer name in the
        # planner's instruction to the id the override object carries.
        "customers": {
            name: {"customer_id": cid, "priority": priority}
            for name, (cid, priority) in CUSTOMERS.items()
        },
    }


@beta_async_tool(
    name=TOOL_NAME,
    description=TOOL_DESCRIPTION,
    input_schema=PULL_TOOL_INPUT_SCHEMA,
)
async def pull_allocation_snapshot(
    seed: int = 42,
    n_orders: int = 120,
    spare_ratio: float = 0.6,
    delay_weeks: int = 3,
) -> str:
    """Generate the snapshot here, return the summary and the seed that reproduces it.

    Async because the session tool runner is async-only.
    """
    params = {
        "seed": seed,
        "n_orders": n_orders,
        "spare_ratio": spare_ratio,
        "delay_weeks": delay_weeks,
    }
    snapshot = generate_snapshot(**params)
    return json.dumps(summarize(snapshot, params), indent=2)

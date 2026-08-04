"""The allocation agent's data tool: pull a snapshot to repair.

ONE contract, ONE definition. ``PULL_TOOL`` is the ``custom`` tool
``setup_allocation_agent.py`` declares on the agent; ``make_pull_tool()`` builds
the implementation ``worker.py`` registers to answer it. Both are derived from
the constants below, so the declaration and the implementation cannot drift into
the failure this arrangement invites: a custom tool call whose name nothing
answers parks the session on a ``requires_action`` idle, which never times out.

The tool writes the full snapshot to ``snapshot.json`` in the session workdir and
returns a *summary*. A 120-order pull is ~100 KB of JSON; returning the rows
would push the entire dataset through the context window on every pull, and the
solver reads the file rather than the conversation. The summary carries exactly
what the agent needs to reason and to compile a steering override: the disruption,
the orders it freed, and the customer-name → customer_id map the §6 steering
contract requires ("prefer Colmobil" → ``CUST-001``).

DECIDE-7: the real XAS pull does not exist. This is the seeded synthetic
generator, so ``seed`` is the whole data snapshot — same seed, byte-identical
bytes on disk, which is the ``data_snapshot`` half of the core invariant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anthropic.lib.tools import BetaAsyncFunctionTool, beta_async_tool

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
    "complete incumbent plan, and the disruption to repair. Writes the full "
    "snapshot to snapshot.json in the working directory and returns a summary — "
    "load the file from the solver, never into the conversation. Call this once "
    "at the start of a repair cycle; the same seed always regenerates a "
    "byte-identical snapshot, which is what makes a ledger replay reproduce the "
    "same plan. Prototype (DECIDE-7): the real XAS pull does not exist yet, so "
    "this is a seeded synthetic generator shaped like XAS bins."
)

PULL_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "seed": {
            "type": "integer",
            "description": (
                "Seed for the synthetic pull. Identifies the data snapshot: "
                "reuse the same seed for every turn of one repair cycle, or a "
                "ledger replay will solve against different data."
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


def _summarize(snapshot: Any, path: Path) -> dict[str, Any]:
    """The part of the pull that goes into the conversation."""
    disruption = snapshot.disruption
    units_by_state: dict[str, int] = {}
    for unit in snapshot.units:
        units_by_state[unit.state] = units_by_state.get(unit.state, 0) + 1

    return {
        "snapshot_path": path.name,
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


def make_pull_tool(workdir: str | Path) -> BetaAsyncFunctionTool[Any]:
    """Build the pull tool bound to one session's workdir.

    Async because ``beta_agent_toolset_20260401`` returns async function tools
    and the session runner is async-only.
    """
    root = Path(workdir)

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
        snapshot = generate_snapshot(
            seed=seed,
            n_orders=n_orders,
            spare_ratio=spare_ratio,
            delay_weeks=delay_weeks,
        )
        root.mkdir(parents=True, exist_ok=True)
        path = root / SNAPSHOT_FILENAME
        # sort_keys so two pulls of one seed are byte-identical on disk, not just
        # equal as objects — the invariant test compares bytes.
        path.write_text(json.dumps(snapshot.as_dict(), indent=2, sort_keys=True))
        return json.dumps(_summarize(snapshot, path), indent=2)

    return pull_allocation_snapshot

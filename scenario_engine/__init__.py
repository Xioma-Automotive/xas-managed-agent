"""Standalone scenario engine — fabricates the rich XAS world, OUTSIDE the agent.

This package is NOT bundled into the skill and never runs inside a session. A
human runs it to produce a dataset (`data/pull.json` + `data/baseline.json`);
the agent's pull ships that dataset in and `xas_allocation.flatten` turns it into
the solver snapshot.

It models the supply-first chain from `docs/xasdatamodel.md`, minus PO:

    PDN  --explodes into-->  Vehicle (pool)      SO line  --allocated to-->  Vehicle

It builds a feasible, on-time world first, then introduces a delay on one PDN —
pushing `planned_delivery_date` on every vehicle that PDN exploded into, which
is what breaks allocations and triggers the repair the agent performs.
"""

from .generate import BASE_DATE, generate

__all__ = ["BASE_DATE", "generate"]

"""Standalone scenario engine — fabricates the rich XAS world, OUTSIDE the agent.

This package is NOT bundled into the skill and never runs inside a session. A
human runs it to produce a dataset (`data/pull.json` + `data/baseline.json`);
the host fetches that dataset and mounts it into the sandbox, where
`xas_allocation.flatten` turns it into the solver snapshot.

It emits the real-XAS vocabulary (`docs/xasdatamodel.md`):

    VSO jobcard (car lines)  --allocated to-->  Vehicle (pool: real ∪ future)

It builds a feasible, on-time world first, then delays a coherent batch of
vehicles — pushing `EtaDealer` on every vehicle of one model — which is what
breaks allocations (their car now arrives past the VSO's promised date) and
triggers the repair the agent performs.
"""

from .generate import BASE_DATE, generate

__all__ = ["BASE_DATE", "generate"]

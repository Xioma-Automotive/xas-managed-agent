"""Standalone scenario engine — fabricates the XAS world, OUTSIDE the agent.

This package is NOT bundled into the skill and never runs inside a session. A
human runs it to produce a dataset; the host fetches that dataset and mounts it
into the sandbox, where `xas_allocation.flatten` turns it into the solver
snapshot.

It fabricates the app MCP's OWN response shapes —
`data/mcp-jobcards.json` + `data/mcp-vehicles.json` — and then writes
`data/pull.json` + `data/baseline.json` by handing them to
`datasource.map_world`, the same mapping the live pull runs through. So the two
MCP payloads are AUTHORED and the rich pull is DERIVED: the fake is substitutable
for the real MCP because there is one mapping, not two kept in step by hand.

It emits the real-XAS vocabulary:

    VSO jobcard (ModelItem lines)  --allocated to-->  Vehicle (pool: real ∪ future)

It builds a feasible, on-time world first, then delays a coherent batch of
vehicles — pushing `EtaDealer` on every vehicle of one model — which is what
breaks allocations (their car now arrives past the VSO's promised date) and
triggers the repair the agent performs.
"""

from .generate import BASE_DATE, generate

__all__ = ["BASE_DATE", "generate"]

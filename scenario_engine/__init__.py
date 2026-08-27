"""Standalone scenario engine — carves the allocation decision out of the REAL
export, OUTSIDE the agent.

This package is NOT bundled into the skill and never runs inside a session. A
human runs it to produce a scenario directory; the host reads that directory,
translates it (`datasource.translate`) and mounts the result into the sandbox,
where `xas_allocation.flatten` turns it into the solver snapshot.

The real export (`data/orders.csv` + `data/vehicles.csv`) holds nothing to solve:
every one of its 1641 orders already has exactly one car, and a car's status IS
its allocation state. So each script manufactures a decision out of it over one
shared `carve` (`real_export.py`):

    real_unallocated  DELETES allocations — orders that need a car at all.
    real_delayed      KEEPS them and slips the cars past the promise — orders a
                      re-allocation may repair.
    real_mixed        both at once; the other two are this one with a count
                      pinned to zero, which is why the carve lives in one place.

Each writes `data/scenario-<name>/` — `orders.csv`, `vehicles.csv` and a
`scenario.json` sidecar carrying the pull date, since no column does. The
fabricated `generate.py` world (and the MCP response payloads it authored) was
removed on 2026-08-27: with the export as the only source there is nothing for a
second, invented vocabulary to be substitutable for.
"""

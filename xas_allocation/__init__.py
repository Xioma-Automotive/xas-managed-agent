"""XAS Allocation Agent — prototype reference solver (see build spec).

Core invariant:  plan = pure_function(data_snapshot, skill, ledger)

Modules:
  decisions   — every <<DECIDE>> stub, surfaced at runtime.
  snapshot    — the flattened, date-based solver snapshot (orders/units/incumbent).
  flatten     — pure rich-pull -> snapshot mapping (the "flatten + freeze" hop).
  solver      — OR-Tools min-cost-flow repair, cost model §2, pins §5, λ sweep.
  ledger      — append-only override store, replay-with-TTL fold.
  session     — the §8 per-turn loop; discrepancy map, flow chart, change list.

The rich relational data (PDN/Vehicle/SO) is fabricated by the standalone
`scenario_engine/` package, OUTSIDE the agent; only its output crosses in.
"""

from .decisions import SOLVER_VERSION

__all__ = ["SOLVER_VERSION"]
__version__ = SOLVER_VERSION

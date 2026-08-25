"""XAS Allocation Agent — prototype reference solver (see build spec).

Core invariant:  plan = pure_function(data_snapshot, skill, override)

Modules:
  decisions   — every <<DECIDE>>, its default and its STATUS, surfaced at runtime.
  snapshot    — the flattened, date-based solver snapshot (orders/units/incumbent).
  flatten     — pure rich-pull -> snapshot mapping (the "flatten + freeze" hop).
  solver      — OR-Tools min-cost-flow repair, cost model §2, pins §5, λ sweep.
  session     — the §8 per-turn loop; discrepancy map, flow chart, planner report.

Steering is a single combined override object the agent carries forward — no
ledger, no replay. Same snapshot + same override => byte-identical plan.

The rich relational data (PDN/Vehicle/SO) is fabricated by the standalone
`scenario_engine/` package, OUTSIDE the agent; only its output crosses in.
"""

from .decisions import SOLVER_VERSION

__all__ = ["SOLVER_VERSION"]
__version__ = SOLVER_VERSION

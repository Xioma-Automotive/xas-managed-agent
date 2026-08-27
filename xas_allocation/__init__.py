"""XAS Allocation Agent — prototype reference solver (see build spec).

Core invariant:  plan = pure_function(data_snapshot, skill, override)

Modules:
  decisions   — every <<DECIDE>>, its default and its STATUS, surfaced at runtime.
  snapshot    — the flattened, date-based solver snapshot (orders/units/incumbent).
  flatten     — pure rich-pull -> snapshot mapping (the "flatten + freeze" hop).
  solver      — OR-Tools min-cost-flow repair, cost model §2, the churn sweep.
  session     — the §8 per-turn loop; discrepancy map, flow chart, planner report.
  solver_config.yaml — every parameter the solver prices with. Read by solver.py
                and nothing else; editing it means re-running setup_agent.py.

Nothing is imported here on purpose. `flatten` runs in the sandbox before
`pip install ortools` has necessarily happened, and a package-level import of
`solver` (which needs OR-Tools, and now PyYAML) would make that fail. Read
``solver.SOLVER_VERSION`` for the version.

Steering is a single combined override object the agent carries forward — no
ledger, no replay. Same snapshot + same override => byte-identical plan.

The rich relational data (PDN/Vehicle/SO) is fabricated by the standalone
`scenario_engine/` package, OUTSIDE the agent; only its output crosses in.
"""

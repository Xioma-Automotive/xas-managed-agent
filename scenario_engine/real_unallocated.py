"""Scenario — UNALLOCATED ORDERS, cut from the real XAS export.

Deletes allocations and nothing else. A chosen subset of orders loses the car it
held (keeping its `OrderId`, model, colour and `etaDealer` promise) and each of
those cars goes back into the pool as available. The question is the plain one:
which car should each of these orders get?

  orders to empty       demand that needs a plan. Their cars are freed, so every
                        emptied order can always at least get its own car back —
                        the interesting part is whether a better one exists.
  extra free cars       further cars freed the same way, but their ORDERS LEAVE
                        THE BOOK: otherwise every freed car arrives with its own
                        claimant attached and the pool never has slack.
  vehicles subset size  how many cars the scenario holds in total.
  % available           the free share of that subset. The two counts above are
                        the FIRST cars counted toward it; the rest is padded with
                        cars the export already had available, and what remains
                        stays allocated with its order intact.

The export already ships 256 late orders, so a subset inherits some — the run
reports them, since the solver will act on them too. For late orders ON PURPOSE
see `real_delayed`, and for both at once `real_mixed`; all three are one `carve`.
"""

from __future__ import annotations

from scenario_engine import real_export as export
from scenario_engine.real_export import Export


def main() -> None:
    parser = export.base_parser(__doc__.splitlines()[0], "scenario-unallocated")
    parser.add_argument("--empty", type=int, help="orders whose allocation is deleted")
    args = parser.parse_args()

    data = Export(args.orders_in, args.vehicles_in)
    export.run(
        data,
        args.out,
        empty=export.ask_int(args.empty, "orders to empty allocation", 8),
        late=0,
        days_late=(0, 0),
        extra_free=export.ask_int(
            args.extra_free, "additional available cars to create (their orders dropped)", 0
        ),
        on_time_pct=export.ask_float(
            args.on_time_pct, "percent of the book allocated and on time", 20.0
        ),
        available_pct=export.ask_float(args.available_pct, "percent of the subset available", 85.0),
        models=2 if args.models is None else args.models,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()

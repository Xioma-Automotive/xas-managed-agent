"""Scenario — MIXED: some orders unallocated, some riding a late car.

Both disturbances at once, which is what a real book looks like: orders with no
car at all competing for the same pool as orders whose car has slipped past its
promise. The two compete — a free car that repairs a late order is a car an
unallocated order cannot have — so this is the scenario where the solver's
weighting actually decides something.

  orders to empty       lose the car they held, keeping their `etaDealer` promise.
                        Their cars are freed.
  orders to make late   keep their car; its `availableBy` moves to
                        `etaDealer + days late`. Drawn from the 694 orders whose
                        car is still inbound and on time, since only those can
                        slip without rewriting history.
  days late             the span to draw from, "1-20" or a single number.
  extra free cars       cars freed by deleting an allocation, their ORDERS LEAVING
                        THE BOOK — pool slack neither disturbance brought with it.
  vehicles subset size  how many cars the scenario holds in total.
  % available           the free share of that subset. Emptied and extra-freed
                        cars are the FIRST counted toward it; the rest is padded
                        with cars the export already had available.

`real_unallocated` and `real_delayed` are this scenario with one of the two counts
pinned to zero — all three run the same `carve`.
"""

from __future__ import annotations

from scenario_engine import real_export as export
from scenario_engine.real_export import Export


def main() -> None:
    parser = export.base_parser(__doc__.splitlines()[0], "scenario-mixed")
    parser.add_argument("--empty", type=int, help="orders whose allocation is deleted")
    parser.add_argument("--late", type=int, help="orders whose car is delayed past its promise")
    parser.add_argument("--days-late", help='how far past the promise, e.g. "1-20" or "8"')
    args = parser.parse_args()

    data = Export(args.orders_in, args.vehicles_in)
    print(f"  delayable (car inbound and on time): {len(export.delayable(data))}")
    export.run(
        data,
        args.out,
        empty=export.ask_int(args.empty, "orders to empty allocation", 50),
        late=export.ask_int(args.late, "orders to make late", 50),
        days_late=export.ask_range(args.days_late, "days late (span or one number)", "1-20"),
        extra_free=export.ask_int(
            args.extra_free, "additional available cars to create (their orders dropped)", 50
        ),
        subset=export.ask_int(args.subset, "vehicles in the subset", 400),
        available_pct=export.ask_float(args.available_pct, "percent of the subset available", 40.0),
        models=args.models or 0,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()

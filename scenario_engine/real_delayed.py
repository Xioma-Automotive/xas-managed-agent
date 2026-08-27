"""Scenario — DELAYED VEHICLES, cut from the real XAS export.

Keeps every allocation and slips the cars instead. A chosen subset of orders has
its car's `availableBy` pushed past its `etaDealer`, so the car it holds now
arrives after the date the customer was promised. Nothing is unallocated: the
question is whether to leave each order riding a late car or swap it onto a free
one that still makes the promise.

  orders to make late   their car's arrival moves to `etaDealer + days late`. The
                        allocation STANDS — only `availableBy` moves, and the
                        order row is not touched at all.
  days late             the span to draw from, "1-20" or a single number. 1-20 is
                        what the export's own 114 real late orders show (median 8).
  extra free cars       cars freed by deleting an allocation, their ORDERS LEAVING
                        THE BOOK — with the already-available padding, this is the
                        only supply a swap can draw on.
  vehicles subset size  how many cars the scenario holds in total.
  % available           the free share of that subset, as in `real_unallocated`.

Only a car still INBOUND can slip: one whose `availableBy` has passed is already
on the dealer's hands (1727 of the export's 3523), and delaying it would rewrite
history. Candidates are the 694 allocated orders whose car is both inbound and
currently on time — mostly orders whose `etaDealer` was filled in rather than
genuinely exported. For unallocated demand see `real_unallocated`, for both at once
`real_mixed`; all three are one `carve`.
"""

from __future__ import annotations

from scenario_engine import real_export as export
from scenario_engine.real_export import Export


def main() -> None:
    parser = export.base_parser(__doc__.splitlines()[0], "scenario-delayed")
    parser.add_argument("--late", type=int, help="orders whose car is delayed past its promise")
    parser.add_argument("--days-late", help='how far past the promise, e.g. "1-20" or "8"')
    args = parser.parse_args()

    data = Export(args.orders_in, args.vehicles_in)
    print(f"  delayable (car inbound and on time): {len(export.delayable(data))}")
    export.run(
        data,
        args.out,
        empty=0,
        late=export.ask_int(args.late, "orders to make late", 100),
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

"""DECIDE-15: arriving early costs something now, so the solver stops grabbing
needlessly-early cars — but gently, so lateness always dominates.

Before this term, an early arrival scored 0 (same as on-time), and the solver was
indifferent between a 2-days-early and a 40-days-early car. Now:
  * between two early options it prefers the closer one;
  * for comparable magnitudes a little-early beats a little-late;
  * but the term is small + linear, so extreme earliness MAY lose to slight
    lateness (the documented, intended crossover — tying a car up for months is
    real waste).
"""

from datetime import date

from xas_allocation.session import _result_phrase
from xas_allocation.snapshot import Order, Snapshot, Unit
from xas_allocation.solver import solve

NOW = date(2026, 8, 3)
PROMISED = date(2026, 11, 1)  # ~90 days out → liquid fence, no λ churn in play


def _order() -> Order:
    return Order(
        order_id="SO-1-1",
        so_id="SO-1",
        customer="Dealer 1",
        customer_id="CUST-001",
        sales_model="SM1",
        priority="B",
        promised_date=PROMISED,
        eta_date=PROMISED,
        price=40000,
        n_prior_delays=0,
        days_backordered=0,
    )


def _unit(vid: str, planned: date) -> Unit:
    return Unit(
        vehicle_id=vid,
        kind="vehicle",
        sales_model="SM1",
        planned_delivery_date=planned,
        location_state="sea",
        po_ref="PO-150-1-1",
        pdn="PDN-150",
        committed=False,
    )


def _snap(units: list[Unit]) -> Snapshot:
    # The disrupted order's ORIGINAL car slipped badly (VEH-INC, far late), so its
    # binding is already broken -> free to leave (break cost 0, DECIDE-3). Both
    # cars under test are replacements, so the break cost cancels and the pure
    # earliness/lateness cost model decides — which is what these tests exercise.
    inc = _unit("VEH-INC", date(2027, 3, 1))
    return Snapshot(
        orders=[_order()],
        units=[*units, inc],
        incumbent={"SO-1-1": "VEH-INC"},
        disruption={"disrupted_orders": ["SO-1-1"]},  # free to re-allocate
        now=NOW,
    )


def test_closer_early_car_preferred():
    near = _unit("VEH-NEAR", date(2026, 10, 30))  # 2 days early
    far = _unit("VEH-FAR", date(2026, 9, 22))  # 40 days early
    snap = _snap([near, far])
    result = solve(snap, {}, lam=0)
    assert result.plan["SO-1-1"] == "VEH-NEAR", "should prefer the less-early car"


def test_slightly_early_beats_slightly_late():
    early = _unit("VEH-EARLY", date(2026, 10, 31))  # 1 day early
    late = _unit("VEH-LATE", date(2026, 11, 2))  # 1 day late
    snap = _snap([early, late])
    result = solve(snap, {}, lam=0)
    assert result.plan["SO-1-1"] == "VEH-EARLY", "early should beat late for equal small gaps"


def test_extreme_early_can_lose_to_slight_late():
    """The documented crossover (DECIDE-15): a car 40 days early costs more than one
    1 day late, so the solver takes the slightly-late car. Intended, not a bug."""
    far_early = _unit("VEH-FAR-EARLY", date(2026, 9, 22))  # 40 days early
    slight_late = _unit("VEH-SLIGHT-LATE", date(2026, 11, 2))  # 1 day late
    snap = _snap([far_early, slight_late])
    result = solve(snap, {}, lam=0)
    assert result.plan["SO-1-1"] == "VEH-SLIGHT-LATE", (
        "extreme earliness may lose to slight lateness"
    )


def test_report_frames_earliness_as_a_caveat_not_a_win():
    o = _order()
    very_early = _unit("VEH-X", date(2026, 10, 1))  # 31 days early
    a_bit_early = _unit("VEH-Y", date(2026, 10, 28))  # 4 days early, under the flag
    phrase = _result_phrase(o, very_early, "days", 1)
    assert "early" in phrase and "ties a car up" in phrase and "✅" not in phrase
    # a couple of days early isn't nagged about
    assert _result_phrase(o, a_bit_early, "days", 1) == "on time"

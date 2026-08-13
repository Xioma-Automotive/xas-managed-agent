"""DECIDE-14: the planner's time_scale sets the resolution the solver reasons at.

Day-gaps are rounded UP to whole units, so differences finer than a unit collapse
and a coarse scale never under-states a gap. Day scale (the default) is the
identity — today's behaviour, unchanged.
"""

from datetime import date

from xas_allocation.flatten import flatten_default
from xas_allocation.session import _dur
from xas_allocation.snapshot import Order, Unit
from xas_allocation.solver import arc_cost_float, scale_units

NOW = date(2026, 8, 3)


def _order(promised: date) -> Order:
    return Order(
        so_id="SO-1",
        line=1,
        customer="Dealer 1",
        customer_id="CUST-001",
        sales_model="SM1",
        priority="B",
        delivery_date=promised,
        price=40000,
        n_prior_delays=0,
        days_backordered=0,
    )


def _unit(planned: date) -> Unit:
    return Unit(
        vehicle_id="VEH-1",
        vehicle_classification="Vehicle",
        sales_model="SM1",
        eta_dealer=planned,
    )


def test_scale_units_rounds_up():
    assert scale_units(0, 7) == 0
    assert scale_units(1, 7) == 1  # any part of a unit → a full unit
    assert scale_units(7, 7) == 1
    assert scale_units(8, 7) == 2
    # within-unit differences collapse: 3 and 5 days both → 1 week
    assert scale_units(3, 7) == scale_units(5, 7) == 1
    # day scale is the identity
    assert [scale_units(d, 1) for d in (0, 1, 9, 21)] == [0, 1, 9, 21]


def test_within_unit_lateness_costs_the_same_at_week_scale():
    o = _order(date(2026, 11, 1))  # liquid; isolate lateness (no churn at lam=0)
    late3 = _unit(date(2026, 11, 4))  # 3 days late
    late5 = _unit(date(2026, 11, 6))  # 5 days late
    boosts: dict[str, float] = {}
    # week scale: both are "1 week late" → identical cost
    c3_w = arc_cost_float(o, late3, 0, NOW, boosts, None, unit_days=7)
    c5_w = arc_cost_float(o, late5, 0, NOW, boosts, None, unit_days=7)
    assert c3_w == c5_w
    # day scale: they differ
    c3_d = arc_cost_float(o, late3, 0, NOW, boosts, None, unit_days=1)
    c5_d = arc_cost_float(o, late5, 0, NOW, boosts, None, unit_days=1)
    assert c3_d != c5_d


def test_churn_respects_the_bucket():
    """λ churn fires only when the delivery differs from the promise by ≥ 1 unit."""
    o = _order(date(2026, 9, 1))  # 29 days out → slushy fence, where λ applies
    near = _unit(date(2026, 9, 4))  # 3 days off promise
    boosts: dict[str, float] = {}
    # week scale: 3-day shuffle is within the unit → λ does NOT fire (cost is λ-independent)
    assert arc_cost_float(o, near, 0, NOW, boosts, None, 7) == arc_cost_float(
        o, near, 100, NOW, boosts, None, 7
    )
    # day scale: a 3-day change IS churn → λ raises the cost
    assert arc_cost_float(o, near, 100, NOW, boosts, None, 1) > arc_cost_float(
        o, near, 0, NOW, boosts, None, 1
    )


def test_default_scale_matches_days():
    snap = flatten_default()
    p_default = solve_plan(snap, {})
    p_days = solve_plan(snap, {"time_scale": "days"})
    assert p_default == p_days, "absent time_scale must equal explicit 'days'"


def test_coarser_scale_is_deterministic_and_valid():
    snap = flatten_default()
    a = solve_plan(snap, {"time_scale": "months"})
    b = solve_plan(snap, {"time_scale": "months"})
    assert a == b  # deterministic


def test_dur_renders_in_the_active_unit():
    assert _dur(21, "days", 1) == "21 days"
    assert _dur(1, "days", 1) == "1 day"
    assert _dur(10, "weeks", 7) == "2 weeks"  # round up
    assert _dur(7, "weeks", 7) == "1 week"
    assert _dur(20, "months", 30) == "1 month"


def solve_plan(snap, override):
    from xas_allocation.solver import solve

    r = solve(snap, override)
    assert r.self_check["ok"], r.self_check["violations"]
    return {k: v for k, v in sorted(r.plan.items())}

"""DECIDE-13: an untouched order is displaced ONLY when the planner authorizes it.

Without `may_move.also`, an order that needs help stays late even though an
untouched one is sitting on the only on-time car. With `also` naming that order,
the solver displaces it — but only because doing so lowers total cost, and only
after paying `break_cost` for the promise it disturbs.

Two things this file exists to hold down after the 2026-08-26 rework:
  * removing the time fence did NOT weaken the no-bump default. The default is
    the free-set rule, and it still refuses to touch a settled order.
  * removing the fence DID fix authorised bumping, which the fence used to cancel
    before the authorisation was even read.
"""

from datetime import date

import pytest

from xas_allocation.session import bump_candidates, carry_forward
from xas_allocation.snapshot import Order, Snapshot, Vehicle
from xas_allocation.solver import CFG, break_cost_of, solve, tardiness

NOW = date(2026, 8, 3)
PROMISED = date(2026, 9, 14)
NEAR_PROMISED = date(2026, 8, 10)  # inside what used to be the frozen fence
LATE = date(2026, 10, 14)
ON_TIME = date(2026, 9, 14)


# One order id, one order, one car. HI is the late one; LO holds the on-time car
# a rescue would have to take.
ORDER_HI, ORDER_LO = "900001-1", "900002-1"


def _order(oid: str, promised: date = PROMISED, alloc_type: str = "soft") -> Order:
    card, _, line = oid.rpartition("-")
    # A bump victim's promise has a price keyed on its firmness, so every fixture
    # here has to name one. `soft` is the old single value, which is what keeps
    # the assertions below about the DEFAULT bump behaviour unchanged.
    return Order(
        job_card=card,
        line=line,
        sales_model="SM1",
        delivery_date=promised,
        alloc_type=alloc_type,
    )


def _vehicle(vid: str, planned: date) -> Vehicle:
    return Vehicle(vehicle_id=vid, sales_model="SM1", eta_dealer=planned)


def _snapshot(promised: date = PROMISED) -> Snapshot:
    """HI is late on its car; LO (untouched) holds the only on-time car; one spare
    late car exists for LO to fall back to."""
    return Snapshot(
        orders=[_order(ORDER_HI, promised), _order(ORDER_LO, promised)],
        vehicles=[
            _vehicle("VEH-HI-LATE", LATE),
            _vehicle("VEH-LO-GOOD", promised),  # lands exactly on the promise
            _vehicle("VEH-SPARE-LATE", LATE),
        ],
        allocations={ORDER_HI: "VEH-HI-LATE", ORDER_LO: "VEH-LO-GOOD"},
        disruption={"disrupted_orders": [ORDER_HI]},
        now=NOW,
    )


# The realistic authorisation, and the ONLY thing that makes a bump worth its
# cost now that no order is heavier than another by default: the planner says
# which order matters, then says who may be displaced for it. Two orders of equal
# weight swapping the same lateness is a wash the solver correctly declines.
URGENT_HI = {"priority": [{"order": ORDER_HI, "step": "urgent"}]}
AUTH = {**URGENT_HI, "may_move": {"also": {"orders": [ORDER_LO]}}}


def test_without_authorization_no_bump_and_the_late_order_stays_late():
    snap = _snapshot()
    result = solve(snap, {}, churn_price=0)
    vehicles = snap.vehicle_by_id()
    assert result.plan[ORDER_LO] == "VEH-LO-GOOD", "untouched order must not move uninvited"
    assert tardiness(snap.order_by_key()[ORDER_HI], vehicles[result.plan[ORDER_HI]]) > 0
    # ...and the agent can see the bump it would need to propose.
    assert any(c["row"] == ORDER_LO for c in bump_candidates(snap, result))


def test_the_no_bump_default_survived_the_fence_removal():
    """The fence used to be a second wall in front of a settled order promised
    within 14 days. It is gone; the free-set rule alone must still hold that order
    still, both when nothing is steered and when the turn is sliced."""
    snap = _snapshot(NEAR_PROMISED)
    for steer in ({}, {"may_move": {"only": {"customers": ["CUST-001", "CUST-002"]}}}):
        result = solve(snap, steer, churn_price=0)
        assert result.plan[ORDER_LO] == "VEH-LO-GOOD", f"bumped uninvited under {steer}"


def test_an_authorized_bump_is_declined_when_it_buys_nothing():
    """Both orders weigh the same, so moving 30 days of lateness from one to the
    other is a wash — and it costs a break. Authorisation is permission, never an
    instruction: the solver still has to be better off."""
    snap = _snapshot()
    result = solve(snap, {"may_move": {"also": {"orders": [ORDER_LO]}}}, churn_price=0)
    assert result.plan[ORDER_LO] == "VEH-LO-GOOD"


def test_authorized_bump_rescues_the_order_that_needs_help():
    snap = _snapshot()
    result = solve(snap, AUTH, churn_price=0)
    vehicles = snap.vehicle_by_id()
    assert result.plan[ORDER_HI] == "VEH-LO-GOOD", "the late order should get the good car"
    assert tardiness(snap.order_by_key()[ORDER_HI], vehicles[result.plan[ORDER_HI]]) == 0
    assert result.plan[ORDER_LO] != "VEH-LO-GOOD", "the authorized order was bumped off it"


def test_an_authorized_bump_works_close_to_delivery_too():
    """The fence's actual bug: it fired BEFORE the authorisation check, so a bump
    a planner had explicitly authorised silently no-oped whenever the victim was
    near delivery. Three did, on 2026-08-25."""
    snap = _snapshot(NEAR_PROMISED)
    result = solve(snap, AUTH, churn_price=0)
    assert result.plan[ORDER_HI] == "VEH-LO-GOOD"


def test_the_priority_step_decides_who_gets_the_one_good_car():
    """Priority is the planner's step, not a letter on the record: the SAME book
    and the SAME authorisation give opposite answers depending on who was named."""
    snap = _snapshot()
    assert solve(snap, AUTH, churn_price=0).plan[ORDER_HI] == "VEH-LO-GOOD"
    unnamed = {"may_move": AUTH["may_move"]}
    assert solve(snap, unnamed, churn_price=0).plan[ORDER_LO] == "VEH-LO-GOOD"


def test_an_unknown_priority_step_is_an_error_not_a_shrug():
    """A mistyped step that silently fell back to 'normal' would look like the
    instruction had been applied."""
    with pytest.raises(ValueError, match="unknown priority step"):
        solve(_snapshot(), {"priority": [{"order": ORDER_HI, "step": "critical"}]})


def test_break_cost_can_block_an_authorized_bump(monkeypatch):
    """break_cost prices the VICTIM's kept promise. With the config's default the
    rescue is worth paying for; make it prohibitive and the solver declines. It is
    config, not steering — a planner does not type this number, which is why it
    left the override object on 2026-08-26."""
    snap = _snapshot()
    assert solve(snap, AUTH, churn_price=0).plan[ORDER_HI] == "VEH-LO-GOOD"

    monkeypatch.setitem(CFG, "break_cost", {"soft": 100_000.0, "hard": 100_000.0})
    blocked = solve(snap, AUTH, churn_price=0)
    assert blocked.plan[ORDER_LO] == "VEH-LO-GOOD", "on-time hard allocation kept"
    assert (
        tardiness(snap.order_by_key()[ORDER_HI], snap.vehicle_by_id()[blocked.plan[ORDER_HI]]) > 0
    )


ANYONE = {**URGENT_HI, "may_move": {"also": True}}


def test_also_true_rescues_the_same_order_a_named_authorisation_would():
    """The fleet-wide form is a wider permission, not a stronger one: on a book
    with one possible displacement it produces exactly what naming that order
    produces."""
    snap = _snapshot()
    assert solve(snap, ANYONE, churn_price=0).plan[ORDER_HI] == "VEH-LO-GOOD"
    assert solve(snap, AUTH, churn_price=0).plan[ORDER_HI] == "VEH-LO-GOOD"


def test_also_true_declines_a_bump_that_buys_nothing():
    """Opening the whole book moves nothing on its own. Both orders weigh the
    same, so shifting 30 days of lateness from one to the other is a wash — and
    it costs a break. Permission is not an instruction."""
    snap = _snapshot()
    result = solve(snap, {"may_move": {"also": True}}, churn_price=0)
    assert result.plan[ORDER_LO] == "VEH-LO-GOOD"
    # The strongest form of "moves nothing on its own": the whole book comes out
    # exactly as it does with no authorisation at all. (Not `n_changes == 0` —
    # this book has one change either way: HI is late, so it is in the free set
    # by default and swaps one equally-late car for another, which is free at
    # churn_price=0. That change is the repair, not a bump.)
    assert result.plan == solve(snap, {}, churn_price=0).plan


def test_carry_forward_spends_the_bump_authorisation():
    """`may_move.also` is permission for ONE solve. Both forms are dropped."""
    assert carry_forward({"may_move": {"also": True}}) == {}
    assert carry_forward({"may_move": {"also": {"orders": [ORDER_LO]}}}) == {}


def test_carry_forward_keeps_every_standing_instruction():
    """Priority, the slice, an absolute hold and the churn price are standing
    instructions until the planner changes them — only the authorisation expires."""
    override = {
        "priority": [{"order": ORDER_HI, "step": "urgent"}],
        "churn_price": 25,
        "may_move": {"only": {"models": ["SM1"]}, "never": [ORDER_LO], "also": True},
    }
    assert carry_forward(override) == {
        "priority": [{"order": ORDER_HI, "step": "urgent"}],
        "churn_price": 25,
        "may_move": {"only": {"models": ["SM1"]}, "never": [ORDER_LO]},
    }


def test_carry_forward_does_not_mutate_the_override_that_was_solved():
    """The solved object is stamped into plan.json, which is what makes the turn
    reproducible. Editing it in place would rewrite history."""
    override = {"may_move": {"also": True, "never": [ORDER_LO]}}
    carry_forward(override)
    assert override == {"may_move": {"also": True, "never": [ORDER_LO]}}


def test_carry_forward_handles_an_empty_or_missing_override():
    assert carry_forward(None) == {}
    assert carry_forward({}) == {}


# --- DECIDE-3, re-split 2026-09-09: a hard promise costs more to break --------


def test_a_hard_promise_costs_more_to_break_than_a_soft_one():
    """The whole of the decision, in one comparison. `AllocType` is the firmness
    of the LINE — a committed VIN against a provisional reservation — and it is
    the only thing that changes here: same order, same car, same date."""
    car = _vehicle("VEH-LO-GOOD", PROMISED)
    soft = _order(ORDER_LO, PROMISED, alloc_type="soft")
    hard = _order(ORDER_LO, PROMISED, alloc_type="hard")
    assert break_cost_of(hard, car) > break_cost_of(soft, car)
    assert break_cost_of(soft, car) == CFG["break_cost"]["soft"]
    assert break_cost_of(hard, car) == CFG["break_cost"]["hard"]


def test_firmness_prices_nothing_when_there_is_no_promise_to_break():
    """Both exemptions survive the split: no car at all, and a car that is
    already late. A promise already broken protects nothing, however firm it was
    — so the rescue of a disrupted order stays free for hard lines too."""
    late_car = _vehicle("VEH-HI-LATE", LATE)
    for kind in ("hard", "soft"):
        order = _order(ORDER_LO, PROMISED, alloc_type=kind)
        assert break_cost_of(order, None) == 0.0
        assert break_cost_of(order, late_car) == 0.0


def test_an_unreadable_firmness_is_an_error_not_a_shrug():
    """Same rule as an unknown priority step: defaulting to either price would
    make a line whose firmness we could not read look deliberately cheap (or
    deliberately protected). The message names the line."""
    car = _vehicle("VEH-LO-GOOD", PROMISED)
    for bad in ("", "HARD-ISH", "firm"):
        with pytest.raises(ValueError, match="neither 'hard' nor 'soft'"):
            break_cost_of(_order(ORDER_LO, PROMISED, alloc_type=bad), car)


def test_the_same_authorised_bump_turns_on_the_victims_firmness(monkeypatch):
    """The behavioural half: the planner authorises the same displacement, and
    whether it happens depends on which price the victim's firmness selects. The
    two numbers are patched rather than read, because the SHIPPED pair is tuned
    and unvalidated (DECIDE-3) — what this pins is that the key is read at all."""
    monkeypatch.setitem(CFG, "break_cost", {"soft": 0.0, "hard": 100_000.0})

    soft_victim = _snapshot()
    assert solve(soft_victim, AUTH, churn_price=0).plan[ORDER_HI] == "VEH-LO-GOOD"

    hard_victim = _snapshot()
    hard_victim.orders[1] = _order(ORDER_LO, PROMISED, alloc_type="hard")
    declined = solve(hard_victim, AUTH, churn_price=0)
    assert declined.plan[ORDER_LO] == "VEH-LO-GOOD", "a hard promise held"
    assert declined.plan[ORDER_HI] != "VEH-LO-GOOD"

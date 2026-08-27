# Allocation Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `xas-allocation` skill and its solver work on the real XAS export — two CSV streams joined inside the sandbox, all three scenarios (no car / late car / both), and a setup that serves **both** modes: bumping and not bumping. For now the agent runs no-bump; who may be bumped is decided later.

**Architecture:** Nothing about bumping is removed. The discovery driving this plan is that no-bump is *already* implemented by one line in `partition` — the free set is `disrupted ∪ deferred ∪ unallocated`, and an on-time allocated order matches none of them, so it is pinned and its car is consumed. The 14/42-day time fence is a **second, blunter** wall on top that fires before the authorisation check, which is why three authorised bumps once no-oped. Removing the fence is therefore what makes bumping work, and it leaves no-bump exactly as it was. Break cost stays and becomes genuinely live: it is the price of taking an on-time car once a planner authorises it.

**Tech Stack:** Python 3.11, OR-Tools min-cost-flow, pytest, uv, ruff. No new dependencies.

---

## The two modes, and where each lives in the code

| | no-bump (today's default) | bump (authorised) |
|---|---|---|
| What may move | orders that are late, deferred, or have no car | those, plus orders a planner names |
| Implemented by | `partition`: `include = (oid in disrupted) or names_order(o, deferred) or (assigned is None)` | the same line, plus `include = _matches(o, bump)` |
| What stops an on-time order moving | it is not in the free set — no wall needed | nothing; **cost** decides |
| Price of taking an on-time car | never arises | `break_cost_of` → `BREAK_COST["hard"]` = 200 |
| How the agent selects it | sets no `bump` key | sets `bump: {...}` after asking |

**There is no mode flag and none is needed.** The mode *is* whether the override carries a `bump` filter. "Who is allowed to get bumped" is that filter's contents, and widening it later — for instance to select by the car's `status.name`, so only `Dealer Order Confirmation` and `Dealer Reservation` cars can be taken — is a new dimension in `_matches`, not a new mechanism. Task 7 carries `status` onto `Unit` so that dimension has something to filter on when it lands.

---

## Read this first: gotchas found while investigating

| # | Gotcha | Where | Handled in |
|---|---|---|---|
| G1 | **`find_discrepancies` iterates `snapshot.incumbent`, so an order with no car cannot appear.** On a pure-unallocated scenario turn 1 prints *"No orders are late — every allocated car still meets its promised date"* over a book where nothing is allocated. That is the "presented as the whole book" failure the skill forbids, produced structurally. | `session.py::find_discrepancies`, `discrepancy_report` | Task 8 |
| G2 | **`planner_report`'s headline is built from `disrupted`**: `f"{n_fixed} of {len(broken)} delayed orders now on time"`. With no disruption it reads "0 of 0 delayed orders now on time" after allocating twenty cars. | `session.py::planner_report` | Task 8 |
| G3 | **The fence is the ONLY input to `repairability`**, so removing it makes that function a constant returning `"movable"`. Dead with it: the "locked in" table in `discrepancy_report`, `stuck`/`reason_of` in `planner_report`, `_why_late`, `Discrepancy.fixable`, `Discrepancy.reason`, the `d.fixable` sort term, and the first branch of `_caveat`. `is_locked_in` and `fence_of` are deleted outright — nothing replaces them, because the free-set line already does the protecting. | `session.py`, `solver.py` | Task 2 |
| G4 | **Removing the fence FIXES bumping, it does not weaken it.** `session.py:243-246` records the bug in its own comment: *"three authorized bumps all no-oped for exactly this reason"* — the rescue targets were fence-locked, so the freed cars sat idle. The fence fires before the authorisation check in `partition`, so it overrode scope, defer and bump alike. Expect `bump_candidates` to start returning more candidates and authorised bumps to start taking effect; that is the fix, not a regression. | `solver.py::partition`, `session.py:243` | Task 2 |
| G5 | **`break_cost_of` is live in bump mode and dormant outside it.** It charges only for taking a car from an order whose car arrives on time — which is exactly what an authorised bump does, and never happens otherwise. Keep every line. Its `BREAK_COST["hard"]=200` is the "how many weighted late-days is one broken promise worth" ratio and it is **unvalidated** — a planner must own that number before bump mode ships to anyone. | `solver.py::break_cost_of` | Task 2 (untouched), Task 10 |
| G6 | **λ is connected to nothing on real data.** It charges when the car's arrival differs from the promised date — which is true of 5288 of 5348 eligible pairings (98.9%), so it is a near-constant added to almost every option and cannot steer a choice. Measured sweep on `scenario-mixed`: weighted late-days is **193.0 at every λ from 0 to 100**, and the change count wobbles 120–124 with no direction (tie-break noise). Charging for *a different car than the order had* instead produces a real curve: 122 moves/193 late-days at λ=0 → 50 moves/701 at λ=100. | `solver.py::arc_cost_float` | Task 3 |
| G7 | **`is_hard` is not only a cost input** — three display sites use it (`data_prep_flowchart`'s real-car count, the `"future"`/`"car"` label in the moved table, `on_the_lot` in `plan_rows`) as well as `break_cost_of`. `Unit.vehicle_classification` survives everything in this plan. | `session.py:287,479,590`, `solver.py:189` | Task 7 |
| G8 | **`alloc_tools.summarize` reads MCP shapes and is what crosses into the agent's context** — `rich["vsos"]`, `JobItems`, `Accounts.Owner`, `JobPriority`. With two CSVs mounted it has nothing to read and must be rewritten, not tweaked. The `customers` map goes (no dealer column); the `disruption` block goes (no manifest — lateness is derived). | `alloc_tools.py::summarize` | Task 7 |
| G9 | **Pre-existing bug that surfaces during G8:** `summarize` computes `orders` as `sum(int(item.get("Quantity") or 1) ...)` while `flatten` builds one order per line and never reads `Quantity`. Its own comment says the two numbers get compared. On any line with `Quantity > 1` the tool tells the agent a different order count than the snapshot it is about to solve. `CLAUDE.md` states "`Quantity` is not read at all" — the summary is the exception nobody caught. | `alloc_tools.py:150-153,183` | Task 7 |
| G10 | **Two mounts break four things**, all assuming one file: `MOUNT_PATH`, `mount_candidates`, `flatten_command` (single `src`), and `web.py`'s `_download_pull` / `_pull_by_session` cache. `tests/test_tool_contract.py` (9 tests) pins the wiring, and a tool call nothing answers parks the session on a `requires_action` idle that **never times out** — the failure looks like a hang, not an error. | `alloc_tools.py:41-55,92`, `web.py:97,155-174,408` | Task 7 |
| G11 | **`Order.key` is `f"{so_id}-{line}"`.** Setting `line = 0` for a flat export yields `502375-0`, which is not the `OrderId` and lands in the planner's report. The key must become the `OrderId`. `names_order` / `not_before_for` / `_matches` all match at two levels and keep working when both collapse to the same string — but `_matches`' docstring still describes THREE levels including a per-car index left over from the removed qty expansion. | `snapshot.py::Order.key`, `solver.py` | Task 6 |
| G12 | **Priority is read from data in one place and displayed in ~18.** `effective_weight` does `D.PRIORITY_WEIGHT[order.priority]`; six report tables print `f"{o.customer} ({o.priority})"`. Moving it to the override means passing it in — reuse the `boosts` parameter slot rather than adding plumbing. | `solver.py::effective_weight` | Task 4, Task 5 |
| G13 | **`bump_candidates` ranks by the priority letter** (`{"C": 0, "B": 1, "A": 2}`) and shows the dealer name. Both inputs are going. It is **kept and reworked**, not deleted — it is how the planner is shown who could be displaced, which is half of bump mode. | `session.py:235-278` | Task 5 |
| G14 | **`exclusion_note` reads exactly two bucket names** — `meta["excluded"]["order_drops"]` and `["flatten_skips"]`. A reader that invents a third makes the mandatory turn-1 note print nothing. Reuse the names; add the CSV reasons to `DROP_PHRASES`. | `session.py::exclusion_note` | Task 7 |
| G15 | **`_self_check` cannot catch the new seam's failure.** It checks eligibility, double-booking and every-order-placed — not that the incumbent is internally consistent. A CSV pair where an `Available For Sale` car is also named by an order's `vehicleCode` would silently double-count supply. The scenario scripts never emit that; the live export is not guaranteed. | `solver.py::_self_check` | Task 7 |
| G16 | **`status.name` has a trailing-space variant** — `'Available For Sale '` on ~86% of the export's available cars. Status decides what is free supply, so an unstripped compare halves the pool. | new reader | Task 7 |
| G17 | **The solver's tie-break is node ordering, which is not stable under repartition.** Solving per sales model gives an equally good plan (identical weighted late-days: 193.0 and 905.0 on two books) but a *different* one — 72 of 126 free orders got a different car, every difference swapping two interchangeable cars of the same model. Nothing in this plan splits the graph, so this is latent, not active; but it means "byte-identical plan" holds only for a fixed OR-Tools build and a fixed book size. | `solver.py::_solve_one` | Task 11 (noted, not fixed) |
| G18 | **Test blast radius by symbol** (of 172 tests): `customer` 29, `disruption` 25, `priority` 19, the three history fields 17, `so_id` 15, `boosts` 13, `vehicle_classification` 11, `bump` 27 — the last of which now mostly **stays**. `tests/test_bump.py` (3 tests) is kept and updated rather than deleted. `tests/test_agent_contract.py` (56) pins skill and prompt text including `"vehicle purchase order"` and `"no VPO ids"`. | `tests/` | every task |

### Naming: the export's words, not XAS's (decided 2026-08-25)

Every field name in the snapshot comes from **the CSV headers**, not from the XAS
schema behind them: `order_id`, `vehicle_code`, `sales_model`, `status`, the order's
`eta_dealer` (its promised date) and the car's `available_by` (its arrival).

The XAS names are deliberately NOT used, and one of them is why. Follow the promise
through the existing MCP path: XAS stores it as `DueDateTime`; `datasource.py:383`
reads that and `datasource.py:466` writes it into the pull under the key
`"DeliveryDate"`; `flatten.py:111` reads `DeliveryDate` into `Order.delivery_date`.
But XAS *also* has a real `DeliveryDate`, which `docs/mcp-field-spec.md:42` calls
"reporting only — **not** the promise … listed so it is never mistaken for
`DueDateTime`". So the pull's `DeliveryDate` and XAS's `DeliveryDate` are different
things, and `SKILL.md:47` tells the agent "the promise is `DueDateTime`, not
`DeliveryDate`" while the code it describes reads `DeliveryDate`. Each statement is
true in its own layer; together they are a trap.

The rule that avoids repeating it: **a snapshot field is named after the column the
data actually uses, and the MCP path translates into it.** That translation lives in
`flatten.py` and nowhere else, so there is one place to look.

---

## File structure

| File | Change | Responsibility after |
|---|---|---|
| `xas_allocation/read_export.py` | **create** | Pure reader: two CSV streams → `Snapshot`. Joins order→car on `vehicleCode`, matches on `SalesModel`, prunes cars nobody wants, derives lateness, counts drops, reports incumbent conflicts. |
| `xas_allocation/snapshot.py` | modify | `Order` loses `customer`, `customer_id`, `priority`, `so_id`, `line`; gains `order_id`. `delivery_date` → `eta_dealer`. `Unit.eta_dealer` → `available_by`, and `Unit` gains `status`. |
| `xas_allocation/solver.py` | modify | Loses `fence_of`, `is_locked_in`, `repairability`, `_combined_boosts`. Gains `_combined_priority`. λ charges for churn. **`break_cost_of` and the whole `bump` path untouched.** |
| `xas_allocation/session.py` | modify | Reports grow the no-car axis; lose the locked-in tables and the dealer column. `bump_candidates` reworked for the new fields. |
| `xas_allocation/decisions.py` | modify | DECIDE-2 retired. DECIDE-3 and DECIDE-13 stay — both are load-bearing for bump mode. `PRIORITY_WEIGHT` → named steps. |
| `xas_allocation/overrides_schema.json` | modify | `boosts` → `priority`. **`bump` and `break_cost` stay.** |
| `xas_allocation/flatten.py` | modify | Field renames only; the MCP/rich path stays. |
| `alloc_tools.py` | modify | Two mount paths, a two-file snapshot command, `summarize` rewritten off the CSVs. |
| `web.py` | modify | Mounts two files; the per-session cache holds both. |
| `skills/xas-allocation/SKILL.md` | rewrite | Three scenarios, CSV field names, no VPO/VGR, the two modes with no-bump as today's default, priority as a lever. |
| `tests/test_bump.py` | modify | Kept. Updated for the removed fence and the new fields; gains a test that the fence removal did not weaken the no-bump default. |
| `tests/test_read_export.py` | **create** | The new reader, including every gotcha above that is testable. |

---

## Test helpers: what exists, and the one you must add

`tests/test_report.py` already has `_order(oid, model, priority, promised)`, `_unit(vid, model, planned)` and a no-argument `_snapshot()` returning a fixed four-order book. `_order` splits `oid` on the last `-` to build `so_id` + `line` and maps a name prefix to a dealer — **both break in Task 5 and Task 6**, so those tasks update these three helpers first.

Add this one parameterised builder to `tests/test_report.py` in Task 1 and reuse it everywhere after. It is the only new helper this plan introduces.

```python
def _book(
    *, orders: int, allocated: int, late: int, promise_days_out: int = 60, spare_cars: int = 0
) -> Snapshot:
    """A book of `orders` orders, `allocated` of which hold a car, `late` of those
    on a car that lands past the promise, plus `spare_cars` free cars.

    One sales model throughout, so every car is eligible for every order and a test
    is about the rule under test rather than about eligibility. Dates derive from a
    fixed NOW so a test never depends on the day it runs.
    """
    NOW = date(2026, 8, 25)
    promised = NOW + timedelta(days=promise_days_out)
    book, units, incumbent = [], [], {}
    for i in range(orders):
        oid = f"ORDER-{i}"
        book.append(Order(order_id=oid, sales_model="SM1", eta_dealer=promised))
        if i < allocated:
            car = f"CAR-{i}"
            arrives = promised + timedelta(days=8) if i < late else promised - timedelta(days=7)
            units.append(
                Unit(
                    vehicle_id=car,
                    vehicle_classification="Vehicle",
                    sales_model="SM1",
                    available_by=arrives,
                    status="Dealer Order Confirmation",
                )
            )
            incumbent[oid] = car
    for j in range(spare_cars):
        units.append(
            Unit(
                vehicle_id=f"SPARE-{j}",
                vehicle_classification="Vehicle",
                sales_model="SM1",
                available_by=promised - timedelta(days=14),
                status="Available For Sale",
            )
        )
    return Snapshot(orders=book, units=units, incumbent=incumbent, disruption={}, now=NOW, meta={})
```

`_book` above is written against the FINAL shapes (`order_id`, `eta_dealer`, `available_by`, `status`, no dealer, no priority). When you add it in Task 1, write it with **that task's** current names — `so_id=`/`line=`, `customer=`, `priority=`, `delivery_date=`, `eta_dealer=` on the unit, no `status=` — and let Tasks 5, 6 and 7 migrate it with everything else.

Where a task below writes `_snapshot(orders=20, ...)` or a named variant, read it as:

| Written as | Means |
| --- | --- |
| `_snapshot(promise_days_out=120, car_arrives_days_out=100)` | `_book(orders=1, allocated=1, late=0, promise_days_out=120)` |
| `_snapshot(promise_days_out=3, car_arrives_days_out=20)` | `_book(orders=1, allocated=1, late=1, promise_days_out=3)` |
| `_two_orders_one_car()` | `_book(orders=2, allocated=0, late=0, spare_cars=1)` |
| `_snapshot(orders=20, allocated=0, late=0)` | `_book(orders=20, allocated=0, late=0, spare_cars=20)` |
| `_snapshot(orders=30, allocated=20, late=10)` | `_book(orders=30, allocated=20, late=10, spare_cars=10)` |

Task 7's `_write`, `_order` and `_car` are new to `tests/test_read_export.py`:

```python
ORDER_COLS = [
    "OrderId",
    "vehicleCode",
    "modelId.name",
    "SalesModel",
    "vehicleColor.code",
    "vehicleColor.name",
    "description",
    "",
    "modelId.wrntyLimitationKm",
    "etaDealer",
]
VEHICLE_COLS = [
    "_id",
    "vehicleCode",
    "vin",
    "modelId.code",
    "modelId.name",
    "SalesModel",
    "inventoryStatus",
    "inv status label",
    "vehicleColor.code",
    "vehicleColor.name",
    "status.code",
    "status.name",
    "description",
    "",
    "availableBy",
    "modelId.wrntyLimitationKm",
]


def _order(oid, car="", model="SM1", eta="2026-10-01"):
    return {"OrderId": oid, "vehicleCode": car, "SalesModel": model, "etaDealer": eta}


def _car(code, model="SM1", status="Dealer Order Confirmation", available_by="2026-09-20"):
    return {
        "vehicleCode": code,
        "SalesModel": model,
        "status.name": status,
        "availableBy": available_by,
    }


def _write(tmp_path, orders, vehicles):
    """Write both CSVs with the export's real headers, return (orders, vehicles).

    Full headers on purpose: the reader must tolerate the export's unnamed column
    and ignore what it does not need, and a test writing only the four columns it
    cares about would not prove that.
    """
    paths = []
    for name, cols, rows in (
        ("orders.csv", ORDER_COLS, orders),
        ("vehicles.csv", VEHICLE_COLS, vehicles),
    ):
        path = tmp_path / name
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\r\n")
            w.writeheader()
            w.writerows(rows)
        paths.append(path)
    return tuple(paths)
```

---

## Task 1: Pin the no-bump default BEFORE removing the fence

The fence is about to go. Before it does, prove that the property everyone assumes it provides is actually provided by the free-set definition — otherwise Task 2 silently starts displacing orders nobody authorised.

**Files:**
- Modify: `tests/test_report.py` (add `_book`), `tests/test_bump.py`
- Test: `tests/test_bump.py`

- [ ] **Step 1: Add `_book` to `tests/test_report.py`**

Use the code from the Test helpers section, written against today's field names (`so_id`/`line`, `customer=""`, `priority="C"`, `delivery_date=`, and `eta_dealer=` on the `Unit`, no `status=`). Export it for reuse: `from tests.test_report import _book` works because `tests/` has no `__init__.py` and pytest adds the rootdir — if that import fails in your environment, move `_book` to `tests/conftest.py` as a fixture instead.

- [ ] **Step 2: Write the failing test**

In `tests/test_bump.py`:

```python
def test_an_on_time_allocated_order_is_untouched_without_authorisation():
    """The no-bump guarantee, stated as a property rather than a side effect.

    It is NOT the time fence that provides this — it is the free-set definition in
    `partition`: an on-time allocated order is not disrupted, not deferred and not
    unallocated, so it is never freed and its car stays consumed. This test must
    keep passing after the fence is removed (Task 2). If it starts failing there,
    the fence WAS load-bearing and Task 2's premise is wrong.
    """
    snap = _book(orders=2, allocated=2, late=1, promise_days_out=200, spare_cars=1)
    rp = solver.partition(snap, {})
    assert "ORDER-1" not in rp.free_orders, "the on-time order must not be freed"
    assert snap.incumbent["ORDER-1"] not in rp.free_units, "nor may its car be taken"


def test_authorisation_frees_an_on_time_order_and_its_car():
    """The other mode, same line of code. `bump` is how a planner says who may be
    displaced; 'who is allowed' is this filter's contents and nothing else."""
    snap = _book(orders=2, allocated=2, late=1, promise_days_out=200, spare_cars=0)
    rp = solver.partition(snap, {"bump": {"orders": ["ORDER-1"]}})
    assert "ORDER-1" in rp.free_orders
    assert snap.incumbent["ORDER-1"] in rp.free_units
```

- [ ] **Step 3: Run them**

Run: `uv run pytest tests/test_bump.py -k "untouched or authorisation" -v`
Expected: **PASS both**, on today's code. That is the point — these tests document existing behaviour so Task 2 cannot break it unnoticed. If the second one fails, the fence is blocking the authorisation, which is G4 and Task 2 fixes it; note the failure and move on.

- [ ] **Step 4: Commit**

```bash
git add tests/test_report.py tests/test_bump.py
git commit -m "tests: pin the no-bump default as a property of the free set"
```

---

## Task 2: Retire the time fence

**Files:**
- Modify: `xas_allocation/solver.py`, `xas_allocation/session.py`, `xas_allocation/decisions.py`
- Test: `tests/test_bump.py`, `tests/test_report.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_near_delivery_on_time_order_can_be_bumped_once_authorised():
    """G4: the fence fired BEFORE the authorisation check, so an authorised bump on
    an order close to delivery silently no-oped. `session.py:243` records three of
    them. Removing the fence is what fixes this."""
    snap = _book(orders=2, allocated=2, late=1, promise_days_out=5, spare_cars=0)
    rp = solver.partition(snap, {"bump": {"orders": ["ORDER-1"]}})
    assert "ORDER-1" in rp.free_orders


def test_a_late_order_close_to_delivery_is_movable():
    """The old fence froze anything inside 14 days, on-time or not."""
    snap = _book(orders=1, allocated=1, late=1, promise_days_out=3, spare_cars=1)
    assert solver.partition(snap, {}).free_orders == ["ORDER-0"]
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_bump.py tests/test_report.py -k "near_delivery or close_to_delivery" -v`
Expected: 2 FAIL — both orders are excluded by `is_locked_in`.

- [ ] **Step 3: Delete the fence**

In `xas_allocation/solver.py`, delete `fence_of` and `is_locked_in` entirely. In `partition`, delete the guard and its comment:

```python
        # The fence stops a KEPT promise being churned this close to delivery —
        # see `is_locked_in` for why an already-late allocation is not protected.
        # A locked-in row is excluded here, which also keeps its car out of
        # `free_units` below: nothing can take it.
        if is_locked_in(o, snapshot.now, units.get(assigned) if assigned else None):
            continue
```

Nothing replaces it. Add this to `partition`'s docstring so the next reader does not re-add a wall:

```python
    """Decide what is pinned vs free (§1, §5) from data rules + the override.

    What protects an on-time allocation is the free-set definition below, not a
    wall: such an order is not disrupted, not deferred and not unallocated, so it
    is never freed and its car stays consumed by its pin. The 14/42-day time fence
    that used to sit above this was removed 2026-08-25 (DECIDE-2, retired) — it
    fired BEFORE the authorisation check, so it overrode scope, defer and bump
    alike, and three authorised bumps no-oped because of it. `forbid: no_move` is
    the deliberate per-order version of what the fence did by calendar.
    """
```

- [ ] **Step 4: Run and watch them pass, and check Task 1 still holds**

Run: `uv run pytest tests/test_bump.py -v`
Expected: PASS, including `test_an_on_time_allocated_order_is_untouched_without_authorisation` from Task 1. **If that one now fails, stop** — the fence was load-bearing after all and this task's premise is wrong.

- [ ] **Step 5: Leave break cost exactly as it is**

Nothing to do, and that is the decision. `break_cost_of`, the `brk` lines in `_solve_one`, `RepairPlan.break_cost`, `D.BREAK_COST`, the `break_cost` schema key and DECIDE-3 all stay — and after this task they are more reachable than before, because an authorised bump on an on-time order is no longer blocked. `break_cost` is what stops the solver taking that car unless the rescue is worth 200 weighted late-days.

- [ ] **Step 6: Delete `repairability` and the locked-in report branches**

`repairability`'s only input was the fence, so it can only return `"movable"` now. Delete it, and in `session.py`:
- `Discrepancy`: delete `fixable` and `reason`.
- `find_discrepancies`: delete the `reason = repairability(...)` line and both constructor fields; sort key becomes `key=lambda d: (-d.days_late, d.order_key)`.
- `discrepancy_report`: delete the `stuck` list and its whole `if stuck:` block; drop the fixable/stuck split.
- `planner_report`: delete `reason_of` and `stuck`; `no_car` becomes every still-late order; delete `" ({len(stuck)} locked in — can't be re-slotted)"` from the headline.
- Delete `_why_late`, the `| {_why_late(...)} |` column and its header cell, and the `why_late` key from `plan_rows`.
- `_caveat`: delete the `if stuck:` branch.
- `bump_candidates`: delete the `is_locked_in` term from `still_late` and the four-line comment above it — the workaround for a bug that no longer exists.

- [ ] **Step 7: Retire DECIDE-2**

Delete the DECIDE-2 entry from `decisions.py` along with `FROZEN_MAX_DAYS` and `SLUSHY_MAX_DAYS`.

- [ ] **Step 8: Run the suite and commit**

Run: `uv run pytest -q`
Expected: failures in `test_report.py` / `test_time_scale.py` where a test asserted a specific change count. Confirm each new number by hand from the cost formula before editing the expectation — a re-baseline that pastes the new output proves nothing, because you would do the same thing if you had broken the solver.

```bash
git add -A
git commit -m "solver: retire the time fence, which was overriding bump authorisation"
```

---

## Task 3: Make λ price churn instead of a missed date

**Files:**
- Modify: `xas_allocation/solver.py`, `skills/xas-allocation/SKILL.md` (one line; the rest in Task 9)
- Test: `tests/test_report.py`

λ is meant to be the "don't reshuffle everything" dial, and the report sells it as one. It currently charges when the car's arrival differs from the **promised date**, which is true of 98.9% of eligible pairings — a near-constant on almost every option, so it cannot steer a choice. Measured on `scenario-mixed`, weighted late-days is 193.0 at every λ from 0 to 100 and the change count wobbles 120–124 with no direction.

- [ ] **Step 1: Write the failing test**

```python
def test_raising_lambda_reduces_the_number_of_cars_moved():
    """λ is the churn dial the report presents as a trade-off table. If the sweep
    returns the same numbers at every setting, the planner is being shown a choice
    that is not a choice."""
    snap = _book(orders=12, allocated=12, late=12, promise_days_out=90, spare_cars=12)
    points, _ = solver.lambda_sweep(snap, {})
    changes = [p.n_changes for p in points]
    late = [p.weighted_late_days for p in points]
    assert changes[0] > changes[-1], f"churn must fall as lambda rises, got {changes}"
    assert late[0] < late[-1], f"and lateness must rise as the price of it, got {late}"
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_report.py -k raising_lambda -v`
Expected: FAIL — `changes` comes back flat.

- [ ] **Step 3: Charge for a different car**

`arc_cost_float` cannot see the incumbent, so the λ term moves to `_solve_one` where `inc_uid` is already in scope. Delete the λ block from `arc_cost_float` along with its `lam` and `now` parameters:

```python
def arc_cost_float(
    order: Order,
    unit: Unit,
    boosts: dict[str, float],
    not_before: date | None,
    unit_days: int = 1,
) -> float:
    """§2 cost for one order->unit arc, in float space (scaled to int later).

    Lateness (convex) plus a small linear earliness term, both quantized to whole
    time-scale units. The churn term is NOT here: λ prices taking a DIFFERENT car
    than the order had, which needs the incumbent, so it is added in `_solve_one`.
    It used to be applied here against the promised date, which meant it landed on
    98.9% of arcs as a near-constant and steered nothing.
    """
```

In `_solve_one`, inside the per-unit loop:

```python
            c = arc_cost_float(o, u, rp.boosts, nb, rp.unit_days)
            if uid != inc_uid:
                # λ: the price of moving this order onto a different car. This IS
                # the churn dial the report presents as "N moves for M late-days";
                # charging per changed allocation is what makes the sweep a real
                # curve instead of six identical rows.
                c += lam
                c += brk  # and the break cost, if it had an on-time car
```

Note that `lam` and `brk` now ride the same condition — both are prices for giving up the incumbent. Keep them as separate additions: `lam` is swept, `brk` is a constant a planner owns.

- [ ] **Step 4: Run and watch it pass**

Run: `uv run pytest tests/test_report.py -k raising_lambda -v`
Expected: PASS. On `data/scenario-mixed` the sweep should now read roughly 122/193, 98/211, 90/237, 78/317, 62/487, 50/701.

- [ ] **Step 5: Verify against real data**

```bash
uv run python -m xas_allocation.session
```
Expected: the Pareto frontier shows a monotonic trade-off, not six identical rows.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "solver: lambda prices a changed allocation, not a missed date"
```

---

## Task 4: Priority becomes a named-step lever

**Files:**
- Modify: `xas_allocation/decisions.py`, `xas_allocation/solver.py`, `xas_allocation/overrides_schema.json`, `xas_allocation/session.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing test**

```python
def test_priority_defaults_to_normal_for_every_order():
    snap = _book(orders=2, allocated=0, late=0, spare_cars=1)
    assert solver.effective_weight(snap.orders[0], {}) == 1.0


def test_a_boosted_order_wins_the_only_car():
    """The step must actually change the outcome, not merely the weight."""
    snap = _book(orders=2, allocated=0, late=0, spare_cars=1)
    override = {"priority": [{"order": "ORDER-1", "step": "urgent"}]}
    assert solver.solve(snap, override).plan.get("ORDER-1")
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_report.py -k priority -v`
Expected: FAIL — `effective_weight` reads `order.priority`.

- [ ] **Step 3: Define the steps**

In `decisions.py`, replace `PRIORITY_WEIGHT`:

```python
# Priority is set by the PLANNER, never read from data — the export has no priority
# column. Named steps, because the weight is not what a planner should sign off on;
# the ratio is. Lateness costs W·days^1.5 and the solver equalises marginal cost, so
# a "high" order is protected until a normal one runs about 4x as late, an "urgent"
# one until about 16x. Those two multiples are the reviewable numbers.
PRIORITY_STEPS: dict[str, float] = {"normal": 1.0, "high": 2.0, "urgent": 4.0}
DEFAULT_PRIORITY_STEP = "normal"
```

- [ ] **Step 4: Resolve it in the solver**

Replace `_combined_boosts`:

```python
def _combined_priority(override: dict) -> dict[str, float]:
    """order name -> weight multiplier, from the override's `priority` list.

    An unknown step falls back to normal rather than raising: a typo must not take
    the turn down, and the agent confirms the translation in words before solving."""
    out: dict[str, float] = {}
    for entry in override.get("priority", []):
        name = entry.get("order")
        if name is None:
            continue
        step = str(entry.get("step", D.DEFAULT_PRIORITY_STEP))
        out[str(name)] = D.PRIORITY_STEPS.get(step, D.PRIORITY_STEPS[D.DEFAULT_PRIORITY_STEP])
    return out
```

and `effective_weight`:

```python
def effective_weight(order: Order, priority: dict[str, float]) -> float:
    """W(o). Every order starts at 1.0; only a planner-set priority raises it.

    The escalation terms that used to ride here — `n_prior_delays` (α),
    `times_rescheduled` (γ), `days_backordered` (β) — have no column in the export
    and are always 0, so they are not read. See DECIDE-1 and DECIDE-11, both parked.
    """
    return priority.get(order.key, 1.0)
```

Rename `RepairPlan.boosts` → `priority` and follow the three call sites in `_solve_one` and two in `session.py`.

- [ ] **Step 5: Delete the three escalation terms**

`effective_weight` above reads only the priority step, so `n_prior_delays`,
`days_backordered` and `times_rescheduled` are unread the moment Step 4 lands.
Delete them rather than parking them: a weight escalation nothing can trigger is a
comment pretending to be code. Decided 2026-08-25 — if a column like these ever
appears in the export, adding the term back is a smaller job than keeping dead
machinery correct in the meantime.

Remove from `snapshot.py::Order`: the three fields, their `to_dict` keys and their
`from_dict` reads. Keep `price` with a default, since a report may still show it:

```python
    price: float = 0.0  # display only; never a cost-model input
```

A dataclass cannot have a defaulted field before a non-defaulted one, so `price` is
declared **last**.

Then remove what this change orphans:
- `decisions.py`: `ALPHA`, `BETA`, `GAMMA`, `AGING_MODE`, and the DECIDE-1 and
  DECIDE-11 entries (Task 10 confirms).
- `datasource.py:455`: the `for extra in ("n_prior_delays", "days_backordered",
  "times_rescheduled")` loop — it would write keys nothing reads.
- `scenario_engine/generate.py:211`: the random `times_rescheduled` emission. Worth
  knowing before deleting it — that random draw was the **only** thing that ever set
  the field, on any source. The repair loop never wrote it back, so reschedule
  fairness was never once driven by a real reschedule.
- `git rm tests/test_reschedule_fairness.py` (2 tests).

- [ ] **Step 6: Swap the schema key**

In `overrides_schema.json`, replace `boosts` with:

```json
"priority": {
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "order": {"type": "string"},
      "step": {"enum": ["normal", "high", "urgent"]}
    },
    "required": ["order", "step"],
    "additionalProperties": false
  }
}
```

- [ ] **Step 7: Update the steering summary**

In `session.py::_steering_summary`, replace the `boosts` branch:

```python
priority = override.get("priority") or []
if priority:
    parts.append(
        "raised priority on " + ", ".join(f"{p.get('order')} ({p.get('step')})" for p in priority)
    )
```

Leave the `bump` branch alone — it is still a lever.

- [ ] **Step 8: Run and commit**

```bash
uv run pytest -q
git add -A
git commit -m "solver: priority is a planner lever with named steps, not a data field"
```

---

## Task 5: Drop the dealer, and rework `bump_candidates`

**Files:**
- Modify: `xas_allocation/snapshot.py`, `xas_allocation/session.py`, `xas_allocation/solver.py`, `xas_allocation/flatten.py`, `xas_allocation/overrides_schema.json`
- Test: `tests/test_report.py`, `tests/test_scope.py`, `tests/test_bump.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_report_names_the_order_and_its_model_since_there_is_no_dealer():
    snap = _book(orders=2, allocated=2, late=1, spare_cars=1)
    text = session.planner_report(snap, session.solve(snap, {}))
    assert "()" not in text and "( )" not in text
    assert "| Order | Model |" in text


def test_bump_candidates_lists_who_holds_a_car_that_would_help():
    """Half of bump mode is showing the planner who could be displaced. With no
    dealer and no data priority it must still name the order, its car, when that
    car arrives, and who it would rescue."""
    snap = _book(orders=2, allocated=2, late=1, promise_days_out=90)
    cands = session.bump_candidates(snap, session.solve(snap, {}))
    assert cands and set(cands[0]) >= {"row", "model", "vehicle", "arrives", "would_rescue"}
    assert "customer" not in cands[0] and "priority" not in cands[0]
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_report.py tests/test_bump.py -k "no_dealer or bump_candidates_lists" -v`

- [ ] **Step 3: Remove the fields**

In `snapshot.py::Order` delete `customer`, `customer_id` and `priority` from the dataclass, `to_dict` and `from_dict`. In `flatten.py` delete the `owner` / `customer` / `customer_id` / `priority` reads and constructor arguments.

- [ ] **Step 4: Remove the display**

In `session.py`: delete `_cid_to_name` and its argument through `_steering_summary`; delete `customer`/`priority` from `Discrepancy`; replace every `| {x.customer} ({x.priority}) ` cell with `| {x.sales_model} ` and each `| Dealer (priority) ` header with `| Model `. Five tables: two in `discrepancy_report`, three in `planner_report`. In `_caveat` replace `f"{o.customer} order {o.key}"` with `f"Order {o.key}"` and delete the priority `rank` dict.

- [ ] **Step 5: Rework `bump_candidates`**

Keep the function and its purpose. Replace the priority rank with the two things left that matter — how much the candidate's own promise would suffer, and whether the planner has already raised its priority:

```python
def bump_candidates(
    snapshot: Snapshot, result: SolveResult, override: dict | None = None
) -> list[dict]:
    """Orders holding a car that would rescue a still-late order, so the agent can
    ask with a concrete list rather than in the abstract.

    Ordered by least harm first: an order the planner has NOT raised, whose own car
    is furthest from its promise, is the cheapest one to take from. Priority is a
    planner input now, so a raised order sorts last — being explicitly boosted is
    exactly the signal not to displace it.
    """
    priority = _combined_priority(override or {})
    ...
    cands.sort(key=lambda c: (priority.get(c["row"], 1.0), -c["slack_days"], c["row"]))
```

Each row carries `row`, `model`, `vehicle`, `arrives`, `slack_days` (days between its car landing and its own promise), `would_rescue`. Drop `customer`, `priority`, `rescue_customer`.

- [ ] **Step 6: Remove the scope dimension**

Drop `"customers"` from `solver._FILTER_DIMS`, delete the `customers` block from `_matches`, and delete `customers` from both the `scope` and `bump` properties in `overrides_schema.json`.

- [ ] **Step 7: Run and commit**

```bash
uv run pytest -q   # G18: 29 `customer` references in tests
git add -A
git commit -m "snapshot: drop the dealer; bump candidates rank by slack, not priority letter"
```

---

## Task 6: Flat OrderId and the field renames

**Files:**
- Modify: `xas_allocation/snapshot.py`, `xas_allocation/solver.py`, `xas_allocation/session.py`, `xas_allocation/flatten.py`
- Test: `tests/test_flatten.py`, `tests/test_report.py`

- [ ] **Step 1: Write the failing test**

```python
def test_an_orders_key_is_its_OrderId_verbatim():
    """The export keys an order by OrderId with no line level. A composed key like
    '502375-0' is not an id the planner can look up, and it lands in the report."""
    order = Order(order_id="502375", sales_model="T6480J1BXLX0018", eta_dealer=date(2026, 10, 1))
    assert order.key == "502375"
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_flatten.py -k OrderId -v`
Expected: FAIL — `Order.__init__` has no `order_id`.

- [ ] **Step 3: Reshape `Order` and rename the two date fields**

Replace `so_id` + `line` with `order_id: str`, and:

```python
    @property
    def key(self) -> str:
        """The order key IS the export's OrderId. One level, no composition."""
        return self.order_id
```

Rename `Order.delivery_date` → `eta_dealer` (the export's column for the promise) and `Unit.eta_dealer` → `available_by` (the export's column for the car's arrival). Update `to_dict`/`from_dict` on both.

**Both renames land in the same commit as their call sites.** `eta_dealer` currently exists on `Unit`; for the duration of a partial edit it would mean two different things at once, which is the exact confusion the rename exists to remove.

- [ ] **Step 4: Follow the renames**

`solver.py`: `tardiness`, `earliness`, `arc_cost_float`, `_matches` (`from_date`/`to_date`), `disrupted_order_keys`, `_solve_one`'s `weighted_late`.
`session.py`: `find_discrepancies`, `_result_phrase`, `planner_report`, `plan_rows`, `bump_candidates`.
`flatten.py`: the `Order(...)` / `Unit(...)` constructors and the `promise_of` / `eta_of` maps.

- [ ] **Step 5: Collapse the two key levels**

```python
def names_order(order: Order, names: set[str] | dict) -> bool:
    """Whether a set of order names refers to this order.

    One level now: the export keys an order by OrderId and there is no VSO above
    it. This stays a function rather than an `in` check because every place that
    names an order by string goes through it — a pin, a forbid, a priority step, a
    bump authorisation, the derived late set — and they must all agree.
    """
    return bool(names) and order.key in set(names)
```

Simplify `not_before_for` to a single lookup, and delete the stale three-level paragraph from `_matches`' docstring (G11).

- [ ] **Step 6: Run and commit**

```bash
uv run pytest -q   # G18: 15 `so_id` references in tests
git add -A
git commit -m "snapshot: flat OrderId; promise is eta_dealer, arrival is available_by"
```

---

## Task 7: Read the two CSV streams

**Files:**
- Create: `xas_allocation/read_export.py`, `tests/test_read_export.py`
- Modify: `xas_allocation/snapshot.py`, `alloc_tools.py`, `web.py`, `xas_allocation/session.py`
- Test: `tests/test_read_export.py`, `tests/test_tool_contract.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_an_order_with_no_vehicle_code_is_kept_with_no_incumbent(tmp_path):
    """The unallocated scenario's whole point: such an order is DEMAND, not an
    unreadable row."""
    snap = read_export.read_pair(*_write(tmp_path, orders=[_order("1")], vehicles=[]))
    assert [o.key for o in snap.orders] == ["1"]
    assert snap.incumbent == {}


def test_the_trailing_space_on_the_status_name_is_stripped(tmp_path):
    """G16: 'Available For Sale ' is on ~86% of the export's available cars."""
    paths = _write(tmp_path, orders=[], vehicles=[_car("C1", status="Available For Sale ")])
    assert read_export.read_pair(*paths).units[0].status == "Available For Sale"


def test_a_car_no_order_wants_is_pruned_and_counted(tmp_path):
    """`datasource.map_response` already prunes to the reachable sub-problem; the
    CSV reader must too, or `exclusion_note` loses the 'X of Y cars match something
    someone ordered' line. 368 of the export's 3523 cars are in this state."""
    paths = _write(
        tmp_path,
        orders=[_order("1", model="SM1")],
        vehicles=[_car("C1", model="SM1"), _car("C2", model="NOBODY-WANTS")],
    )
    snap = read_export.read_pair(*paths)
    assert [u.vehicle_id for u in snap.units] == ["C1"]
    assert snap.meta["excluded"]["units_seen"] == 2
    assert snap.meta["excluded"]["units_kept"] == 1


def test_an_available_car_that_an_order_still_claims_is_reported(tmp_path):
    """G15: a car cannot be both free supply and someone's incumbent. The scenario
    scripts never emit this; the live export is not guaranteed."""
    paths = _write(
        tmp_path, orders=[_order("1", car="C1")], vehicles=[_car("C1", status="Available For Sale")]
    )
    snap = read_export.read_pair(*paths)
    assert snap.incumbent == {}
    assert "C1" in str(snap.meta["conflicts"])


def test_an_order_naming_a_missing_car_loses_its_incumbent_under_the_known_bucket(tmp_path):
    """G14: `exclusion_note` reads only `order_drops` and `flatten_skips`."""
    paths = _write(tmp_path, orders=[_order("1", car="GONE")], vehicles=[])
    snap = read_export.read_pair(*paths)
    assert snap.meta["excluded"]["order_drops"]["allocation_to_a_dropped_vehicle"] == 1


def test_lateness_is_derived_from_the_incumbent_against_the_promise(tmp_path):
    paths = _write(
        tmp_path,
        orders=[_order("1", car="C1", eta="2026-10-01")],
        vehicles=[_car("C1", available_by="2026-10-09")],
    )
    assert read_export.read_pair(*paths).disruption["disrupted_orders"] == ["1"]


def test_the_same_two_files_read_twice_give_an_identical_snapshot(tmp_path):
    """The invariant. A dict-ordering or set-iteration leak here breaks every
    reproducibility claim downstream."""
    paths = _write(
        tmp_path,
        orders=[_order(str(i), car=f"C{i}") for i in range(20)],
        vehicles=[_car(f"C{i}") for i in range(20)],
    )
    a = json.dumps(read_export.read_pair(*paths).as_dict(), sort_keys=True)
    b = json.dumps(read_export.read_pair(*paths).as_dict(), sort_keys=True)
    assert a == b
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_read_export.py -v`
Expected: 7 FAIL — no module `read_export`.

- [ ] **Step 3: Add `status` to `Unit`**

```python
    status: str = ""  # the export's `status.name`, stripped. Free supply is
    # "Available For Sale"; the other two pool statuses mean some order holds it.
    # Carried so a bump authorisation can select BY STATUS later without re-reading
    # the data — the one forward-looking field in this plan, and it is one string.
```

- [ ] **Step 4: Write the reader**

Create `xas_allocation/read_export.py`. It ships in the skill bundle, so it imports nothing beyond the standard library and `.snapshot`.

```python
"""Join the XAS export's two CSV streams into the solver snapshot — pure code.

The two streams stay separate all the way into the sandbox and are joined HERE, by
code, never by the model: the same rule `flatten` follows for the MCP-shaped pull,
for the same reason. If the join were re-derived per turn, the plan would stop
being a function of the snapshot.

    orders.csv    OrderId, vehicleCode, SalesModel, etaDealer, ...
    vehicles.csv  vehicleCode, SalesModel, status.name, availableBy, ...

Four mappings and one derivation:
  * the promise is `etaDealer` -- on the ORDER row. This COLLIDES with the MCP
    shape, where `EtaDealer` is a vehicle's arrival; here the car's arrival is
    `availableBy`. Getting these two the wrong way round is the single easiest
    mistake to make against this data.
  * eligibility is `SalesModel` equality on both sides, never `modelId.code`, which
    holds the model above it and matches no order.
  * a car is in the pool only in the three statuses below, and is FREE supply only
    when available; an order's `vehicleCode` is its incumbent.
  * lateness is DERIVED -- an order whose car lands past its promise. The export
    records no delay manifest, so there is nothing to read.
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import date
from pathlib import Path

from .snapshot import Order, Snapshot, Unit, parse_date

POOL_STATUSES = {"available for sale", "dealer order confirmation", "dealer reservation"}
AVAILABLE = "available for sale"


def _day(stamp: str) -> date:
    return parse_date(stamp[:10])


def _status(row: dict) -> str:
    """Stripped, because 'Available For Sale ' with a trailing space is on ~86% of
    the export's available cars and an unstripped compare halves the pool."""
    return (row.get("status.name") or "").strip()


def read_pair(
    orders_path: str | Path, vehicles_path: str | Path, now: date | None = None
) -> Snapshot:
    """``now`` is the snapshot's "today": it decides which cars have already arrived
    (hard, expensive to take) and which are still shipping (soft, free) — which is
    the break cost an authorised bump pays, so it is not cosmetic.

    Neither CSV carries a capture date, so it must be passed in. `web.py` does the
    fetch and therefore knows when: it stamps the fetch time and the snapshot command
    passes it here. ``date.today()`` is the OFFLINE fallback only, for a hand-run
    against files on disk.

    Sensitivity, measured on the export: arrival dates come in clusters, so moving
    ``now`` by a week flips ZERO cars between hard and soft; moving it by a month
    flips 859 of 3523. Day-to-day drift is harmless, month-scale drift is not.
    """
    at = now or date.today()
    drops: Counter[str] = Counter()
    conflicts: list[dict] = []

    # --- pass 1: the pool ----------------------------------------------------
    pool: list[tuple[str, dict]] = []
    seen_units = 0
    for row in csv.DictReader(Path(vehicles_path).open(newline="")):
        seen_units += 1
        status = _status(row)
        if status.lower() not in POOL_STATUSES:
            drops["car_not_for_sale"] += 1
            continue
        if not (row.get("SalesModel") or "").strip():
            drops["vehicle_without_a_model"] += 1
            continue
        if not (row.get("availableBy") or "").strip():
            drops["vehicle_without_an_arrival_date"] += 1
            continue
        pool.append(((row.get("vehicleCode") or "").strip(), row))

    # --- pass 2: demand ------------------------------------------------------
    orders: list[Order] = []
    claims: dict[str, str] = {}
    seen_orders = 0
    for row in csv.DictReader(Path(orders_path).open(newline="")):
        seen_orders += 1
        model = (row.get("SalesModel") or "").strip()
        promise = (row.get("etaDealer") or "").strip()
        if not model:
            drops["no_model_on_the_order"] += 1
            continue
        if not promise:
            drops["no_promised_date"] += 1
            continue
        oid = (row.get("OrderId") or "").strip()
        orders.append(Order(order_id=oid, sales_model=model, eta_dealer=_day(promise)))
        car = (row.get("vehicleCode") or "").strip()
        if car:  # no car is DEMAND, not a dropped row
            claims[oid] = car

    # --- prune to the reachable sub-problem ----------------------------------
    # A car no surviving order wants can never be allocated, since eligibility is
    # hard equality, so dropping it is lossless. Mirrors `datasource.map_response`
    # deliberately: the same rule has to hold whichever source the pull came from.
    wanted = {o.sales_model for o in orders}
    units = [
        Unit(
            vehicle_id=code,
            # Display, plus the break cost bump mode charges (DECIDE-3): a car
            # already on the lot vs one still shipping.
            vehicle_classification="Vehicle" if _day(row["availableBy"]) <= at else "Future",
            sales_model=row["SalesModel"].strip(),
            available_by=_day(row["availableBy"]),
            status=_status(row),
        )
        for code, row in pool
        if row["SalesModel"].strip() in wanted
    ]
    drops["no_order_wants_this_model"] = len(pool) - len(units)
    by_code = {u.vehicle_id: u for u in units}

    # --- resolve the incumbent ----------------------------------------------
    incumbent: dict[str, str] = {}
    for oid, car in claims.items():
        unit = by_code.get(car)
        if unit is None:
            drops["allocation_to_a_dropped_vehicle"] += 1
            continue
        if unit.status.lower() == AVAILABLE:
            # Both free supply and someone's incumbent. Reported, never resolved by
            # guessing which is true — treating it as allocated would take a car out
            # of the pool that the export says is for sale, and treating it as free
            # would double-book it.
            conflicts.append({"vehicle": car, "orders": [oid], "status": unit.status})
            continue
        incumbent[oid] = car

    # --- lateness, derived ---------------------------------------------------
    promise_of = {o.key: o.eta_dealer for o in orders}
    disruption = {
        "disrupted_orders": sorted(
            key for key, car in incumbent.items() if by_code[car].available_by > promise_of[key]
        )
    }

    unmatched = sorted(o.key for o in orders if o.sales_model not in {u.sales_model for u in units})
    excluded: dict = {
        "orders_seen": seen_orders,
        "orders_kept": len(orders),
        "units_seen": seen_units,
        "units_kept": len(units),
    }
    if any(drops.values()):
        excluded["order_drops"] = {k: v for k, v in sorted(drops.items()) if v}
    if unmatched:
        excluded["orders_with_no_eligible_car"] = unmatched
    meta: dict = {"now": at.isoformat(), "excluded": excluded}
    if conflicts:
        meta["conflicts"] = conflicts
    return Snapshot(
        orders=orders,
        units=units,
        incumbent=incumbent,
        disruption=disruption,
        now=at,
        meta=meta,
    )
```

- [ ] **Step 5: Run and watch them pass**

Run: `uv run pytest tests/test_read_export.py -v`
Expected: 7 PASS.

- [ ] **Step 6: Add the one new drop reason to the phrasebook**

`session.py::DROP_PHRASES` already has every other name the reader emits — that is why it reuses them. Add:

```python
    "car_not_for_sale": "car not for sale (delivered, registered, in dispute or demo)",
    "no_order_wants_this_model": "no order wants that model",
```

- [ ] **Step 7: Two mounts in the tool contract**

In `alloc_tools.py` replace `MOUNT_PATH` with two paths:

```python
ORDERS_MOUNT = "/workspace/orders.csv"
VEHICLES_MOUNT = "/workspace/vehicles.csv"
```

`mount_candidates(path)` already takes a path, so it needs no change. Rename `flatten_command` to `snapshot_command()`; it resolves BOTH files from their candidate lists, exits with a message naming whichever is missing, and calls `read_export.read_pair`. Keep the bounded package self-location — **never `rglob` from `/`**, which once swept the container and killed the shell — and keep writing `snapshot.json` to the working directory.

- [ ] **Step 8: Rewrite `summarize` off the CSVs**

This is what crosses into the agent's context, so it carries the counts the turn-1 reply must state and nothing more. Fields: `snapshot` (the command), `snapshot_path`, `now`, `orders`, `orders_with_no_car`, `orders_late`, `vehicles`, `available`, `excluded`, `conflicts`, `sales_models`. No dealer map (no dealer column) and no disruption block (no manifest). `orders` counts rows of `orders.csv`, which is the same grain the snapshot uses — fixing G9, where the old summary counted `Σ Quantity` while `flatten` built one order per line.

- [ ] **Step 9: Mount both files in `web.py`, and stamp the fetch time**

`MOUNTED_PULL_FILENAME` becomes two names; the `_pull_by_session` cache holds both;
`_download_pull` fetches both; the container spec gets two `{"type": "file", ...}`
entries. Update the comment at `web.py:99` — the pull is now two mounts, and the
reporting lane still has none.

Record `now` here, where the fetch happens:

```python
        # The snapshot's "today". Stamped host-side at fetch time because neither CSV
        # carries a capture date and the sandbox must not guess: `now` decides which
        # cars count as already-arrived, which sets the break cost an authorised bump
        # pays. One stamp per session, so every turn of a repair cycle shares it —
        # the same reason the rows themselves are fetched once.
        pulled_at = date.today().isoformat()
```

Pass it through `summarize`'s `now` field and into the snapshot command as an
argument, so `read_pair` never reaches its offline fallback inside a session.

- [ ] **Step 10: Run the contract tests and commit**

Run: `uv run pytest tests/test_tool_contract.py tests/test_agent_contract.py -v`
Verify by hand that the declared tool and the registered implementation still come from the same constants — a drift parks the session on an idle that never times out.

```bash
git add -A
git commit -m "read the export's two CSV streams, joined in the sandbox"
```

---

## Task 8: Make the reports scenario-agnostic

**Files:**
- Modify: `xas_allocation/session.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_turn_one_reports_orders_with_no_car_even_when_nothing_is_late():
    """G1: `find_discrepancies` iterated the incumbent, so 20 carless orders printed
    'No orders are late'."""
    snap = _book(orders=20, allocated=0, late=0, spare_cars=20)
    text = session.discrepancy_report(snap)
    assert "20 order(s) have no car" in text
    assert "No orders are late" not in text


def test_the_headline_counts_orders_given_a_car_not_only_delays_repaired():
    """G2: with no disruption the headline read '0 of 0 delayed orders now on
    time' after allocating twenty cars."""
    snap = _book(orders=20, allocated=0, late=0, spare_cars=20)
    text = session.planner_report(snap, session.solve(snap, {}))
    assert "0 of 0" not in text
    assert "now have a car" in text


def test_a_mixed_book_reports_both_shapes_separately():
    snap = _book(orders=30, allocated=20, late=10, spare_cars=10)
    text = session.discrepancy_report(snap)
    assert "10 order(s) have no car" in text
    assert "10 order(s) are late" in text
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_report.py -k "no_car or headline or mixed_book" -v`
Expected: 3 FAIL.

- [ ] **Step 3: Iterate orders, not the incumbent**

```python
def find_discrepancies(snapshot: Snapshot) -> list[Discrepancy]:
    """Every order the planner has to act on, in two shapes: it has no car at all,
    or the car it has lands past its promise.

    Iterating ORDERS rather than the incumbent is the whole point. The old version
    walked `snapshot.incumbent`, so an order with no car could not appear — and a
    book of twenty carless orders reported as "no orders are late".
    """
    orders = snapshot.order_by_key()
    units = snapshot.unit_by_id()
    out: list[Discrepancy] = []
    for oid, o in sorted(orders.items()):
        uid = snapshot.incumbent.get(oid)
        if uid is None:
            out.append(
                Discrepancy(
                    order_key=oid,
                    sales_model=o.sales_model,
                    vehicle_id=None,
                    promised=o.eta_dealer,
                    now_arriving=None,
                    days_late=0,
                    shape="no_car",
                )
            )
            continue
        late = tardiness(o, units[uid])
        if late > 0:
            out.append(
                Discrepancy(
                    order_key=oid,
                    sales_model=o.sales_model,
                    vehicle_id=uid,
                    promised=o.eta_dealer,
                    now_arriving=units[uid].available_by,
                    days_late=late,
                    shape="late",
                )
            )
    out.sort(key=lambda d: (d.shape, -d.days_late, d.order_key))
    return out
```

Add `shape: str` to `Discrepancy`; make `vehicle_id` and `now_arriving` optional.

- [ ] **Step 4: Two sections in `discrepancy_report`**

`exclusion_note` stays first and unchanged — still mandatory. Then a `no_car` table (Order | Model | Promised) and a `late` table (Order | Model | Promised | Now arriving | Late). The all-clear line becomes "Every order has a car and every car meets its promised date." and prints only when BOTH lists are empty.

- [ ] **Step 5: Two headline clauses in `planner_report`**

```python
    carless = sorted(oid for oid in orders if not incumbent.get(oid))
    n_carless_filled = sum(1 for oid in carless if plan.get(oid))
    broken = sorted(oid for oid in disrupted if late_by(oid, incumbent.get(oid)))
    n_fixed = sum(1 for oid in broken if plan.get(oid) and not late_by(oid, plan[oid]))

    clauses = []
    if carless:
        clauses.append(f"{n_carless_filled} of {len(carless)} orders with no car now have a car")
    if broken:
        clauses.append(f"{n_fixed} of {len(broken)} late orders now on time")
    if not clauses:
        clauses.append("nothing needed changing")
```

Keep the `bumped` flag and its own line in the change list — it is real again, and a displacement must never be silent.

- [ ] **Step 6: Run and commit**

```bash
uv run pytest -q
git add -A
git commit -m "reports: an order with no car is visible on turn 1"
```

---

## Task 9: Rewrite the skill

**Files:**
- Modify: `skills/xas-allocation/SKILL.md`, `setup_agent.py`
- Test: `tests/test_agent_contract.py`

- [ ] **Step 1: Update the contract tests first**

```python
@pytest.mark.parametrize("phrase", ["deliveries", "sales order", "what is late", "which car"])
def test_alloc_description_carries_the_words_users_type(phrase):
    assert phrase in _description(setup_agent.ALLOC_SKILL_DIR / "SKILL.md").lower()


def test_alloc_skill_states_the_three_shapes_of_work():
    """One skill, three scenarios. A skill that only describes a disruption makes
    the agent narrate a repair over a book where nothing was disrupted."""
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for phrase in ("no car", "late", "both"):
        assert phrase in skill.lower()


def test_alloc_skill_asks_before_displacing_anyone():
    """Both modes are supported; only one is the default. The ask is the whole
    guardrail, and nothing structural backs it up."""
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Never do that uninvited" in skill


def test_alloc_skill_still_stops_a_status_question_at_the_report():
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "A question about the state stops at the discrepancy report." in skill
```

Delete the `"vpo"` entry from `test_reporting_description_disclaims_the_allocation_vocabulary` and the `"no VPO ids"` assertion.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_agent_contract.py -k "alloc_description or three_shapes or uninvited" -v`

- [ ] **Step 3: Rewrite the skill, section by section**

- **Frontmatter description:** drop "after a disruption" and all VPO/VGR wording; add "which car should this order get" and "these orders have no car".
- **The data:** two mounted files joined by the snapshot command. Keep "never fetch it yourself" and the one-frozen-picture rule verbatim.
- **The fields and the traps:** replace all four. New list — the promise is the ORDER's `etaDealer`; the car's arrival is `availableBy`; **the same word means different things on the two rows, and this is the one to get right**; eligibility is `SalesModel` equality, never `modelId.code`; a car is supply only in the three statuses and free supply only when `Available For Sale`.
- **What people ask for:** add rows for "which car for these orders" and "nothing is allocated" → run the snapshot command, print `discrepancy_report`, stop.
- **What the solver optimises:** delete the time-fence paragraph. Replace with one line: an order whose car arrives on time is left alone unless you are explicitly authorised to displace it; everything else is available. Keep lateness/earliness/churn, and state that λ prices moving a car onto a different one.
- **Steering:** `boosts` → `priority` with the three steps, and in plain words what they mean — a high order is protected until a normal one runs about four times as late.
- **Bumping — keep this section and sharpen it.** Both modes exist; no-bump is the default because the free set never includes an on-time allocated order. The sequence is unchanged: solve plainly, and only if something is still late, show who could be displaced (lowest cost first) and ask. Add that taking an on-time car is priced, not free, so the solver refuses a bump that does not earn its keep. Say that who may be bumped is the planner's list — for now given by name.
- **Delete outright:** every VPO/VGR paragraph and the `bumped`-column mention if the field name changes.
- **Keep verbatim:** "You do not decide allocations", "A question about the state stops at the discrepancy report", the `plan.json` read-it-never-retype-it rule, and "What is NOT in the plan, first, on turn 1". Those are the load-bearing guardrails and nothing here touches them.

- [ ] **Step 4: Update the system prompt**

In `setup_agent.py`, remove "vehicle purchase order" from `SYSTEM_PROMPT`'s routing words; add "which car" and "nothing allocated". The reporting-lane hard rule is untouched.

- [ ] **Step 5: Run the whole gate and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest -q
PYTHONPATH=. uv run python tests/test_invariant.py
git add -A
git commit -m "skill: three scenarios, export field names, two modes with no-bump default"
```

---

## Task 10: Prune the decisions register

**Files:**
- Modify: `xas_allocation/decisions.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing test**

The register is the module-level list `decisions.DECISIONS`, and a `Decision`'s identifier field is `key` (not `id`) — there is no accessor function:

```python
def test_the_register_has_no_entry_for_a_retired_mechanism():
    """A register that still lists the time fence sends the next reader to tune a
    number that no longer exists."""
    keys = {d.key for d in decisions.DECISIONS}
    assert "DECIDE-2" not in keys
    assert {"DECIDE-3", "DECIDE-13"} <= keys, "both are load-bearing for bump mode"
```

- [ ] **Step 2: Run, watch it fail, delete the DECIDE-2 entry, run again**

- [ ] **Step 3: Restate the two that bump mode depends on**

**DECIDE-3** (`BREAK_COST["hard"]=200`): the mechanism is settled and now more reachable than before, since the fence no longer blocks authorised bumps. The number is still unvalidated — it is the "how many weighted late-days is one broken promise worth" ratio, and a planner must own it before bump mode is offered to anyone.
**DECIDE-13** (no uninvited bumps): unchanged and still enforced, by the free-set definition rather than by the fence. Add that the fence used to over-enforce it, blocking bumps the planner had authorised.

- [ ] **Step 4: Retire DECIDE-1 and DECIDE-11 with their terms**

Both go, per Task 4 Step 5. Neither escalation term has any input and neither ever
did. Decided 2026-08-25: delete rather than park. Add a line to the register's header
noting that back-order aging and reschedule fairness were removed on that date, and
what would bring them back — an actual column, or the cross-turn store DECIDE-5 is
still about.

```python
def test_the_register_does_not_list_a_mechanism_with_no_input():
    keys = {d.key for d in decisions.DECISIONS}
    assert {"DECIDE-1", "DECIDE-2", "DECIDE-11"}.isdisjoint(keys)
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "decisions: retire the fence; DECIDE-3 and 13 restated for bump mode"
```

---

## Task 11: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `COMMANDS.md`

- [ ] **Step 1: `CLAUDE.md`**

Rewrite these bullets in "Invariants that bite": the mount bullet (one mount → two), ONE CAR PER LINE (one row of `orders.csv` is one order), the frozen-fence bullet (**retired — and it was overriding bump authorisation, which is why it went**), and the two-mapping-rules bullet (add the `etaDealer` collision). Add one new bullet: **what protects an on-time allocation is the free-set definition in `partition`, not a wall — and `break_cost` is the price when a planner lifts it.** Update the trust-level table if the mount count is named there.

- [ ] **Step 2: Note the latent tie-break issue (G17)**

Add a short bullet: the solver's choice between interchangeable cars is decided by node ordering, so "same snapshot + same override → byte-identical plan" holds for a fixed OR-Tools build and a fixed book, not across either. Measured: solving per sales model gives identical weighted late-days (193.0, 905.0 on two books) but a different assignment for 72 of 126 free orders. Nothing here depends on it; making the tie-break explicit is the fix if it ever matters.

- [ ] **Step 3: `README.md` and `COMMANDS.md`**

Add the snapshot command's two-file form to the run list. The scenario-generator sections need no change — they already emit exactly the two files this reads.

- [ ] **Step 4: Verify the whole gate and commit**

```bash
uv run python -m scenario_engine.real_mixed --empty 50 --late 50 --days-late 1-20 --extra-free 50 --subset 400 --available-pct 40
uv run ruff format . && uv run ruff check . && uv run pytest -q
PYTHONPATH=. uv run python tests/test_invariant.py
git add -A
git commit -m "docs: two streams, no fence, both bump modes"
```

---

## What bump mode still needs (deferred by decision, not designed away)

Everything above leaves both modes working. What is deliberately NOT done yet is **who** may be bumped beyond naming them one at a time:

- **A `statuses` dimension on the `bump` filter**, so a planner can say "any car currently in Dealer Order Confirmation or Dealer Reservation may be taken" instead of listing orders. This is one new key in `_FILTER_DIMS` and one block in `_matches`, reading `Unit.status` — which Task 7 already carries onto the snapshot for exactly this.
- **The agent asking which mode before the first solve.** It changes the plan, so it cannot be a silent default. The skill's Bumping section is where that lives.
- **A validated `BREAK_COST["hard"]`.** 200 is a guess. Until a planner owns it, an authorised bump's threshold is arbitrary.

## Decisions settled while writing this

All three questions the first draft left open were answered on 2026-08-25:

1. **Field names** — the export's own column names. See "Naming" above for the
   `DeliveryDate` collision that argues for the rule.
2. **Reschedule fairness** — removed, not parked. A column like it may appear later;
   adding the term back then is smaller than keeping it alive now.
3. **`now`** — stamped host-side by `web.py` at fetch time and passed in, because
   neither CSV carries a capture date and the sandbox must not guess.

Nothing in this plan is blocked on a decision. What is deliberately deferred is
listed under "What bump mode still needs" above.

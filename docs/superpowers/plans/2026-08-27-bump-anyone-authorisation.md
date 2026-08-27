# Fleet-Wide Bump Authorisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a planner say "yes, you may bump anyone" in one value — `may_move.also: true` — and have that permission expire at the end of the turn it was solved in.

**Architecture:** No new set logic. `partition` already implements the three cases exactly: an order with no car is free (`allocations.get(oid) is None`), an order whose car now lands late is free (`oid in disrupted`), and a settled on-time order is free **only** if `may_move.also` matches it. The single gap is that `also` accepts a *filter* and `_filter_active({})` deliberately treats an empty one as "nobody" — so there is no honest way to authorise everybody. Today it can only be faked as `also: {"from_date": "2000-01-01"}`. This plan adds `true` as a sentinel on that one existing key: three lines in `partition`, a phrase in the plain-words summary, one pure helper for the expiry, and the docs. No new override key, no new precedence rule, default behaviour byte-identical.

**Tech Stack:** Python 3.11, OR-Tools min-cost-flow, pytest, uv, ruff. No new dependencies.

---

## As built — 27 August 2026

Implemented by four subagents, one per independent task. **194 passed**, invariant 4/4, ruff clean.

Deviations from the plan as written, all deliberate:

- **Nothing was committed.** Every "Commit" step was skipped: the tree holds another engineer's uncommitted CSV-export refactor across the same files, so `git add` would have swept their work into these commits. The changes are in the working tree, unstaged.
- **The redeploy (Task 7, Step 3) was not run.** `setup_agent.py` mutates the live deployed agent, and the skill it would ship sits in that same half-committed tree. It needs a human to decide when.
- **Task 2's `n_changes == 0` assertion was wrong** and is corrected above. It was transplanted from `test_may_move.py`'s settled fixture onto `test_bump.py`'s book, where `ORDER_HI` is late and therefore already free. The implemented assertion — same book as no authorisation — is the stronger form and diverges the moment a bump genuinely fires.
- **Task 6, Step 4's insertion point** landed after the paragraph rather than mid-paragraph: the quoted sentence is followed on the same line by "It is the only thing that must survive…", and splitting there would have orphaned it.
- Intermediate suite totals ran one above the plan's (187 baseline, then 189 / 190 / 194) because Task 6's contract test landed before Tasks 2-4 started. The final 194 is the plan's number.

### Known gap, not implemented

`may_move.also` accepts anything: `also: "anyone"` coerces to `{}` and frees nobody, **silently**. It fails closed, but a mistyped sentinel then reads as an authorisation that did nothing — the failure mode `weight_of_step` deliberately raises on for priority steps. Left as-is pending a decision, since making a previously-ignored shape throw is a behaviour change beyond this plan.

---

## Baseline: read this before starting

This plan targets the **uncommitted working tree of 2026-08-27**, after the CSV-export refactor — *not* `4efa340`. Confirm the tree still matches before Task 1:

```bash
uv run pytest -q                      # expect: 182 passed
grep -n '_FILTER_DIMS = ' xas_allocation/solver.py
#   expect: ("models", "orders", "from_date", "to_date")   <- no `customers`
grep -n 'return CFG\["break_cost"\]' xas_allocation/solver.py
#   expect: one hit — break_cost is a single number, the hard/soft split is gone
grep -n 'order_id=oid' tests/test_may_move.py
#   expect: a hit — order keys are flat OrderIds, not {so_id}-{line}
```

If any of those disagree, the refactor moved again: re-read `partition`, `_matches` and the test helpers before writing code, because every code block below quotes them verbatim.

Facts that shape the plan, all verified on this tree:

| verified | value |
|---|---|
| `_filter_active({})` | `False` — so `{}` means nobody, and the sentinel must be a distinct value |
| settled book, `also: {}` | free set `[]` |
| settled book, match-everything filter | free set `['500001', '500002']`, and **0 changes** |
| tight book, authorised + urgent | the bump fires — permission plus a weight difference is what pays for it |

That third row is the whole reason the feature is safe: opening the book does not move anything on its own.

---

## What already works, and is not touched

| the planner's case | how it is already handled |
|---|---|
| order with no car → allocate it | `allocations.get(oid) is None` → in the default free set |
| order whose car is now late → re-allocate it | `oid in disrupted` → in the default free set |
| settled on-time order → sometimes bumpable | `may_move.also` — the only widener |

`never` beats `only` beats `also` stays the contract sentence. The second gate stays too: an authorised bump happens only if it lowers total cost, paying `break_cost` (200) for the disturbed promise.

---

## File structure

| File | Change | Responsible for |
|---|---|---|
| `xas_allocation/solver.py` | Modify `partition`, ~line 289 | the sentinel: `also: true` means every settled order is in play |
| `tests/test_may_move.py` | Add 4 tests | membership and precedence for the new form |
| `tests/test_bump.py` | Add 2 tests + 4 for the helper | that it still only bumps when it pays, and that the permission expires |
| `xas_allocation/session.py` | Modify `_steering_summary`, add `carry_forward` | say it in plain words without crashing; spend the authorisation |
| `tests/test_report.py` | Add 1 test | the boolean form must be phrased before `_who` ever sees it |
| `xas_allocation/overrides_schema.json` | Modify `may_move.properties.also` | the contract the agent reads |
| `skills/xas-allocation/SKILL.md` | 4 edits | how to ask, how to compile the answer, that it lasts one turn |
| `setup_agent.py` | 1 prompt line | the standing hard rule |
| `tests/test_agent_contract.py` | Add 1 test | pin the prompt clause |
| `CLAUDE.md` | 1 bullet | the invariant note |

---

## Decisions already taken

- **Spelling: `true`.** Not `"anyone"`. A boolean is the smallest thing that cannot be confused with a filter.
- **Lifetime: one turn.** `may_move.also` is spent when it is solved. Every other key is a standing instruction until the planner changes it. The solved override still lands in `plan.json` (`save_plan` already stamps `"override"`), so the turn stays reproducible after the key is dropped.

---

## Task 1: `also: true` puts every settled order in play

**Files:**
- Modify: `xas_allocation/solver.py` (`partition`, the `may_move` unpacking and the free-set loop)
- Test: `tests/test_may_move.py`

- [x] **Step 1: Add `partition` to the test file's imports**

In `tests/test_may_move.py`, replace:

```python
from xas_allocation.solver import solve
```

with:

```python
from xas_allocation.solver import partition, solve
```

- [x] **Step 2: Write the failing tests**

Append to `tests/test_may_move.py`:

```python
def test_also_true_puts_every_settled_order_in_play():
    """The fleet-wide form. A filter can only name orders, models or a date range,
    so "yes, you may bump anyone" had to be faked as a filter matching everything
    — which reads as a date instruction nobody gave."""
    snap = _settled_snapshot()
    assert partition(snap, {}).free_orders == [], "the default protects a settled book"
    assert partition(snap, {"may_move": {"also": True}}).free_orders == [ORDER_A, ORDER_B]


def test_an_empty_also_filter_still_means_nobody():
    """`{}` must never widen. A half-built override — the key present, the filter
    not filled in yet — would otherwise open the whole book."""
    snap = _settled_snapshot()
    assert partition(snap, {"may_move": {"also": {}}}).free_orders == []


def test_never_beats_also_true():
    """Precedence is unchanged by the new form: an absolute hold still wins."""
    snap = _settled_snapshot()
    steer = {"may_move": {"also": True, "never": [ORDER_A]}}
    assert partition(snap, steer).free_orders == [ORDER_B]


def test_only_bounds_also_true():
    """`only` bounds the whole turn, the fleet-wide permission included."""
    snap = _settled_snapshot()
    steer = {"may_move": {"also": True, "only": {"orders": [ORDER_B]}}}
    assert partition(snap, steer).free_orders == [ORDER_B]
```

- [x] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_may_move.py -k "also_true or empty_also" -v`

Expected: `test_an_empty_also_filter_still_means_nobody` PASSES (that behaviour is already correct); the three `also_true` tests FAIL, all three with `AttributeError: 'bool' object has no attribute 'get'` raised inside `_filter_active`. That error is the point: today the value is assumed to be a dict.

- [x] **Step 4: Write the implementation**

In `xas_allocation/solver.py`, inside `partition`, replace:

```python
    may_move = override.get("may_move") or {}
    only = may_move.get("only") or {}
    also = may_move.get("also") or {}
    never = set(may_move.get("never") or [])
    narrowed = _filter_active(only)
    widened = _filter_active(also)
```

with:

```python
    may_move = override.get("may_move") or {}
    only = may_move.get("only") or {}
    # `also: true` is the FLEET-WIDE form: every settled order is in play this
    # turn. A filter stays the scoped form, and `{}` still means NOBODY — an
    # empty filter must never widen, or an override half-built by the agent
    # opens the whole book. That is why the sentinel is a distinct value rather
    # than "an empty filter matches everything".
    raw_also = may_move.get("also")
    bump_anyone = raw_also is True
    also = raw_also if isinstance(raw_also, dict) else {}
    never = set(may_move.get("never") or [])
    narrowed = _filter_active(only)
    widened = bump_anyone or _filter_active(also)
```

and replace the free-set test:

```python
        if not (needs_help or (widened and _matches(o, also))):
            continue
```

with:

```python
        if not (needs_help or (widened and (bump_anyone or _matches(o, also)))):
            continue
```

- [x] **Step 5: Update the `also` bullet in `partition`'s docstring**

Replace:

```
    * ``also`` WIDENS, inside ``only``: untouched orders the planner has
      EXPLICITLY authorised the solver to displace to rescue someone late
      (DECIDE-13). The solver moves one only if it lowers total cost, and pays
      ``break_cost`` to do it. This is the one place permission to displace is
      given; nothing is ever bumped without it.
```

with:

```
    * ``also`` WIDENS, inside ``only``: untouched orders the planner has
      EXPLICITLY authorised the solver to displace to rescue someone late
      (DECIDE-13). A filter names them; ``True`` means ANYONE still settled, for
      this turn only (see ``session.carry_forward``). The solver moves one only
      if it lowers total cost, and pays ``break_cost`` to do it. This is the one
      place permission to displace is given; nothing is ever bumped without it.
```

- [x] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_may_move.py -v`
Expected: PASS, all 14 — the four new ones and the ten that were already there.

- [x] **Step 7: Run the whole suite — the default must not have moved**

Run: `uv run pytest -q`
Expected: `186 passed`. A failure anywhere outside `test_may_move.py` means the default free set changed, which this task must not do.

- [ ] **Step 8: Commit**

```bash
git add xas_allocation/solver.py tests/test_may_move.py
git commit -m "may_move: also=true authorises bumping anyone this turn"
```

---

## Task 2: it still only bumps when it pays

No code. These two tests are why the feature is safe to hand a planner, and they belong beside the other DECIDE-13 tests.

**Files:**
- Test: `tests/test_bump.py`

- [x] **Step 1: Write the tests**

Append to `tests/test_bump.py`:

```python
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
    # NOT `n_changes == 0`: ORDER_HI is late, so it is already free by default
    # and swaps its late car for the equally-late spare at churn 0 — one change
    # that is the repair, not a bump. The honest form of the property is that the
    # fleet-wide permission produces the IDENTICAL book to no authorisation.
    assert result.plan == solve(snap, {}, churn_price=0).plan
```

- [x] **Step 2: Run them**

Run: `uv run pytest tests/test_bump.py -k also_true -v`
Expected: PASS both, with no code change — they characterise Task 1's behaviour.

- [ ] **Step 3: Commit**

```bash
git add tests/test_bump.py
git commit -m "tests: a fleet-wide authorisation still bumps only when it pays"
```

---

## Task 3: say it in plain words, without crashing the report

`_steering_summary` passes `may_move["also"]` straight to `_who`, which reads `filt.get("orders")`. Handed `True` that raises `AttributeError` and takes the whole planner report down — a worse failure than a wrong number, and it happens on every turn the new form is used.

**Files:**
- Modify: `xas_allocation/session.py` (`_steering_summary`)
- Test: `tests/test_report.py`

- [x] **Step 1: Write the failing test**

Append to `tests/test_report.py`:

```python
def test_the_fleet_wide_authorisation_is_said_in_plain_words():
    """`_who` takes a filter; handed `True` it raises and the whole report dies.
    So the boolean form must be phrased BEFORE `_who` is ever reached. The
    planner also has to see that this permission is for one turn, since it is the
    only key that expires."""
    snap = _snapshot()
    steer = {"may_move": {"also": True}}
    cyc = run_cycle(snap, steer)
    report = planner_report(snap, cyc.chosen, steer)
    assert "anyone still settled" in report
    assert "this turn" in report
    low = report.lower()
    leaked = [t for t in JARGON if t.lower() in low]
    assert not leaked, f"solver jargon leaked into planner reply: {leaked}"
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_report.py -k fleet_wide -v`
Expected: FAIL with `AttributeError: 'bool' object has no attribute 'get'` from `_who`.

- [x] **Step 3: Write the implementation**

In `xas_allocation/session.py`, inside `_steering_summary`, replace:

```python
    if may_move.get("also"):
        parts.append(f"allowed bumping {_who(may_move['also'])}")
```

with:

```python
    # `is True` FIRST: `_who` reads filter keys off a dict, so handed the
    # fleet-wide sentinel it raises and takes the whole report with it.
    if may_move.get("also") is True:
        parts.append("allowed bumping anyone still settled, this turn")
    elif may_move.get("also"):
        parts.append(f"allowed bumping {_who(may_move['also'])}")
```

- [x] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_report.py -k fleet_wide -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add xas_allocation/session.py tests/test_report.py
git commit -m "report: phrase the fleet-wide authorisation before _who sees it"
```

---

## Task 4: the permission expires at the end of the turn

**Files:**
- Modify: `xas_allocation/session.py` (new function, place it directly after `run_cycle`)
- Test: `tests/test_bump.py`

- [x] **Step 1: Write the failing tests**

Append to `tests/test_bump.py`:

```python
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
```

Add `carry_forward` to the imports at the top of `tests/test_bump.py`, replacing:

```python
from xas_allocation.session import bump_candidates
```

with:

```python
from xas_allocation.session import bump_candidates, carry_forward
```

- [x] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_bump.py -k carry_forward -v`
Expected: FAIL at collection — `ImportError: cannot import name 'carry_forward'`

- [x] **Step 3: Write the implementation**

In `xas_allocation/session.py`, immediately after `run_cycle`, add:

```python
def carry_forward(override: dict | None) -> dict:
    """The override for the NEXT turn: this turn's bump authorisation is spent.

    ``may_move.also`` is permission for ONE solve (decided 2026-08-27). Every
    other key is a standing instruction that holds until the planner changes it,
    which is why this drops exactly one thing. A permission that quietly persisted
    would mean a later turn displacing a settled order on the strength of a
    sentence said three turns ago.

    Returns a NEW object: the override that was solved is stamped into
    ``plan.json`` by ``save_plan``, and that record is what makes the turn
    reproducible, so it must not be edited in place.
    """
    out = {key: value for key, value in (override or {}).items() if key != "may_move"}
    may_move = override.get("may_move") if override else None
    kept = {key: value for key, value in (may_move or {}).items() if key != "also" and value}
    if kept:
        out["may_move"] = kept
    return out
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_bump.py -k carry_forward -v`
Expected: PASS, all four.

- [x] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: `193 passed`

- [ ] **Step 6: Commit**

```bash
git add xas_allocation/session.py tests/test_bump.py
git commit -m "session: carry_forward spends the bump authorisation"
```

---

## Task 5: the schema says `true` is legal

Nothing validates against this file — it is the contract the agent reads, so it has to be right prose *and* right JSON Schema.

**Files:**
- Modify: `xas_allocation/overrides_schema.json` (`properties.may_move.properties.also`)

- [x] **Step 1: Replace the `also` block**

Replace the whole `"also"` value inside `may_move.properties` with:

```json
        "also": {
          "description": "WIDEN the set (inside 'only'): settled orders the planner has EXPLICITLY authorized the solver to displace to rescue someone late (DECIDE-13). Two forms — `true` means ANYONE still settled, a filter names who. This is the ONE place permission to displace is granted: ASK before setting it. It lasts ONE TURN — drop it once the plan is solved (`session.carry_forward`) — and it is permission, not an instruction: the solver displaces someone only when it lowers total cost, paying break_cost for the promise it disturbs. Absent = nobody may be bumped, which is the default. `{}` also means nobody: an empty filter never widens.",
          "oneOf": [
            {
              "const": true,
              "description": "The fleet-wide form: every settled order is in play for this turn."
            },
            {
              "type": "object",
              "additionalProperties": false,
              "properties": {
                "models": {
                  "type": "array",
                  "items": { "type": "string" },
                  "description": "sales_models whose orders may be displaced."
                },
                "orders": {
                  "type": "array",
                  "items": { "type": "string" },
                  "description": "explicit order ids, e.g. ['502377']."
                },
                "from_date": {
                  "type": "string",
                  "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                  "description": "Earliest promised date in play, inclusive."
                },
                "to_date": {
                  "type": "string",
                  "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                  "description": "Latest promised date in play, inclusive."
                }
              }
            },
            { "type": "null" }
          ]
        }
```

- [x] **Step 2: Verify it is still valid JSON and still parses to the same three keys**

Run:

```bash
uv run python -c "
import json; d = json.load(open('xas_allocation/overrides_schema.json'))
assert set(d['properties']) == {'priority', 'may_move', 'churn_price'}, d['properties'].keys()
also = d['properties']['may_move']['properties']['also']
assert [f.get('const', f.get('type')) for f in also['oneOf']] == [True, 'object', 'null']
print('schema ok')
"
```

Expected: `schema ok`

- [ ] **Step 3: Commit**

```bash
git add xas_allocation/overrides_schema.json
git commit -m "schema: may_move.also accepts true, and says it lasts one turn"
```

---

## Task 6: the skill and the prompt

**Files:**
- Modify: `skills/xas-allocation/SKILL.md` (4 places)
- Modify: `setup_agent.py` (the settled-order hard rule)
- Test: `tests/test_agent_contract.py`

- [x] **Step 1: The steering table row**

In `skills/xas-allocation/SKILL.md`, in the three-keys table, replace the `may_move` row:

```
| `may_move` | `{only, also, never}` — who is in play. The default with this absent is the orders that need help: late, or with no car. `only` and `also` take the same filter `{models, orders, from_date, to_date}`; `never` takes a list of order ids. |
```

with:

```
| `may_move` | `{only, also, never}` — who is in play. The default with this absent is the orders that need help: late, or with no car. `only` and `also` take the same filter `{models, orders, from_date, to_date}`; `also` can instead be `true`, meaning anyone still settled; `never` takes a list of order ids. |
```

- [x] **Step 2: The `also` bullet**

Replace the `**`also` WIDENS, inside `only`.**` bullet with:

```
- **`also` WIDENS, inside `only`.** The orders the planner has authorised you to
  displace to rescue someone late. **This is the only way anyone gets bumped, and
  you ASK first** — `bump_candidates` gives you the concrete list to ask with.
  Their answer compiles to a filter if they named who, or to `true` if they said
  "whoever it takes". **It lasts this turn only:** run
  `session.carry_forward(override)` after the plan is out and carry the result
  into the next turn, so a permission given once is not still open three turns
  later. Say so when you confirm it.
```

- [x] **Step 3: The bumping procedure**

In the "Bumping — ask first" section, replace step 3:

```
3. Compile their answer into `may_move.also`. The solver then displaces one only
```

with:

```
3. Compile their answer into `may_move.also` — the named orders, or `true` if
   they authorised anyone. The solver then displaces one only
```

- [x] **Step 4: The carrying-forward section**

In "Carrying the instructions forward", after the sentence "There is no history to replay and no order to get wrong.", insert:

```
One key is the exception: `may_move.also` is permission for a single solve, so
`session.carry_forward(override)` returns the object to carry into the next turn
with that permission spent. Everything else stands until they change it.
```

- [x] **Step 5: The prompt**

In `setup_agent.py`, replace this line of `SYSTEM_PROMPT`:

```
- A settled order — it has a car and that car still meets the promise — is out of play and keeps it. Never BUMP one unless the planner authorized who may be displaced: list `session.bump_candidates`, ASK, compile the answer into `may_move.also`. A `never` they set earlier beats everything, including permission granted in the same breath.
```

with:

```
- A settled order — it has a car and that car still meets the promise — is out of play and keeps it. Never BUMP one unless the planner authorized it: list `session.bump_candidates`, ASK, compile the answer into `may_move.also` — the orders they named, or `true` for anyone. That permission is for ONE turn: spend it with `session.carry_forward` before the next one. A `never` they set earlier beats everything, including permission granted in the same breath.
```

- [x] **Step 6: Write the contract test**

Append to `tests/test_agent_contract.py`:

```python
def test_prompt_says_a_bump_authorisation_lasts_one_turn():
    """`may_move.also` is the only key that expires. If the prompt does not say
    so, the agent carries the permission forward like everything else and a later
    turn displaces a settled order on the strength of one old sentence."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "`may_move.also`" in prompt
    assert "`true` for anyone" in prompt
    assert "permission is for ONE turn" in prompt
    assert "session.carry_forward" in prompt
```

- [x] **Step 7: Run the contract and phrasebook tests**

Run: `uv run pytest tests/test_agent_contract.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add skills/xas-allocation/SKILL.md setup_agent.py tests/test_agent_contract.py
git commit -m "skill/prompt: authorise anyone with true, and spend it after the turn"
```

---

## Task 7: the working notes, then the gate

**Files:**
- Modify: `CLAUDE.md`

- [x] **Step 1: Extend the `may_move` bullet**

In `CLAUDE.md`, inside the `**`may_move` precedence is part of the contract...**` bullet, replace this exact two-sentence tail:

```
nothing. `never` is absolute, and it is the only way to hold an order that is
  itself late. `tests/test_may_move.py` pins all three.
```

with:

```
nothing. `never` is absolute, and it is the only way to hold an order that is
  itself late. `also` takes a filter or the fleet-wide `true`, and it is the ONE
  key that EXPIRES: `session.carry_forward` drops it after the solve, because a
  permission that persisted would bump a settled order on the strength of a
  sentence said three turns ago. `{}` is not `true` — an empty filter widens
  nothing, so a half-built override cannot open the book.
  `tests/test_may_move.py` pins all three, and `tests/test_bump.py` the expiry.
```

- [x] **Step 2: Run the full gate**

```bash
uv run pytest                                       # expect 194 passed
PYTHONPATH=. uv run python tests/test_invariant.py  # expect 4/4
uv run ruff format . && uv run ruff check .
```

Expected: all green. `ruff format` will report `docs/superpowers/plans/2026-08-25-allocation-rework.md` as reformatted — that file is a dated record; revert it with `git checkout -- docs/superpowers/plans/2026-08-25-allocation-rework.md`.

- [ ] **Step 3: Redeploy — the skill and the prompt changed**

```bash
uv run python setup_agent.py
```

Expected: it prints the agent and skill ids. **This is not optional:** `agents.update()` preserves omitted array fields, so a skill edit that is not re-sent does nothing to the live agent.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "claude.md: the fleet-wide authorisation, and the one key that expires"
```

---

## Non-goals

- **No car-side authorisation.** "Are these *cars* allowed in the solver" cannot be its own gate: a car reaches the pool only by its order being freed, and letting a car in while its order stays pinned is a double-booking that trips the solver's own self-check. The car side needs nothing.
- **No third level.** An "only cars that have not arrived yet" middle setting died with the hard/soft break-cost split on 2026-08-27; there is one `break_cost` now, so there is nothing for that level to price differently.
- **`bump_candidates` is unchanged.** It stays the tool for asking about a *scoped* bump. For the fleet-wide question the agent asks with counts, not a list.
- **No expiry enforcement in the solver.** `partition` is pure and knows nothing about turns; the expiry is a session-level helper the skill tells the agent to call.

## Open question

`carry_forward` is a helper the agent must remember to call — the same shape of rule as "print the exclusion note", and it will be forgotten some fraction of the time. The structural alternative is for `repair_and_report` to return the next override alongside the reply, which changes a signature the skill documents as returning one string. Worth revisiting if a live session is seen carrying an authorisation into a second turn.

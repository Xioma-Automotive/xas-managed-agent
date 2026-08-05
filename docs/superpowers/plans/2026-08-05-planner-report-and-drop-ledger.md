# Planner report + drop the ledger — Implementation Plan

> Fixes from the run analysis. Scope: (1) a single `planner_report()` that emits
> finished, jargon-free markdown; (2) mark **fixable vs stuck** in the discrepancy
> map on turn 1; (4) skill rule to call the helper, not hand-derive; and **drop
> the ledger** (replace the per-turn replay with a plain combined-override the
> agent carries). NO code until approved.

**Why:** the live run was verbose/jargon-heavy, buried the real situation (all
near-term broken orders are frozen = unrepairable, revealed only on turn 4),
re-derived the solver's output by hand across many bash scripts ($106 / 6 min for
a 40-row toy), and used the ledger as a throwaway empty object every turn — while
the ledger it *would* persist lives in the ephemeral sandbox and wouldn't survive
a reclaim anyway.

---

## The invariant, restated

`plan = pure_function(data_snapshot, skill, ledger)`  →
**`plan = pure_function(data_snapshot, skill, steering)`**, where `steering` is a
single **combined override object** (weights / pins / forbid / lambda / scope /
bump). No append-only log, no replay, no TTL. Determinism still holds: same
snapshot + same override → byte-identical plan; discard the sandbox, re-pull,
re-apply the same override → same plan.

**Durability (be honest, don't hide it):** with the ledger gone, the combined
override lives only in the conversation — the agent shows it each turn and
carries it forward, recovering it from its own last shown object after a sandbox
reclaim. A durable host-side store (web.py keyed by session id, shipped in via the
pull) is the real fix and stays **deferred** — `DECIDE-5`. This plan does not
claim durability we don't have.

---

## Fix 1 — `planner_report()` (the finished reply)

New in `session.py`. One call the agent prints verbatim:

- `repair_and_report(snapshot, override=None) -> str` — runs the cycle and
  returns the whole markdown reply. (Thin: `run_cycle` then `planner_report`.)
- `planner_report(snapshot, cyc, override) -> str` — the renderer (testable).

Sections: **headline** (plain: what steering was applied + "N of M broken orders
on time; K still late, J of them locked-in") → **changed table** (Order · Dealer
(priority) · Was · Now · Promised · New allocation (VIN/PO-ref, was→now) · Result)
→ **still-late table** split into *locked-in (can't fix)* vs *no better car* →
**unchanged count** → **one caveat line**. No λ table, no solver internals.

A small **plain-language translator** turns internal reasons into business terms:
`frozen`→"locked in (within N days of delivery)"; committed→"already in final
prep"; `boosted`→"prioritized"; `times_rescheduled`→"already rescheduled before —
protected"; `Nd late`→"N days late"; bump→"moved to free a car for a delayed
order". This is what actually kills the jargon.

## Fix 2 — fixable vs stuck in the discrepancy map

- New `repairability(snapshot, order) -> "movable" | "frozen" | "committed"` in
  `solver.py` (reuses `fence_of` + `unit.committed` — the same rules `partition`
  already applies).
- `Discrepancy` gains `fixable: bool` + `reason: str`; `find_discrepancies` fills
  them.
- New `discrepancy_report(snapshot) -> str` (jargon-free): "The PO-151 delay broke
  8 orders. **5 are locked in** — too close to delivery to re-slot, they'll be
  ~21 days late (needs a call to those dealers): … **3 can be repaired**: …". This
  is the turn-1 truth the run hid until turn 4.

## Fix 4 — skill rule: call the helper, trust it

`SKILL.md` + system prompt: the per-turn flow is pull → flatten →
`discrepancy_report` → (steer) → `repair_and_report` → print. **Do not** write
ad-hoc analysis scripts or re-derive the solver's output; the sanctioned building
blocks are `discrepancy_report`, `repair_and_report`, `bump_candidates`. This
directly attacks the $106 hand-derivation.

## Fix 3 — drop the ledger

- **Delete** `xas_allocation/ledger.py`.
- `run_cycle(snapshot, ledger, current_date)` → `run_cycle(snapshot, override=None,
  current_date=None)`. `build_change_list` / `_order_reasons` take the override's
  boosts (from `partition`), lose the `who_touched` attribution trail (audit
  deferred).
- `main()` demo: accumulate ONE override dict across its turns (add a boost, then
  a scope) to show composition without a log. (Optional tiny pure
  `merge_overrides(base, new)` — accumulate lists, last-wins scalars — only if the
  demo reads cleaner; no persistence, no history.)
- Remove `ttl` from `overrides_schema.json` (its only evaluator was ledger
  replay) and the schema's "A ledger entry wraps one of these" line.

---

## Files touched

| File | Change |
| --- | --- |
| `xas_allocation/ledger.py` | **delete** |
| `xas_allocation/session.py` | drop ledger; `run_cycle(override)`; add `repairability` use, `discrepancy_report`, `planner_report`, `repair_and_report`, plain-language translator; rewrite `main()`; drop `who_touched` trail |
| `xas_allocation/solver.py` | add `repairability()`; docstrings ("ledger λ" → "override λ") |
| `xas_allocation/overrides_schema.json` | remove `ttl`; reword description |
| `xas_allocation/decisions.py` | rewrite DECIDE-5 (ledger → combined-override steering; durable store deferred) |
| `xas_allocation/__init__.py`, `snapshot.py` | invariant phrase; module list (drop `ledger`) |
| `alloc_tools.py`, `web.py` | reword ledger mentions (cosmetic) |
| `skills/xas-allocation/SKILL.md` | invariant; §8 flow; delete §7 ledger section → short "combined override" note; planner-facing → point to the helper; add "don't re-derive" + fixable/stuck map |
| `setup_allocation_agent.py` | SYSTEM_PROMPT: invariant + steering paragraph; "produce the report via the helper, don't hand-derive" |
| `CLAUDE.md`, `README.md` | invariant; drop ledger row/bullets; DECIDE-5; test counts |
| `tests/test_invariant.py` | **rewrite** — remove `Ledger`/TTL; determinism via (snapshot, override) + re-pull |
| `tests/test_report.py` | **new** — report sections; frozen order flagged locked-in; assert no jargon tokens (`λ`, `objective`); `repairability` classification |
| `tests/test_tool_contract.py` | docstring phrase only |
| unaffected | `test_scope.py`, `test_bump.py`, `test_reschedule_fairness.py`, `test_flatten.py` (all call `solve(snapshot, override)` directly) |

## Gotchas (surfaced from the code)

1. **Invariant phrase is load-bearing in ~10 files** — change all consistently
   (code docstrings, SKILL, CLAUDE, README, prompt, tests). Leave the historical
   `docs/superpowers/specs/*` and older plan docs as the record.
2. **`test_invariant.py` is 100% ledger-based** — full rewrite; the TTL test is
   deleted (TTL was a ledger feature); keep the sandbox-discard determinism test
   but express it with an override dict.
3. **`ttl` loses its evaluator** — remove it rather than leave it silently
   ignored (a no-op field that "does nothing" is a future trap).
4. **Durability is a real regression** — the ledger was ephemeral anyway, but
   removing it must be *documented*, not hidden: steering now survives only via
   the conversation/shown-override; host-side persistence deferred (DECIDE-5).
5. **Lost audit trail** — `who_touched` ("steered: turn N (Olga)") goes; the
   change line no longer says who steered a move. Acceptable (audit deferred),
   but note it.
6. **`run_cycle` signature change** ripples to `main()` and the invariant test.
7. **Earliness mislabel** (decision below) — the run sold a 4-week-early delivery
   as "✅ on time". `planner_report` should show `on time` but append
   "(N days early)" past a threshold so it isn't oversold.
8. **λ sweep** — `planner_report` never prints it; keep computing it (cheap) but
   render at most one plain sentence when the frontier genuinely trades off.
   (Every run so far was flat.)
9. **Scenario realism (adjacent, OPTIONAL, not in this change):** the demo
   dataset makes most disrupted orders frozen, so the honest report will read "5
   of 8 locked in" — correct but a dull demo. Consider later biasing the engine's
   disrupted promised-dates into the slushy/liquid band. Fix 2 at least makes the
   stuck-ness honest.
10. **Deploy:** package + skill + prompt change → re-run `setup_allocation_agent.py`.
    Data unchanged (no regenerate).

## Decisions to confirm

- **A. Earliness:** show "(N days early)" when a row lands >14 days before
  promised? (Recommend yes — otherwise the report oversells.)
- **B. `merge_overrides` helper:** include the tiny pure merge, or have the agent
  edit one override object in place? (Recommend: no helper; agent edits the one
  shown object — simplest, no history creeps back.)
- **C. Audit trail:** drop `who_touched` entirely now (recommend), or keep a
  who-steered note derived from the override? (Recommend drop; revisit if audit
  becomes a requirement — same trigger as durable persistence.)

## Verify

`uv run pytest` · `PYTHONPATH=. uv run python tests/test_invariant.py` ·
`uv run ruff format . && uv run ruff check .` · eyeball
`uv run python -m xas_allocation.session` output for jargon-free report.

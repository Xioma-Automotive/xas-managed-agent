# Time-scale (granularity) knob — Implementation Plan

> A planner-settable knob — `days` | `weeks` | `months` — that sets the
> **resolution the solver reasons at**. At week scale it measures every gap in
> weeks and stops seeing sub-week differences; at month scale, in months. It looks
> at *all* times through that lens, and the report speaks the same unit. This
> **changes the plan** (coarser). NO code until approved.
>
> **Independent of the earliness plan** (`2026-08-09-earliness-penalty.md`). This
> one sets the *resolution* of time; that one adds a *direction* (early is bad).
> They compose but ship separately.

**Driver:** planner works at different horizons — sometimes to the day, sometimes
"just get August roughly right." At a coarse scale the solver should stop fussing
over a few days and the report should talk in that unit.

---

## What it does

One knob, `time_scale ∈ {days, weeks, months}` (nominal unit-days `1 / 7 / 30`),
carried in the override. The solver converts every day-gap it prices into whole
**units** before costing:

```
units(days) = round(days / unit_days)          # sub-unit differences → 0
```

- **Lateness term** uses `units(tardiness_days)` instead of raw days — so at week
  scale, 3 days late → 0 units → treated on time; 10 days → 1 unit late.
- **Churn term** (`λ · [date ≠ promised]`) fires only when arrival and promise fall
  in **different buckets** — so a sub-unit date change isn't counted as churn.
- **Report** speaks the unit: "1 week late", "on time" = 0 units late. (Actual
  arrival dates can still be shown; the *durations* are in units.)
- The **discrepancy map** uses the same scale, so "what's broken" and "what the
  solve did" agree — no mixing exact-days detection with unit-scale solving.

**Measure deltas, don't snap to the calendar.** We round the *day-gap between two
dates*, not bucket dates to calendar weeks/months. That avoids the calendar-edge
trap (Aug 31 vs Sep 1 reading "a month apart") and makes `months ≈ 30 days` a fine
nominal unit rather than a ragged calendar boundary.

## The principle it respects

`plan = f(snapshot, skill, override)` holds: `time_scale` is a scalar in the
override, so it's part of the deterministic input. Same snapshot + same override
(scale included) → byte-identical plan. The knob is the *width* (planner-settable);
the *shape* (that the solver quantizes at all) is code — the usual split.

---

## Fix 1 — quantize the objective (`solver.py`)

- A small helper `scale_units(days, unit_days, mode="round") -> int`.
- `arc_cost_float(...)` gains `unit_days: int`; the lateness term uses
  `scale_units(tardiness, unit_days)`; the churn `[date ≠ promised]` test becomes a
  **same-bucket** test at the given scale.
- `solve()` reads `override.get("time_scale")` → maps to `unit_days` via
  `decisions.SCALE_DAYS`, threads it into every `arc_cost_float` call (like `lam`).
- **Hard structural rules stay in days** — the time fence (DECIDE-2) and committed
  (DECIDE-3) are physical (how close to delivery / pipeline stage), not a planner
  reasoning lens; quantizing them would change *feasibility*. Keep them in days.
  (Decision A — confirm.)

## Fix 2 — the knob (`overrides_schema.json`)

```json
"time_scale": {
  "type": "string",
  "enum": ["days", "weeks", "months"],
  "description": "Resolution the solver reasons at and the report speaks in.
    Coarser = ignores sub-unit differences (e.g. 'months' stops sweating a few
    days). Absent → DEFAULT_TIME_SCALE. Changes the plan, not just the wording."
}
```

The agent compiles "work August roughly" → `months`, "hit the dates" → `days`, and
shows it back in plain words.

## Fix 3 — constants + decision (`decisions.py`)

- `SCALE_DAYS = {"days": 1, "weeks": 7, "months": 30}`, `DEFAULT_TIME_SCALE =
  "days"` (default = today's behaviour exactly, so nothing shifts unless asked),
  and the rounding `mode`.
- New registry entry **DECIDE-14** ("time-scale granularity + rounding + does the
  fence quantize"), surfaced by `format_decisions()`.

## Fix 4 — report + discrepancy map speak the unit (`session.py`)

- `_result_phrase`, `planner_report`, and `discrepancy_report` read the scale from
  the override and render durations in units ("2 weeks late", "on time"). They
  already receive the override / snapshot; thread the scale in.
- "On time" now means **0 units late** at the chosen scale — see gotcha 2.

## Fix 5 — skill + prompt

- `SKILL.md`: document `time_scale` next to `lambda` / `scope`; state it changes the
  plan (coarser), measured on day-deltas (not calendar buckets), and that hard
  fences stay in days.
- SYSTEM_PROMPT: the agent sets `time_scale` from the planner's horizon; it does not
  round dates by hand.

---

## Files touched

| File | Change |
| --- | --- |
| `xas_allocation/solver.py` | `scale_units` helper; `arc_cost_float` quantizes tardiness + same-bucket churn test; `solve()` reads `time_scale` → `unit_days` and threads it |
| `xas_allocation/overrides_schema.json` | add `time_scale` |
| `xas_allocation/decisions.py` | `SCALE_DAYS`, `DEFAULT_TIME_SCALE`, rounding mode; new **DECIDE-14** |
| `xas_allocation/session.py` | report + discrepancy map render durations in the unit; "on time" = 0 units late |
| `skills/xas-allocation/SKILL.md` | document the knob; delta-rounding; fence stays in days |
| `setup_allocation_agent.py` | SYSTEM_PROMPT: set the scale, don't round by hand |
| `tests/test_time_scale.py` | **new** — see below |
| `tests/test_report.py` | durations render in the active unit |
| `CLAUDE.md`, `README.md` | one line + DECIDE-14 in the summary table |

## Tests (`tests/test_time_scale.py`)

1. **Sub-unit differences vanish.** At `weeks`, two cars 3 and 5 days late are
   equal-cost; at `days` they differ.
2. **Default = today.** `DEFAULT_TIME_SCALE = "days"` reproduces current plans
   byte-for-byte (a safety net for existing fixtures + the invariant test).
3. **Coarser ⇒ fewer distinctions.** A scenario where `months` yields a
   different (coarser) plan than `days`, asserting the intended collapse.
4. **Churn respects the bucket.** A within-unit date change is not counted as churn
   at that scale.
5. **Determinism.** Same snapshot + same override (with `time_scale`) → identical
   plan.
6. **Report unit.** Durations render in the active unit; "on time" = 0 units late.

## Gotchas

1. **Rounding mode is a real choice.** `round` (balanced), `floor` (generous — up to
   nearly a full unit late is free), `ceil` (strict — any lateness ≥ 1 unit).
   Recommend `round`; it's a Decision.
2. **"On time" coarsens — and can under-report.** At month scale a 20-day-late order
   reads "on time (0 months late)". Intended (that's the planner's lens) but it can
   hide real lateness, so: keep the *exact* date in the row even when the *duration*
   is in units, and never let the coarse label suppress a locked-in/stuck flag.
3. **Fence stays in days (Decision A).** Quantizing DECIDE-2/3 would change what's
   movable/feasible; keep physical constraints in days, quantize only the objective.
4. **`months = 30` is nominal, by design.** We round day-deltas, not calendar
   months, so no ragged month boundaries; document that "months" ≈ 30-day units.
5. **Determinism preserved** — quantization is pure integer math + the scale scalar.
   The invariant test's default-scale run is unchanged.
6. **One scale per solve** (like `lambda`), not per-customer. Per-customer scale is a
   possible future extension, out of scope.

## Decisions to confirm

- **A. Fence in days or quantized?** (Recommend days — it's physical, not a
  reasoning lens.)
- **B. Rounding mode:** `round` / `floor` / `ceil`? (Recommend `round`.)
- **C. `months = 30` nominal** OK, or do you want real calendar months (accepting
  the edge effects)? (Recommend nominal 30.)
- **D. Default scale = `days`** (no behaviour change unless asked)? (Recommend yes.)

## Verify

`uv run pytest` · `PYTHONPATH=. uv run python tests/test_invariant.py` (default
scale = days ⇒ unchanged) · `uv run ruff format . && uv run ruff check .` · eyeball
`uv run python -m xas_allocation.session` with `time_scale` set to `weeks`/`months`
and watch sub-unit distinctions collapse and durations render in the unit. Redeploy:
package + SKILL + prompt changed → `setup_allocation_agent.py` (data unchanged).

# Time-scale (granularity) knob — Implementation Plan

> A planner-settable knob — `days` | `weeks` | `months` — that sets the
> **resolution the solver reasons at**. At week scale it measures every gap in
> weeks and stops distinguishing differences finer than a week; at month scale, in
> months. It looks at *all* times through that lens, and the report speaks the same
> unit. This **changes the plan** (coarser). NO code until approved.
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
**units**, **rounding up**, before costing:

```
units(days) = ceil(days / unit_days)          # any part of a unit counts as a full unit
```

Round-up ("bill by the week") means: at week scale, 1–7 days late all read as
**1 week late**; 8–14 as **2 weeks**; and **0 days is the only thing that reads
"on time."** Differences *within* a unit collapse (3 and 5 days late are equal),
but crossing into a new unit always counts as a whole unit.

- **Lateness term** uses `units(tardiness_days)` instead of raw days — so at week
  scale, 3 days late and 5 days late cost the same (both 1 week), while 0 days is
  on time and 8 days is 2 weeks.
- **Churn term** (`λ · [date ≠ promised]`) fires only when arrival and promise fall
  in **different unit-buckets** — a within-unit date shuffle isn't counted as churn.
- **Report** speaks the unit: "1 week late", "on time" = 0 days late. (Actual
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

- A small helper `scale_units(days, unit_days) -> int` using **`math.ceil`**
  (round up; decided).
- `arc_cost_float(...)` gains `unit_days: int`; the lateness term uses
  `scale_units(tardiness, unit_days)`; the churn `[date ≠ promised]` test becomes a
  **same-bucket** test at the given scale.
- `solve()` reads `override.get("time_scale")` → maps to `unit_days` via
  `decisions.SCALE_DAYS`, threads it into every `arc_cost_float` call (like `lam`).
- **Hard structural rules stay in days** (decided) — the time fence (DECIDE-2) and
  committed (DECIDE-3) are physical (how close to delivery / pipeline stage), not a
  planner reasoning lens; quantizing them would change *feasibility*. Keep them in
  days.

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

- `SCALE_DAYS = {"days": 1, "weeks": 7, "months": 30}` (month = 30 days nominal,
  decided), `DEFAULT_TIME_SCALE = "days"` (default = today's behaviour exactly, so
  nothing shifts unless asked), rounding = **ceil**.
- New registry entry **DECIDE-14** ("time-scale granularity; round up; fence stays
  in days; month = 30d nominal"), surfaced by `format_decisions()`.

## Fix 4 — report + discrepancy map speak the unit (`session.py`)

- `_result_phrase`, `planner_report`, and `discrepancy_report` read the scale from
  the override and render durations in units ("2 weeks late", "on time"). They
  already receive the override / snapshot; thread the scale in.
- "On time" means **0 days late** (round-up: any lateness is at least 1 unit) —
  see gotcha 2, which is now a benefit.

## Fix 5 — skill + prompt

- `SKILL.md`: document `time_scale` next to `lambda` / `scope`; state it changes the
  plan (coarser), rounds day-deltas **up** to whole units (not calendar buckets),
  and that hard fences stay in days.
- SYSTEM_PROMPT: the agent sets `time_scale` from the planner's horizon; it does not
  round dates by hand.

---

## Files touched

| File | Change |
| --- | --- |
| `xas_allocation/solver.py` | `scale_units` helper (ceil); `arc_cost_float` quantizes tardiness + same-bucket churn test; `solve()` reads `time_scale` → `unit_days` and threads it |
| `xas_allocation/overrides_schema.json` | add `time_scale` |
| `xas_allocation/decisions.py` | `SCALE_DAYS`, `DEFAULT_TIME_SCALE`, ceil rounding; new **DECIDE-14** |
| `xas_allocation/session.py` | report + discrepancy map render durations in the unit; "on time" = 0 days late |
| `skills/xas-allocation/SKILL.md` | document the knob; round-up delta semantics; fence stays in days |
| `setup_allocation_agent.py` | SYSTEM_PROMPT: set the scale, don't round by hand |
| `tests/test_time_scale.py` | **new** — see below |
| `tests/test_report.py` | durations render in the active unit |
| `CLAUDE.md`, `README.md` | one line + DECIDE-14 in the summary table |

## Tests (`tests/test_time_scale.py`)

1. **Within-unit differences vanish.** At `weeks`, two cars 3 and 5 days late are
   equal-cost (both 1 week); at `days` they differ.
2. **Round-up boundary.** At `weeks`, 1 day late = 1 week late (not "on time"), and
   only 0 days late reads on time.
3. **Default = today.** `DEFAULT_TIME_SCALE = "days"` reproduces current plans
   byte-for-byte (safety net for existing fixtures + the invariant test).
4. **Coarser ⇒ fewer distinctions.** A scenario where `months` yields a different
   (coarser) plan than `days`.
5. **Churn respects the bucket.** A within-unit date change is not counted as churn.
6. **Determinism.** Same snapshot + same override (with `time_scale`) → identical
   plan.
7. **Report unit.** Durations render in the active unit.

## Gotchas

1. **Round-up = strict.** Every whole (or partial) unit past the promise counts.
   1 day late at month scale is a full month late. That's the chosen semantics
   (conservative — never under-states lateness).
2. **"On time" does NOT hide lateness (benefit of round-up).** Because any lateness
   rounds up to ≥ 1 unit, a late order never reads "on time" at a coarse scale — the
   worry with round-to-nearest is gone. Keep the exact date in the row anyway; never
   let the unit label suppress a locked-in/stuck flag.
3. **Fence stays in days (decided).** Physical constraints (DECIDE-2/3) are not
   quantized; only the objective is.
4. **`months = 30` is nominal (decided).** We round day-deltas, not calendar months,
   so no ragged boundaries; document that "months" ≈ 30-day units.
5. **Determinism preserved** — quantization is pure integer math + the scale scalar.
   The invariant test's default-scale run is unchanged.
6. **One scale per solve** (like `lambda`), not per-customer. Per-customer scale is a
   future extension, out of scope.

## Decisions (confirmed)

- **A. Fence:** stays in **days** — physical, not a reasoning lens. ✅
- **B. Rounding:** **round up (ceil)** — any part of a unit counts as a full unit. ✅
- **C. Month:** **30 days nominal** (round day-deltas, not calendar months). ✅
- **D. Default scale:** **days** — no behaviour change unless the planner asks. ✅

## Verify

`uv run pytest` · `PYTHONPATH=. uv run python tests/test_invariant.py` (default
scale = days ⇒ unchanged) · `uv run ruff format . && uv run ruff check .` · eyeball
`uv run python -m xas_allocation.session` with `time_scale` set to `weeks`/`months`
and watch within-unit distinctions collapse and durations render in the unit.
Redeploy: package + SKILL + prompt changed → `setup_allocation_agent.py` (data
unchanged).

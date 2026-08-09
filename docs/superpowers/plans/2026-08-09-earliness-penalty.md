# Earliness penalty — Implementation Plan

> Add a new term to the solver's cost so arriving early actually costs something.
> Today only lateness is priced; a car 28 days early scores the same as landing on
> the promise, so the solver grabs wildly-early cars and the report frames them as
> wins. NO code until approved.
>
> **Independent of the time-scale plan** (`2026-08-09-time-scale.md`). This one adds
> a direction (early = mildly bad); that one sets the resolution time is measured
> at. They compose but ship separately; earliness works on its own at day scale.

**Driver:** planner feedback — *"agent shouldn't prioritize early delivery; too
early is also not good, a little bit early is ok."*

---

## The change, in one line

`arc_cost_float` gains an early-side term:

```
early_days = max(0, promised_date - arrival_date)
cost += EARLY_WEIGHT · W(o) · early_days          # linear, small weight
```

**Why no tolerance band:** the term is *linear with a small weight*, so it is
self-scaling — 1 day early costs almost nothing ("a little early is fine"),
2 months early costs real money ("too early is bad"). The "a little is OK"
behaviour falls out of the gradient; it does **not** need a separate day-count
knob. (Coarsening below a unit is the time-scale plan's job, not this one's.)

- **Linear, not convex.** Lateness keeps its `^1.5` convex shape; earliness is
  linear and small, so **lateness always dominates** — the solver still prefers an
  earlier-but-not-late car over a late one, and never trades an on-time slot for a
  late one to avoid earliness.
- **Scaled by `W(o)`** so it sits on the same importance scale as the lateness
  term (a high-priority order's earliness matters proportionally).
- **Lateness untouched.** This prices earliness only; a late order is still the
  thing being repaired and is never made cheaper.

## The principle it respects

`plan = f(snapshot, skill, override)` unchanged: this is a fixed formula term with
a fixed coefficient — a **model change**, code + tests ("a human moves the model").
It is *not* an agent knob. Determinism holds (no new inputs).

---

## Fix 1 — the term (`solver.py`)

- `arc_cost_float(...)` adds the early-side term after the lateness term. `early`
  is `max(0, (promised - arrival).days)`.
- No new parameter needed from the override; `EARLY_WEIGHT` is a constant.
- Existing `not_before` soft pin is unchanged — that's a planner-pinned *hard*
  "don't arrive before date X" on one order; this is a gentle *global* preference.
  They coexist.

## Fix 2 — coefficient + decision (`decisions.py`)

- `EARLY_WEIGHT` — the earliness coefficient. Default **small** (≈`0.15`), term is
  **linear**. Small enough that lateness always wins, firm enough to break ties
  toward the closer car.
- New registry entry **DECIDE-15** ("price earliness; weight + linear shape"),
  surfaced by `format_decisions()` — whether/how hard to discourage earliness is a
  genuine business choice.

## Fix 3 — stop selling earliness as a win (`session.py`)

- `_result_phrase` and the stray `EARLY_FLAG_DAYS = 14`: an early arrival is no
  longer a ✅ win.
  - late → "N days late" (unchanged);
  - on time (arrival ≥ promise) → plain **"on time"**;
  - meaningfully early → a neutral caveat, **not** a ✅ — e.g. "on time, but lands
    N days early (ties a car up sooner than needed)".
- Replace the magic `14` with a small threshold constant (or, once the time-scale
  plan lands, "≥ 1 unit early"); the point is the framing, not the exact number.

## Fix 4 — skill + prompt

- `SKILL.md` cost-model section: document the earliness term next to the lateness
  term; state lateness is unaffected and that earliness is a soft, proportional
  preference the solver handles — the agent does not hand-pick early cars or praise
  earliness in prose.
- SYSTEM_PROMPT: same, one line.

---

## Files touched

| File | Change |
| --- | --- |
| `xas_allocation/solver.py` | `arc_cost_float` gains the linear early-side term |
| `xas_allocation/decisions.py` | `EARLY_WEIGHT`; new **DECIDE-15** |
| `xas_allocation/session.py` | earliness framed as a neutral caveat, not a ✅; retire `EARLY_FLAG_DAYS = 14` magic number |
| `skills/xas-allocation/SKILL.md` | document the term; lateness unaffected |
| `setup_allocation_agent.py` | SYSTEM_PROMPT: earliness is a solver preference, don't hand-pick |
| `tests/test_earliness.py` | **new** — see below |
| `tests/test_report.py` | over-early row reads as a caveat, not a win |
| `CLAUDE.md`, `README.md` | one line + DECIDE-15 in the summary table |

## Tests (`tests/test_earliness.py`)

1. **Closer car wins.** Two feasible cars, one 2 days early, one 40 days early →
   solver picks the closer one. (Before: indifferent.)
2. **Lateness dominates.** Given an on-time option, the solver never picks a *late*
   car to avoid earliness — early is always cheaper than late for the same order.
3. **Proportionality.** Cost of 2-days-early ≪ cost of 60-days-early (the "a little
   is fine, a lot is bad" gradient), and 1-day-early is negligible.
4. **Determinism.** Folds into the invariant proof — same inputs, same plan.
5. **Report framing.** Meaningfully-early row reads as a caveat, not ✅.

## Gotchas

1. **Never let earliness cause a worse plan.** Small + linear vs convex lateness
   guarantees early < late for any given order; test #2 pins it. Do not make the
   term convex or the weight large.
2. **Extreme-earliness crossover is real.** A 100-day-early car *can* cost more
   than a 1-day-late one (linear term × big gap). Likely desirable (tying a car up
   for months is bad), but a judgment call — see Decisions; document with a test.
3. **Default weight shifts current output.** The demo's "28 days early ✅" becomes a
   caveat and some fixtures move — that shift is the point; update them deliberately.
4. **Interoperates with the time-scale plan.** If that lands, `early` is measured in
   whatever unit the scale sets, **rounded up** (decided there) — so at week scale
   even 1 day early counts as 1 week early. "A little early is fine" therefore comes
   from *this* plan's small linear weight (1 unit early is cheap), NOT from the scale
   rounding it down. At day scale (default) `early` is raw days. Same term either way.

## Decisions (confirmed)

- **A. Weight/shape:** `EARLY_WEIGHT = 0.15`, **linear**. ✅
- **B. Extreme-early crossover:** **uncapped** — a very-early car may lose to a
  slightly-late one (tying a car up for months is real waste). Documented with a
  test so the crossover isn't a surprise. ✅
- **C. Late side:** **earliness only** — lateness stays strict, every day counts;
  no late-side grace in this change. ✅

## Verify

`uv run pytest` · `PYTHONPATH=. uv run python tests/test_invariant.py` ·
`uv run ruff format . && uv run ruff check .` · eyeball
`uv run python -m xas_allocation.session` — the formerly "28 days early ✅" row now
reads as a neutral early caveat. Redeploy: package + SKILL + prompt changed →
`setup_allocation_agent.py` (data unchanged).

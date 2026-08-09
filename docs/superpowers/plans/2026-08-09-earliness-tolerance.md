# Earliness penalty + planner tolerance knob — Implementation Plan

> Today the solver only penalizes **lateness**; arriving 28 days early costs the
> same as landing exactly on the promise, so the agent grabs wildly-early cars and
> reports them as wins. Add a gentle **earliness penalty** with an acceptable
> **tolerance** band, and make that tolerance a **planner-settable knob** so
> "a few weeks early is fine" vs "hit the date" is a live dial, not a code edit.
> NO code until approved.

**Driver:** planner feedback — *"agent shouldn't prioritize early delivery; too
early is also not good, a little bit early is ok."* A car that lands months early
ties up inventory and isn't the win the report framed it as.

---

## What changes, in one line

`cost(o→u)` gains a small early-side term, bounded by a **tolerance** the planner
controls:

```
early_days = max(0, promised_date - arrival_date)
if early_days > tolerance:
    cost += EARLY_WEIGHT · W(o) · (early_days − tolerance)
```

Within `tolerance` → free (a little early is fine). Beyond it → a *gentle, linear*
cost (much softer than the convex lateness term), scaled by the same `W(o)` so it
sits on the same importance scale as everything else.

**Lateness is untouched.** This plan tolerates *earliness only*. A late order is
the thing we're repairing — we never make lateness "free," and we never hide it.
(A symmetric late-side grace is a separate, riskier idea — see Decisions.)

## The principle it respects

Same split the whole system follows: **the prompt moves weights and pins; a human
moves the model.**
- The *shape* — that there is an earliness term with a tolerance band — is **code**
  (`solver.py` + `decisions.py`), a reviewed change with tests.
- The *width* — how many days early is acceptable — is a **knob** in the typed
  override (`tolerance_days`), which the agent compiles from plain language
  ("work this at month scale" → `30`) with no code change.

Determinism holds: `tolerance_days` is a scalar in the override, so it's part of
the deterministic input. Same snapshot + same override (tolerance included) →
byte-identical plan.

---

## Fix 1 — the earliness term (`solver.py`)

- `arc_cost_float(...)` gains a `tolerance: int` parameter and the early-side term
  above, added after the existing lateness term.
- `solve()` reads `override.get("tolerance_days", D.DEFAULT_TOLERANCE_DAYS)` once
  and threads it into every `arc_cost_float` call (same way `boosts` / `lam` /
  `not_before` are already threaded). No graph-structure change — only arc costs.
- Keeps the existing `not_before` soft pin as-is: that's a planner-pinned *hard*
  "don't arrive before date X" on one order; the new term is a *gentle global
  preference* against needless earliness. They coexist and don't conflict.

## Fix 2 — the coefficients + a new decision (`decisions.py`)

- `EARLY_WEIGHT` — the earliness coefficient. Default **small** (≈`0.15`) and the
  term is **linear** (not the `1.5` convex exponent lateness uses), so lateness
  always dominates earliness for comparable magnitudes.
- `DEFAULT_TOLERANCE_DAYS` — the default acceptable-early band (recommend `7`: a
  week early is fine out of the box).
- New registry entry **DECIDE-14** ("penalize earliness beyond a tolerance;
  default band + weight") surfaced by `format_decisions()`, since whether/how hard
  to penalize earliness is a genuine open business choice.

## Fix 3 — the knob (`overrides_schema.json`)

Add one property to the override object:

```json
"tolerance_days": {
  "type": "integer",
  "minimum": 0,
  "description": "How many days EARLY is acceptable before earliness is penalized.
    Larger = looser ('a few weeks early is fine', month-scale planning); 0 = 'hit
    the date exactly'. Does not affect lateness. Absent → DEFAULT_TOLERANCE_DAYS."
}
```

The agent compiles a planner instruction into it ("don't tie cars up early" → a
small number; "just get them roughly in the right month" → ~30). Shown back to the
planner as plain words like every other lever.

## Fix 4 — stop selling earliness as a win (`session.py`)

- `_result_phrase` (and the `EARLY_FLAG_DAYS` constant, currently a hardcoded `14`):
  drive the flag off the **tolerance**, not a separate magic number.
  - late → "N days late" (unchanged);
  - within tolerance early → plain **"on time"**;
  - beyond tolerance early → a mild caveat, **not** a ✅ — e.g. "on time, but lands
    N days early (ties a car up sooner than needed)".
- `planner_report` already receives the override, so it can read `tolerance_days`
  to phrase the flag; thread it into `_result_phrase`.
- The "Worth knowing" caveat may surface a badly-early placement the same way it
  surfaces a stuck order.

## Fix 5 — skill + prompt wording

- `SKILL.md` cost-model section: document the earliness term and the
  `tolerance_days` knob next to `boosts` / `lambda`; note lateness is unaffected.
- SYSTEM_PROMPT: earliness is a soft preference the solver already handles — the
  agent sets `tolerance_days` from the planner's words, it does **not** hand-pick
  early cars or reward earliness in prose.

---

## Files touched

| File | Change |
| --- | --- |
| `xas_allocation/solver.py` | `arc_cost_float` gains `tolerance` + the early-side term; `solve()` reads `tolerance_days` from the override and threads it |
| `xas_allocation/decisions.py` | `EARLY_WEIGHT`, `DEFAULT_TOLERANCE_DAYS`; new **DECIDE-14** entry |
| `xas_allocation/overrides_schema.json` | add `tolerance_days` |
| `xas_allocation/session.py` | `_result_phrase` / earliness flag driven by tolerance; neutral (non-✅) framing for over-early rows |
| `skills/xas-allocation/SKILL.md` | document the term + knob; lateness unaffected |
| `setup_allocation_agent.py` | SYSTEM_PROMPT: earliness is a solver preference; set the knob, don't hand-pick |
| `tests/test_tolerance.py` | **new** — see below |
| `tests/test_report.py` | over-early row reads as a caveat, not a win; within-tolerance reads "on time" |
| `CLAUDE.md`, `README.md` | one line on the earliness term + tolerance knob; DECIDE-14 in the summary table |

## Tests (`tests/test_tolerance.py`)

1. **Closer car wins.** Two feasible cars for one order — one 2 days early (within
   tol), one 40 days early (beyond tol). Solver picks the closer one. (Before this
   change it was indifferent.)
2. **Lateness still dominates earliness.** Given an on-time/within-tolerance option,
   the solver never chooses a *late* car instead just to avoid earliness — early is
   always cheaper than late for the same order.
3. **The knob works both ways.** `tolerance_days` huge → earliness never penalized
   (matches today's behaviour, a safety net for old expectations);
   `tolerance_days = 0` → all earliness penalized.
4. **Determinism.** Same snapshot + same override (with `tolerance_days`) →
   identical plan; folds into the existing invariant proof.
5. **Report framing.** Within tolerance → "on time"; beyond → the caveat wording,
   no ✅.

## Gotchas (from the code)

1. **Earliness must never outrank lateness into a worse plan.** The term is small +
   linear while lateness is `W·days^1.5`, so early is always cheaper than late for a
   given order — the solver still prefers an earlier-but-not-late car over a late
   one. Test #2 pins this. Keep `EARLY_WEIGHT` small; do not make the term convex.
2. **The extreme-earliness crossover is real and intended-ish.** A car 100 days
   early *can* cost more than one 1 day late (linear term × big gap). That may be
   correct (tying a car up for months is bad) — but it's a judgment call; see
   Decisions. Whatever we pick, document the crossover with a test so it's not a
   surprise.
3. **Default tolerance changes existing output.** With any finite default, the
   demo's "28 days early ✅" becomes a caveat and some current tests/fixtures shift.
   Update them deliberately; that shift is the point.
4. **Unify the earliness flag.** `session.py` already has a stray `EARLY_FLAG_DAYS
   = 14`; replace it with the tolerance rather than leaving two different "how early
   is notable" numbers.
5. **Lateness stays fully penalized** — do not let `tolerance_days` leak onto the
   late side. If someone later wants late-side grace, that's a separate, guarded
   change (it can mask real lateness — the exact trap the discrepancy map exists to
   avoid).
6. **Scope of the knob.** `tolerance_days` is one scalar for the whole solve
   (like `lambda`), not per-customer. Per-customer tolerance is a possible future
   extension, out of scope here.

## Decisions to confirm

- **A. Default tolerance.** `DEFAULT_TOLERANCE_DAYS = 7`? (Recommend yes — a week
  early is unremarkable; tighten per session when it matters.)
- **B. Earliness weight.** `EARLY_WEIGHT = 0.15`, linear? (Recommend yes — soft
  enough that lateness always wins, firm enough to break ties toward the closer
  car.)
- **C. Extreme-earliness crossover.** Allow a very-early car to lose to a slightly
  late one (uncapped linear term), or cap the earliness penalty so lateness is
  *always* worse? (Recommend uncapped — tying a car up for months genuinely can be
  worse than a day late; document with a test. Cap only if the planner wants
  "late is always the worst outcome, full stop.")
- **D. Late-side grace.** Out of scope here (earliness only). Confirm you don't want
  small lateness treated as free in this change.

## Verify

`uv run pytest` · `PYTHONPATH=. uv run python tests/test_invariant.py` ·
`uv run ruff format . && uv run ruff check .` · eyeball
`uv run python -m xas_allocation.session` — a formerly "28 days early ✅" row now
reads as a neutral early caveat, and the demo can set `tolerance_days` to show the
band widening/narrowing. Redeploy: package + SKILL + prompt changed →
`setup_allocation_agent.py` (data unchanged).

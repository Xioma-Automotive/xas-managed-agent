# Date-based data model + scenario engine — Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.
> Implement task-by-task, running the stated command after each step. Companion
> to `docs/superpowers/specs/…-design.md` and `docs/xasdatamodel.md` (the entity
> model). Supersedes the week-based synthetic model in `xas_allocation/`.

**Goal:** Move the allocation problem from an abstract week-bucketed synthetic
generator to the real XAS entity shape (`PDN → Vehicle`, `Customer → SO`,
allocation links), keyed on **specific dates** instead of ISO weeks. A
**standalone scenario engine** (outside the agent) fabricates the world — first
feasible, then disrupted; the agent **pulls the rich data, flattens it to the
solver snapshot with pure code**, maps the damage for the planner, and drives
the solver as before.

**Why:** `DECIDE-7` (the data contract) was a stand-in. `docs/xasdatamodel.md`
now pins the entities and the disruption causal chain
(`PO/PDN problem → vehicle planned_delivery_date moves → allocation breaks`).
This plan makes the code match it.

---

## Decisions locked in (from the design discussion)

| # | Decision |
| --- | --- |
| Entities | **`PDN`, `Vehicle`, `SO`** only — no PO, no Customer table (customer is a field on the SO). Minimal: **30 customers**. |
| Delay lever | The **PDN**. Delaying a PDN pushes `planned_delivery_date` on every vehicle it exploded into. |
| Time unit | **Real dates** (`YYYY-MM-DD`), calendar days. Tardiness measured in **days**. No wall-clock — a fixed base date is a generator constant. |
| Eligibility | **Hard `sales_model` equality**, computed at solve time, never stored. **No date term in the arc** — lateness is *priced* (§2), not forbidden, so the solver can still place a slightly-late vehicle. |
| Spec match | **Deleted.** `sales_model` equality replaces the 4-field spec + the LLM residual + `ResidualCache`. The §11 residual-determinism concern goes with it. |
| `planned_delivery_date` | The one **mutable** field on the Vehicle; drives `tardiness`. |
| `location_state` | Pipeline `future→sea→port→transfer→bonded→pdi→…`; `committed` is **derived** from it (DECIDE-3 sets the threshold). Replaces today's `state ∈ {shipped,in_prep}`. |
| `promised_date` vs `eta_date` | `promised_date` = fixed customer commitment; **tardiness is measured against it**. `eta_date` = the originally-expected delivery, frozen on the SO line (informational + the discrepancy anchor). In *good* data `vehicle.planned_delivery_date == eta == promised`. The disruption moves only the vehicle's date. |
| `price` | **Display-only** for now (not a cost-model input). |
| `reserved_for_customer` | **New DECIDE** (a reserved vehicle is eligible only for its customer). Deferred — not in the minimal build. |
| Pull payload | The pull now **ships the dataset** (rich relational rows), not a seed + materialize command — the engine's code won't live in the sandbox to regenerate from. Small at this scale. |
| Flatten | Rich → snapshot is a **pure, bundled function** the agent invokes (not model reasoning) — required by the `plan = pure_function(...)` invariant. |

---

## Architecture — two pieces + one pure hop

```
┌─ scenario_engine/  (standalone, OUTSIDE the agent, seeded) ───────────┐
│  build good world (PDN→Vehicle pool, SOs, allocation, spare pool)     │
│  → introduce delay (one PDN, +N days) → emit rich dataset + baseline  │
└───────────────────────────────────────────────────────────────────────┘
                              │  dataset files (pdns/vehicles/sos/allocation + manifest)
                              ▼
┌─ the agent (per turn) ────────────────────────────────────────────────┐
│  1. pull  → tool ships the rich dataset into the sandbox               │
│  2. flatten() [pure, bundled]  → snapshot.json {orders[],units[],      │
│               incumbent[]}   ← THIS is the "flow chart" step           │
│  3. detect discrepancies (allocated vehicle PDD > promised) → map it   │
│  4. ask planner for priorities / steering                              │
│  5. draft flow chart (mermaid of steps 1–2) → invoke solver → λ sweep  │
│  6. planner-facing change list (already-added output section)          │
└───────────────────────────────────────────────────────────────────────┘
```

The engine stays dumb (emits rich data only); all shaping is the one shared,
tested `flatten`. Eligibility arcs are built by the solver, never stored.

---

## File structure

| Path | Change |
| --- | --- |
| `scenario_engine/` | **new** — standalone seeded generator: entity models (`PDN/Vehicle/SO`), `generate.py` (good world → disruption → emit). Not in the skill bundle. |
| `data/` (or engine `--out`) | **new** — committed sample scenario the pull serves (`snapshot`/rich rows + `baseline` + `disruption` manifest). |
| `xas_allocation/flatten.py` | **new** — pure `flatten(rich) → Snapshot`; derives `committed` from `location_state`; computes broken-allocation set. |
| `xas_allocation/snapshot.py` | **rewrite of `synth_data.py`** — `Order`/`Unit`/`Snapshot` dataclasses in the new date/`sales_model` vocabulary + a JSON loader. Generator removed (moved to the engine). |
| `xas_allocation/solver.py` | **rewrite** — dates; `tardiness` in days off `planned_delivery_date`; arcs by `sales_model` equality; drop `resolve_compatibility`. |
| `xas_allocation/spec_match.py` | **delete** — relocate `PRIORITY_WEIGHT` to `decisions.py`. |
| `xas_allocation/decisions.py` | **edit** — DECIDE-7 concretized; DECIDE-3 → `location_state` commit threshold; new DECIDE `reserved_for`; date constants; drop residual/aging-in-weeks bits; keep λ sweep. |
| `xas_allocation/ledger.py` | **edit** — TTL as a date, not a week label. |
| `xas_allocation/overrides_schema.json` | **edit** — `not_before` / `ttl` become date patterns; `defer` targets a date. |
| `xas_allocation/session.py` | **edit** — call `flatten`; add discrepancy map + flow chart; drop `ResidualCache`; keep λ sweep + change list. |
| `skills/xas-allocation/SKILL.md` | **edit** — §8 flow rewrite (pull→flatten→map→ask→chart→solve), new vocabulary + eligibility rule, drop spec residual. (Planner-facing output section already landed.) |
| `alloc_tools.py`, `web.py` | **edit** — pull reads the engine's dataset and ships it; no seed/materialize. Keep the single-definition tool contract. |
| `tests/test_flatten.py` | **new** — flatten is deterministic + round-trips; `committed` derivation; broken-set detection. |
| `tests/test_invariant.py` | **edit** — determinism = `flatten(pull)` + ledger replay reproduces the plan across a sandbox discard; no residual cache. |
| `tests/test_tool_contract.py` | **edit** — new pull contract (ships dataset). |
| `CLAUDE.md`, `README.md` | **edit** — engine, flatten, new invariants, re-run-setup note. |
| `setup_allocation_agent.py` | **re-run required** (bundle + tool schema changed); edit only if the schema surface changes. |

---

## Task P0 — the scenario engine (build first; everything reads its output)

**Files:** create `scenario_engine/` + a committed sample under `data/`.

- [ ] **Entity models** — `PDN(pdn_id, sales_model, quantity, delayed_days)`,
  `Vehicle(vehicle_id, pdn_id, sales_model, planned_delivery_date, location_state)`,
  `SO(order_id, customer, sales_model, priority, promised_date, eta_date, price, n_prior_delays, days_backordered, current_vehicle_id)`.
- [ ] **Good world** (seeded, fixed base date): 30 customers, ~5 sales_models,
  ~40 SO lines; build a feasible on-time allocation by construction (one
  compatible vehicle per SO line with `planned_delivery_date == promised == eta`);
  group vehicles under PDNs (~8/PDN); add ~15 **unallocated spare** vehicles
  (the wiggle room). Assign `location_state` by nearness of the delivery date.
- [ ] **Disruption**: pick one PDN deterministically (carries allocated,
  non-committed vehicles), push its vehicles' `planned_delivery_date` by ~21
  days. Record a manifest (`pdn`, `delay_days`, affected vehicles, broken orders).
- [ ] **Emit**: rich dataset for the disrupted state (the pull target) + the good
  baseline for reference, as JSON. Commit one sample scenario under `data/`.
- [ ] **Run:** `uv run python -m scenario_engine.generate --seed 20 --out data/scenario_20`
  → inspect the output shape. `uv run ruff format . && uv run ruff check .`

## Task P1 — snapshot model, `flatten`, solver

**Files:** `snapshot.py` (rewrite `synth_data.py`), `flatten.py`, `solver.py`,
delete `spec_match.py`, `decisions.py`, `tests/test_flatten.py`.

- [ ] Rewrite the dataclasses in date/`sales_model` vocabulary + JSON loader.
- [ ] Write `flatten(rich) → Snapshot` (pure): map SO→order, Vehicle→unit
  (derive `committed`), allocation→incumbent; compute the broken-allocation set.
- [ ] `tests/test_flatten.py`: same rich input → byte-identical snapshot;
  `committed` derivation; broken set matches the manifest. Run: `uv run pytest tests/test_flatten.py`.
- [ ] Rewrite `solver.py`: `tardiness` in days off `planned_delivery_date`; arcs
  by `sales_model` equality (drop `resolve_compatibility`); keep the λ sweep,
  backorder dummy, self-check. Move `PRIORITY_WEIGHT` into `decisions.py`.
- [ ] Update `decisions.py` (DECIDE-3/7 + new `reserved_for`), `ledger.py` +
  `overrides_schema.json` (dates). Run: `uv run pytest`.

## Task P2 — the agent flow (SKILL.md + session.py)

- [ ] `session.py`: invoke `flatten`; add **discrepancy detection + map**
  (orders whose allocated vehicle now delivers after `promised`) before solving;
  emit the **flow chart** (mermaid of pull→flatten→snapshot→arcs→solver); drop
  `ResidualCache`.
- [ ] Rewrite SKILL.md §8 to the new flow + vocabulary + eligibility rule; remove
  spec-residual language. Keep the Planner-facing output section.

## Task P3 — the tool contract (pull ships data)

- [ ] `alloc_tools.py` + `web.py`: the pull reads the engine's committed dataset
  and ships it into the sandbox (no seed/materialize); keep both sides of the
  contract in one module. Update `tests/test_tool_contract.py`. Run: `uv run pytest tests/test_tool_contract.py`.

## Task P4 — determinism proof + docs

- [ ] `tests/test_invariant.py`: prove `flatten(pull) + ledger replay` reproduces
  the plan across a sandbox discard (no residual cache). Run:
  `PYTHONPATH=. uv run python tests/test_invariant.py`.
- [ ] Update `CLAUDE.md` + `README.md` (engine, flatten, new invariants, re-run
  setup). Full gate: `uv run pytest && uv run ruff format . && uv run ruff check .`
- [ ] Re-run `setup_allocation_agent.py` so the sandbox gets the new bundle.

---

## Open decisions to surface at build time

- **DECIDE-3** — where `location_state` becomes "committed" (`pdi`? `bonded`?).
- **DECIDE-7** — now concretized by this model; mark the field shapes as the
  proposed contract, not confirmed.
- **`reserved_for_customer`** — new DECIDE; reserved vehicle eligible only for its
  customer. Deferred from the minimal build.

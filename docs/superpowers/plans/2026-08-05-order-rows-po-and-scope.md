# Order rows, PO-line supply, and scope — Implementation Plan

> Builds on `2026-08-05-date-based-data-model.md`. Adds a finer demand grain, a
> second kind of supply, and a **scope** filter so the agent can work a slice
> ("allocate all Colmobil orders for August") and suggest **localized** fixes
> that don't disturb everything else. Checkbox steps; run the stated check after
> each.

**Goal:** Make the agent as flexible as possible without adding brittle logic —
by making the *typed steering object* the flexibility surface and keeping the
system prompt thin and principled.

---

## Data model v2

**Demand gains a grain.** A **Sales Order** (one customer) groups multiple
**vehicle order rows**; the **row** becomes the allocatable unit the solver
matches (not the whole SO). Each row carries its own `promised_date` / `eta_date`
(each car can have its own timing).

**Supply gains a kind.** A row is allocated to **either**:
- a **concrete Vehicle** (a physical car, VIN — the "vehicle shield"), or
- a **PO line**, keyed `PO-model-row` → `PO-150-1-5` (PO 150, model line 1, row 5)
  — a *future* car not yet built.

So the solver's supply pool is **vehicles ∪ PO-line slots**, each a unit with a
`sales_model`, an expected delivery date, and committed-ness. Eligibility stays a
hard `sales_model` equality; a row can be re-linked between a PO slot and a
vehicle (or another slot) to hit its date. `PO → PDN → Vehicle` is now complete;
a delayed PO/PDN slips the date on its slot and on the vehicles it explodes into.

**Assumptions (confirm or veto — chosen for maximum capability):**
1. PO line is **real allocatable supply**, not just a traceability label.
2. "vehicle shield" = the **VIN / chassis id** of a concrete vehicle.
3. `promised_date` / `eta_date` live on the **row**, not the SO.
4. The pull is **multi-customer** (the whole book for the horizon); the agent
   scopes down inside it.

## Scope — the flexibility lever

A **general filter** carried in the override object (so it's replayable via the
ledger), NOT a fixed customer+month pair:

```
scope: { customers?: [...], models?: [...], po?: [...], from_date?, to_date? }
```

Semantics: **when a scope is present it DEFINES the free set** — only rows
matching it are re-allocatable; everything else is pinned. This delivers both
asks with one mechanism:
- "allocate all Colmobil orders for August" → `scope {customers:[CUST-001], from:2026-08-01, to:2026-08-31}`, solve the slice.
- "fix this delay without disrupting everything" → a narrow scope (or the
  disrupted set) keeps churn local; repair-not-rebuild already pins the rest.

When no scope is given, today's behavior stands (free set = disrupted rows).

## Prompt vs Skill — placement & flexibility (the contract)

Flexibility comes from an expressive typed object + a thin, principled prompt —
**not** from enumerating request types in prose. Adding a new kind of request
extends the **skill schema**, never the prompt.

**System prompt** (always on, stable, needs a setup re-run to change) holds only:
- identity + one-line job;
- the invariant `plan = pure_function(data_snapshot, skill, ledger)`;
- the HARD RULES (the solver decides, not you; a runtime request = a typed
  override incl. **scope**, a new *constraint* = a reviewed PR; never move a
  frozen/committed vehicle; write back only on approval; infeasible → stop);
- the flexibility pattern: **"translate any planner request into the typed
  object; the solver decides"**;
- a pointer to the skill.

It must **not** hold field names, cost coefficients, scope mechanics, the output
layout, or procedure steps — those churn and belong in the skill.

**Skill** (loaded when relevant, versioned, cheap to change) holds: the data
model (rows / PO lines / vehicles / dates), the **override + scope schema** (the
flexibility surface, fully specified), cost model §2, procedure §8, the
planner-facing output format, infeasibility, ledger, run commands.

**Test:** must the rule hold even when the skill isn't in context? → prompt.
Is it *how* to do the job? → skill.

### Edits this implies (applied WITH the code, never ahead of it)

- **Prompt** (`setup_allocation_agent.py`): replace the hard-coded per-turn
  output list with a pointer to the skill's "Planner-facing output"; generalize
  the steering line so **scope** is named as a runtime-override class. Net: thinner.
- **SKILL.md**: add a **Scope** section + the row/PO data model; document the
  extended override schema.
- **`overrides_schema.json`**: add the `scope` object.

---

## Tasks

- [ ] **P0 — engine**: SOs with multiple rows; a PO table; some rows allocated to
  PO-line slots, some to concrete vehicles; the disruption slips a PO/PDN.
- [ ] **P1 — snapshot/flatten**: row grain in `orders[]`; `units[]` becomes
  vehicles ∪ PO-line slots (a `kind` field); flatten maps both. Tests.
- [ ] **P2 — solver**: supply union (unchanged sales_model eligibility, dates);
  `partition` honors a **scope** to define the free set. Tests: scope bounds the
  change list; localized fix leaves out-of-scope rows untouched.
- [ ] **P3 — steering/schema**: add `scope` to `overrides_schema.json` + ledger
  replay; `session` resolves NL scope ("Colmobil, August") → the object.
- [ ] **P4 — prompt/skill/docs**: apply the placement edits above IN LOCKSTEP;
  re-run `setup_allocation_agent.py`. Full gate green.

## Open decisions

- **DECIDE-12** — PO-line slot committed-ness: a slot is freely movable until it
  explodes into vehicles; where exactly does it become a hard pin?
- Confirm assumptions 1–4 above before P0.

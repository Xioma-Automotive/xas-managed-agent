# XAS Data Model & Disruption Flow

Companion to `xas-allocation-agent-spec.md`. Captures the entity model behind the
allocation problem and the causal chain a disruption travels. Feeds `DECIDE-7`
(the XAS API data contract).

> Reflects the prototype's **v2** shape. The code that realises it: the rich
> world is fabricated by `scenario_engine/` and flattened to the solver snapshot
> by `xas_allocation/flatten.py`. Still the *proposed* contract, not a confirmed
> one (DECIDE-7).

---

## Direction of flow: supply-first

Cars are planned and ordered **before** demand exists. The supply chain builds a
pool of planned cars; customers then place Sales Orders whose rows **pull** from
that pool via allocation.

```
1 · SUPPLY (we plan & order cars)            2 · DEMAND (customers pull)

Purchase Order (PO)                          Customer
      │ 1 → many                                  │ places
      ▼                                           ▼
Purchase Delivery Note (PDN)                 Sales Order (SO)
      │ explodes into                             │ has rows
      ▼                                           ▼
   Vehicle (VIN) ─┐                        vehicle order ROW
                  ├── pool of supply ─── allocation ──┘
   PO-line slot ──┘   (a ROW ← a Vehicle OR a PO-line slot)
   (a future car,
    keyed PO-model-row)
```

Two things become the allocatable **unit** of supply: a concrete **Vehicle**
(already built, has a VIN) and a **PO-line slot** (a future car of a PO line,
not yet exploded into a vehicle). A row early in the pipeline is allocated to a
slot; later, once a physical car exists, to a Vehicle.

---

## Entities

### Purchase Order (PO) — supply
- Supplier
- Lines, each: `sales model`, `quantity`. One line is addressed as **PO-model-row**
  (e.g. `PO-150-1-5` = PO 150, model line 1, row 5).
- What we ordered from the supplier. One PO → many PDNs.
- A PO line item can be **allocated directly, as a PO-line slot**, before any
  physical vehicle exists for it.

### Purchase Delivery Note (PDN) — supply
- A delivery against a PO; **can be partial**
- Lines mirror PO lines: `sales model`, `quantity`
- Cars from one PO may be split across multiple PDNs; the split always traces
  back to the PO (ordered 10 SM1, a given PDN lists only 5).
- Each PDN line **explodes into rows of individual Vehicle records** — this is
  where abstract "model × qty" becomes physical, trackable units.

### Vehicle — supply (a concrete allocatable unit)
- `vehicle id` (a VIN — the "vehicle shield")
- `sales model` (inherited from the PDN → PO line)
- `planned delivery date` ← **the mutable field disruptions write to**
- `location state` — where the vehicle physically is (pipeline below)
- `po ref` — the PO line it fulfils

### PO-line slot — supply (a *future* allocatable unit)
- Identified by its **PO-model-row** ref (e.g. `PO-150-1-5`)
- `sales model`, a `planned delivery date`, `location state = future`
- A not-yet-built car. Freely re-allocatable until a PDN explodes it into
  concrete Vehicles (see `DECIDE-12`).

### Customer — demand
- Places Sales Orders. One SO belongs to exactly one customer.

### Sales Order (SO) — demand
- `customer`
- **vehicle order rows**, each (the allocatable demand unit — one car):
  - `sales model`
  - `price` (display-only in the prototype)
  - `promised date` — the customer commitment; **tardiness is measured against it**
  - `eta date` — the originally-expected delivery (frozen); a discrepancy is when
    the allocated supply now delivers past it
  - `n_prior_delays` — supply-side delays before us (escalates weight, §2)
  - `times_rescheduled` — reschedules **our** repair loop caused, for fairness
    (`DECIDE-11`): a repeatedly-bumped row gets heavier so it isn't delayed again
  - the current allocation: a **Vehicle** or a **PO-line slot**

### Allocation — the demand↔supply link
- Binds an SO **row** to a supply item (a Vehicle or a PO-line slot).
- This is the bipartite matching in the spec: **row = "order"**, supply item =
  "unit". The solver treats both supply kinds identically (each is capacity-1
  with a `sales model` + a date), so a row can be re-linked between a slot and a
  vehicle.

---

## Vehicle location pipeline (early → late)

```
future → sea → port → transfer → bonded → pdi → (committed?)
```

- Cooler / earlier = freely reassignable. (A PO-line slot sits at `future`.)
- Warmer / later = harder to move.
- Where "committed" begins (`pdi`? `bonded`?) is open — see `DECIDE-3`
  (commit-point states for recall pins). Prototype default: `{bonded, pdi}`.

---

## Disruption path (the repair trigger)

A problem upstream does **not** hit allocation directly. It travels:

```
PO / PDN problem   →   supply planned          →   allocation may
(stuck, delayed)       delivery date updates        need repair
```

1. Something happens to a PO or PDN (stuck, delayed). In the prototype the
   engine delays **one whole PO**.
2. The affected **supply items' planned delivery date** slips — every PO-line
   slot and every Vehicle under that PO.
3. That date shift breaks the rows riding those items (the supply can no longer
   meet the row's promised date) → triggers the solver repair described in the
   spec.

This matches the spec's core loop: the disruption changes a field on the data
(arrival timing), the agent re-pulls, and the solver repairs from the incumbent.

---

## Implications for `DECIDE-7` (data contract)

- Supply is the **union {Vehicles, PO-line slots}**; a row allocates to either.
- `planned_delivery_date` (on both supply kinds) is the **mutable field**:
  disruptions write it, allocation reads it (drives `tardiness`, §2). Real dates
  (`YYYY-MM-DD`); tardiness in **days**.
- `sales model` is the shared key that makes eligibility a plain **equality**
  against the row's model — not a fuzzy rule (no LLM residual).
- `location state` maps onto the time fence / commit-point logic (`DECIDE-3`);
  a PO-line slot is never committed until it explodes into vehicles (`DECIDE-12`).
- Row-level fairness (`times_rescheduled`, `DECIDE-11`) and the working-set
  **scope** filter (customer / month / PO) are agent-side levers over this data,
  not new entities — see `SKILL.md`.

## Resolved in the prototype (were open questions)

- **Does a row reference a specific vehicle, or only model + qty until
  allocation?** Both are possible: a row is allocated to **either** a concrete
  Vehicle **or** a PO-line slot (model+row of a PO). Slot early, vehicle once
  built.
- **Automatic disruption event, or planner-triggered?** Planner-triggered: the
  planner pulls a fresh snapshot (optionally scoped to a slice) and the agent
  maps the discrepancies and repairs. No auto-wake in the prototype.

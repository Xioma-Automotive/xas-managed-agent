# XAS Data Model & Disruption Flow

Companion to `xas-allocation-agent-spec.md`. Captures the entity model behind the
allocation problem and the causal chain a disruption travels. Feeds `DECIDE-7`
(the XAS API data contract).

> Reflects the prototype's **v2** shape. The code that realises it: the rich
> world is fabricated by `scenario_engine/` and flattened to the solver snapshot
> by `xas_allocation/flatten.py`. Still the *proposed* contract, not a confirmed
> one (DECIDE-7).

---

## The one job

**Match each VSO row to one vehicle — real or future — so its promised date is
met, at the lowest total cost.** Everything below is in service of that
sentence.

## Real XAS entities ↔ this model

The XAS system exposes four **jobcard** types plus a standalone vehicle record.
They are not four separate things a Sales Order chooses between — they are
**stages of one supply pipeline** plus the demand side. This is the vocabulary
the rest of the doc uses (abstract name in the right column, kept because the
solver code still uses it):

| XAS entity | What it is | Row granularity | Abstract name |
| --- | --- | --- | --- |
| **VPO** (Vehicle Purchase Order) jobcard | What we ordered from the factory | one row = `model × quantity` | Purchase Order (PO) |
| **VGR** (Vehicle Goods Receipt) jobcard | A shipment against a VPO; one VPO may split across several VGRs | one row = **one car** (qty 1) | Purchase Delivery Note (PDN) |
| **Inventory vehicle** (not a jobcard) | The concrete car, born when shipping info arrives; carries a VIN | — (it *is* the unit) | Vehicle |
| **VSO** (Vehicle Sales Order) jobcard | What a customer ordered | one row = **one wanted car** + allocation pointer | Sales Order (SO) |

A jobcard is a header + **jobitems** (its rows). What a *row* means narrows as
the car moves down the pipeline (model×qty → one car → a VIN). The parent
jobcard's `JobClassification` (`"VPO"` / `"VGR"` / `"VSO"`) is what distinguishes
the types — **not** `JobItemType`, since VGR and VSO rows are both typed
`"SpareParts"`.

---

## Direction of flow: supply-first

Cars are planned and ordered **before** demand exists. The supply chain builds a
pool of vehicles; customers then place Sales Orders whose rows **pull** from that
pool via allocation.

```
1 · SUPPLY (we plan, order, ship cars)          2 · DEMAND (customers pull)

VPO jobcard  (row = model × qty)                Customer
      │ ships ⇒ explodes into                        │ places
      ▼                                              ▼
VGR jobcard  (row = one car, qty 1)             VSO jobcard  (row = one wanted car)
      │ becomes                                       │
      ▼                                               │
Inventory Vehicle (VIN)                               │
      │                                               │
      └──── the SUPPLY POOL ◄──── allocation ─────────┘
             (a VSO row ← ONE vehicle: real OR future)
```

### The supply pool: two kinds of vehicle

The pool holds **one kind of thing — a vehicle, capacity 1** — in one of two
flavors:

- **future vehicle** — a VPO row's `model × qty` expanded into `qty`
  capacity-1 slots. Not yet built. (`kind: "po_line"`, `location_state:
  "future"` in the snapshot.)
- **real vehicle** — an inventory vehicle, born from a VGR when cars ship.
  Exists as soon as the record does — **even while in transit** (a real record
  can sit at `Status = "On The Way"` with a VIN but no arrival date yet).
  (`kind: "vehicle"`.)

A VSO row always binds to *a vehicle*; the only question is which flavor. As its
car ships, its binding naturally migrates future → real.

### Soft vs hard allocation

The flavor of the vehicle a VSO row is bound to names the **allocation**:

| Binding | Name | Cost to break |
| --- | --- | --- |
| VSO row → **future** vehicle | **soft** | cheap — freely reassignable |
| VSO row → **real** vehicle | **hard** | **expensive, but movable** |

Hard is *not* a wall. The repair loop may bump a hard allocation "for the sake
of another" order — it just pays a large cost to do so. This is a two-level
distinction: **soft vs hard, no gradient.** We deliberately do *not* grade the
cost along a vehicle's pipeline position — see "Location: not available" below.

---

## Location: not available (why there is no gradient)

An earlier version of this doc imagined a fine reassignability gradient along a
vehicle location pipeline (`future → sea → port → bonded → pdi`). The real
inventory-vehicle API does **not** carry a usable physical location: on a live
record `VehicleLocation`, `PortLocation`, `ParkingLot`, `BinLocation`,
`Warehouse`, `Branch`, `EtaDealer`, `DeliveryDate`, `PDICompletedDateTime`,
`HandoverDate`, and `CustomClearanceDate` are all `null`. The only populated
position signal is a coarse `Status` enum (e.g. `{Code:"02", Name:"On The
Way"}`), whose code dictionary we don't have, alongside other status axes
(`PurchaseStatus`, `SalesStatus`, `InventoryStatus`).

So the model collapses to the one bit we *can* read reliably: **is the bound
vehicle real or future** (equivalently, the VSO row's `AllocSourceClassification`
— VGR ⇒ hard, VPO ⇒ soft — or the supply `kind`). No status parsing, no
location pipeline.

---

## Entities

### VPO — Vehicle Purchase Order (supply)
- What we ordered from the factory. Header carries the estimated dates a
  disruption later moves: `DeliveryDate`, `ShippingETA`, `DueDateTime`; plus
  logistics context (`OrderedBy`, shipping line / vessel in `GenericNote*`) and a
  chain of statuses (`JobStatus`, `LogisticsStatus`, `ComplianceStatus`).
- **jobitems** are model-level: `Label` (e.g. "Porsche 718 Cayman"), `Quantity`
  (e.g. 7), `SalesModelCode`, `JobItemType: "ModelItem"`.
- Each jobitem row of quantity *n* is **`n` future vehicles** (PO-line slots),
  addressed as a **PO-model-row** (e.g. `PO-150-1-5` = PO 150, model line 1,
  slot 5). Freely re-allocatable until a VGR explodes it into concrete vehicles.

### VGR — Vehicle Goods Receipt (supply)
- A shipment against a VPO; **can be partial**, and one VPO may split across
  several VGRs (the split traces back to the VPO).
- **explodes** the VPO's `model × qty` into **one row per car** (`Quantity: 1`).
  Rows carry the allocation block (`AllocSourceClassification`,
  `AllocSourceJobNum`, `AllocSourceLineNum`, `AllocStatus`, `AllocatedQty`,
  `AvailableQty`, …) that records what pulls from them.

### Inventory vehicle — the concrete allocatable unit
- Not a jobcard. Born when shipping info arrives. Carries a `Vin`, model
  identity, a coarse `Status` (see above), and milestone dates that fill in as it
  advances (`FirstRegDate`, `PDICompletedDateTime`, `EtaDealer`, `HandoverDate` —
  mostly `null` early).
- A **real vehicle** in the pool. Hard-allocatable the moment it exists.

### VSO — Vehicle Sales Order (demand)
- What a customer ordered. One VSO belongs to one customer.
- **jobitems** are the allocatable demand unit — one wanted car each:
  - a `sales model` (the eligibility key — see below)
  - `promised_date` — the customer commitment; **tardiness is measured against it**
  - `eta_date` — originally-expected delivery (frozen); a discrepancy is when the
    allocated supply now delivers past it
  - `n_prior_delays` — supply-side delays before us (escalates weight, §2)
  - `times_rescheduled` — reschedules **our** repair loop caused, for fairness
    (`DECIDE-11`): a repeatedly-bumped row gets heavier so it isn't delayed again
  - the current **allocation pointer** (WAD-6604): `AllocSourceClassification`
    (VPO ⇒ soft / VGR ⇒ real ⇒ hard), `AllocSourceJobNum`, `AllocSourceLineNum`,
    `AllocatedQty` — i.e. which supply row, at which stage, this row pulls from.

### Allocation — the demand↔supply link
- Binds a VSO **row** to one vehicle (future or real).
- This is the bipartite matching in the spec: **row = "order"**, vehicle =
  "unit". The solver treats both vehicle flavors identically (each is capacity-1
  with a `sales_model` + a date), so a row can be re-linked between a future slot
  and a real vehicle.

> **Eligibility key — still open.** VPO rows key on `SalesModelCode`
> (`202509231112`), while VGR/VSO rows use a longer `DMSDocItemCode`
> (`T7160USSPMH0006`) with color/trim in the label. So a row's match against a
> *future* vehicle may be model-level while its match against a *real* vehicle is
> spec-exact. The prototype assumes a single `sales_model` equality
> (`flatten.py`). Settle empirically by diffing a real already-allocated pair
> (a VSO row with `Alloc*` populated → its source row). Independent of soft/hard.

---

## The objective

The solver minimises a single cost:

```
total cost  =  Σ tardiness(order)          missing the VSO row's promised date
            +  Σ break_cost(reallocation)   disturbing an existing binding

break_cost  =  { soft: cheap, hard: expensive }     hard ≫ soft
```

It bumps a hard allocation only when the tardiness it **saves** on the promoted
order exceeds the `hard` break-cost it **pays** on the bumped one. Bumping a real
vehicle should also weigh heavier on fairness (`times_rescheduled`) than a soft
reshuffle, since that customer was closer to "your car is here".

`break_cost` (both levels) and the tardiness exchange rate are **solver
parameters**, carried in the session override (the prompt can move them, §6) with
defaults in config — not hard-coded, not new model structure. This keeps the
invariant: *the prompt moves weights and pins; a human moves the model.*

---

## Disruption path (the repair trigger)

A problem upstream does **not** hit allocation directly. It travels:

```
VPO / VGR problem   →   vehicle planned         →   allocation may
(stuck, delayed)        delivery date updates        need repair
```

1. Something happens to a VPO or VGR (stuck, delayed). In the prototype the
   engine delays **one whole VPO**.
2. The affected **vehicles' planned delivery date** slips — every future vehicle
   and every real vehicle under that VPO.
3. That date shift breaks the VSO rows riding those vehicles (the supply can no
   longer meet the row's promised date) → triggers the solver repair: re-solve
   with **soft cheap to move and hard expensive-but-movable**.

This matches the spec's core loop: the disruption changes a field on the data
(arrival timing), the agent re-pulls, and the solver repairs from the incumbent.

---

## Implications for `DECIDE-7` (data contract)

- Supply is the **pool of vehicles = {real, future}**; a VSO row allocates to
  either. `kind` (`vehicle` / `po_line`) is the flavor; that flavor *is* the
  soft/hard distinction.
- `planned_delivery_date` (on both flavors) is the **mutable field**: disruptions
  write it, allocation reads it (drives `tardiness`, §2). Real dates
  (`YYYY-MM-DD`); tardiness in **days**.
- `sales model` is the shared key for eligibility (equality) — pending the
  model-level-vs-spec-exact question above.
- **No location gradient.** Soft/hard is binary and derives from the allocation
  target's flavor, not from a parsed `Status`/location (those are `null` in
  practice). `break_cost` is a two-entry parameter.
- Row-level fairness (`times_rescheduled`, `DECIDE-11`) and the working-set
  **scope** filter (customer / month / VPO) are agent-side levers over this data,
  not new entities — see `SKILL.md`.

## Reconcile with current solver code

The converged model above says **hard = expensive-but-movable**. The current
solver treats a `committed` vehicle as a **hard wall**: committed units are
pre-committed out of the graph and `solver.py` explicitly forbids reassigning
one off its incumbent order. Under soft/hard, a real (hard) vehicle should
instead be a large finite `break_cost` the solver *can* pay, not an exclusion.
This is a change to make when the model moves off the prototype — tracked under
`DECIDE-3` (what counts as committed / immovable). Until then, "committed" in the
code and "hard" in this doc are **not** the same thing: committed = wall, hard =
expensive.

## Resolved in the prototype (were open questions)

- **Does a row reference a specific vehicle, or only model + qty until
  allocation?** Both: a VSO row allocates to **either** a real vehicle **or** a
  future vehicle (a VPO model-row slot). Future (soft) early, real (hard) once
  shipped.
- **Automatic disruption event, or planner-triggered?** Planner-triggered: the
  planner pulls a fresh snapshot (optionally scoped to a slice) and the agent
  maps the discrepancies and repairs. No auto-wake in the prototype.

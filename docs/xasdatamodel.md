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
flavors, given directly by its `VehicleClassification`:

- **future vehicle** — a car we have ordered but not yet built/received. Not
  modelled as PO-line slots or a `model × qty` expansion — it is simply a vehicle
  record flagged `VehicleClassification: "Future"`. (`vehicle_classification:
  "Future"` in the snapshot.)
- **real vehicle** — an inventory vehicle with a VIN. Exists as soon as the
  record does — **even while in transit** (a real record can sit at `Status =
  "On The Way"` with a VIN but no arrival date yet). (`VehicleClassification:
  "Vehicle"`; `vehicle_classification: "Vehicle"` in the snapshot.)

A VSO row always binds to *a vehicle*; the only question is which flavor. As its
car ships, its binding naturally migrates future → real (the classification
flips `Future` → `Vehicle`).

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
vehicle real or future** — the vehicle's `VehicleClassification`
(`Vehicle` ⇒ hard, `Future` ⇒ soft). (Equivalently, the VSO row's
`AllocSourceClassification` — VGR ⇒ hard, VPO ⇒ soft.) No status parsing, no
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
- Conceptually a jobitem row of quantity *n* stands for *n* future cars. **The
  prototype does not model this expansion**: supply is ONE flat `vehicles` list,
  and a future car is just a vehicle record flagged `VehicleClassification:
  "Future"` (no PO-line slots, no `PO-model-row` addressing, no qty-expansion).
  A future vehicle becomes real when shipping info arrives (classification flips
  to `"Vehicle"`).

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
- **jobitems** are the allocatable demand unit — one wanted car each, keyed
  `{JobKey}-{LineNum}` (the order key):
  - a `sales model` (the eligibility key — see below)
  - `delivery_date` (from the VSO header `DeliveryDate`) — the customer
    commitment; **tardiness is measured against it**
  - the discrepancy is when the allocated vehicle's `eta_dealer` now runs past
    that `delivery_date` (a delayed shipment slips `EtaDealer`)
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
  with a `sales_model` + a date), so a row can be re-linked between a future
  and a real vehicle.

The link has **two sides**, and a VSO row shows which by which one is populated:

- **soft / future side** — the `Alloc*` block
  (`AllocSourceClassification` VPO/VGR, `AllocSourceJobNum`, `AllocSourceLineNum`,
  …) points at a supply *row* (a VPO/VGR jobitem).
- **hard / real side** — `VehicleId` points at a concrete vehicle:
  `VSO.VehicleId.Code ⟷ Vehicle.VehicleCode` (exact equality). E.g. a VSO row
  with `VehicleId.Code = "11317"` is bound to the vehicle whose
  `VehicleCode = "11317"`; a row with `VehicleId = {Description: "undefined"}`
  (or no `VehicleId`) is not yet hard-linked. Two rows of the *same* spec, one
  with `VehicleId.Code` set and one without, are exactly the hard/soft pair.

#### Eligibility key: model-level, via `ModelId.Code` (resolved)

Which vehicles may fill a VSO row is a **model-level** equality, not spec-exact —
because the real vehicle does not expose the configured spec code:

| | VSO jobitem | Vehicle |
| --- | --- | --- |
| model/spec code | `SalesModelCode` = `DMSDocItemCode` = `T5040UECLMQ0009` | `ModelId.Code` = `T5040` |
| model name | `Label` = "JAECOO7 4WD Exclusive …" | `ModelId.Name` / `Description` = "JAECOO7 4WD" |
| base model | (in label) | `BaseModelId.Code` = "JAECOO7" |

The VSO code is **model (`T5040`) + config (`UECLMQ0009`)**; the vehicle carries
only the model half (`ModelId.Code`), never the config suffix, and none of its
`ItemCode` / `ExternalModelCode` / `DMSDocItemCode` fields are populated. So the
only shared, matchable key against a real vehicle is `ModelId.Code` ⟷ the model
prefix of the VSO's `SalesModelCode`. **Color/trim cannot be matched against a
real vehicle with this data** — eligibility is model-level, full stop. (Future
vehicles come from VPO `SalesModelCode`, which is model-level too.) The prototype
models this as a single `sales_model` equality (`flatten.py`), which is correct
at this granularity.

---

## The objective

The solver minimises a single cost:

```
total cost  =  Σ tardiness(order)          missing the VSO row's delivery_date
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
shipment problem   →   vehicle EtaDealer        →   allocation may
(stuck, delayed)       updates                       need repair
```

1. A shipment is stuck or delayed. In the prototype the engine delays **a
   coherent batch of vehicles — every incumbent-carrying vehicle of one model**
   (a "model X shipment slipped").
2. The affected **vehicles' `EtaDealer`** slips — future and real vehicles alike.
3. That date shift breaks the VSO rows riding those vehicles (the vehicle can no
   longer meet the row's `delivery_date`) → triggers the solver repair: re-solve
   with **soft cheap to move and hard expensive-but-movable**.

This matches the spec's core loop: the disruption changes a field on the data
(arrival timing), the agent re-pulls, and the solver repairs from the incumbent.

---

## Implications for `DECIDE-7` (data contract)

- Supply is ONE **pool of vehicles = {real, future}**; a VSO row allocates to
  either. `VehicleClassification` (`Vehicle` / `Future`) is the flavor, and that
  flavor *is* the soft/hard distinction (`Unit.vehicle_classification` in the
  snapshot). No PO-line slots, no `∪` of two supply kinds, no qty-expansion.
- `EtaDealer` (`Unit.eta_dealer`) is the **mutable field**: disruptions write it,
  allocation reads it (drives `tardiness`, §2, against the VSO's `DeliveryDate` /
  `Order.delivery_date`). Real dates (`YYYY-MM-DD`); tardiness in **days**.
- `sales model` is the shared key for eligibility (equality), and it is
  **model-level**: `Vehicle.ModelId.Code` ⟷ the model prefix of the VSO
  `SalesModelCode`. The real vehicle never carries the configured spec code, so
  color/trim is not matchable against it. The hard link to a specific vehicle is
  `VSO.VehicleId.Code ⟷ Vehicle.VehicleCode`.
- **No location gradient.** Soft/hard is binary and derives from the
  `VehicleClassification`, not from a parsed `Status`/location (those are `null`
  in practice). `break_cost` is a two-entry parameter.
- Row-level fairness (`times_rescheduled`, `DECIDE-11`) and the working-set
  **scope** filter (customer / month / model / order) are agent-side levers over
  this data, not new entities — see `SKILL.md`.

## Reconcile with current solver code

Reconciled: **hard = expensive-but-movable** is now what the code does. There is
no `committed` hard wall any more — the retired flag and its
`COMMIT_POINT_STATES` set are gone. A real (`VehicleClassification: "Vehicle"`)
allocation is a large finite `break_cost` the solver *can* pay (`DECIDE-3`), not
an exclusion; the only remaining hard wall is the frozen time fence (`DECIDE-2`),
which is physical, not a hardness property of the vehicle.

## Resolved in the prototype (were open questions)

- **Does a row reference a specific vehicle, or only model + qty until
  allocation?** Both: a VSO row allocates to **either** a real vehicle **or** a
  future vehicle (`VehicleClassification: "Future"`). Future (soft) early, real (hard) once
  shipped.
- **Automatic disruption event, or planner-triggered?** Planner-triggered: the
  planner pulls a fresh snapshot (optionally scoped to a slice) and the agent
  maps the discrepancies and repairs. No auto-wake in the prototype.

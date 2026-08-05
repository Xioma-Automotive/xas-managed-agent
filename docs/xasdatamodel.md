# XAS Data Model & Disruption Flow

Companion to `xas-allocation-agent-spec.md`. Captures the entity model behind the
allocation problem and the causal chain a disruption travels. Feeds `DECIDE-7`
(the XAS API data contract).

---

## Direction of flow: supply-first

Cars are planned and ordered **before** demand exists. The supply chain builds a
pool of planned vehicles; customers then place Sales Orders that **pull** from
that pool via allocation.

```
1 · SUPPLY (we plan & order cars)          2 · DEMAND (customers pull)

Purchase Order                              Customer
      │ 1 → many                                 │ places
      ▼                                           ▼
Purchase Delivery Note                       Sales Order
      │ explodes into                             ▲
      ▼                                           │
   Vehicle  ─────── pool of planned cars ─────────┘
                         (Allocation: SO line ← pool vehicle)
```

---

## Entities

### Purchase Order (PO) — supply
- Supplier
- Lines, each: `sales model`, `quantity`
- What we ordered from the supplier. One PO → many PDNs.

### Purchase Delivery Note (PDN) — supply
- A delivery against a PO; **can be partial**
- Lines mirror PO lines: `sales model`, `quantity`
- Cars from one PO may be split across multiple PDNs; the model/quantity split
  always traces back to the PO (ordered 10 SM1, a given PDN lists only 5).
- Each PDN line **explodes into rows of individual Vehicle records** — this is
  where abstract "model × qty" becomes physical, trackable units.

### Vehicle — supply (the allocatable unit)
- `vehicle id`
- `sales model` (inherited from the PDN line)
- `planned delivery date` ← **the mutable field disruptions write to**
- `location state` — where the vehicle physically is (pipeline below)
- Collectively the Vehicles form a **pool of planned cars**.

### Customer — demand
- Places Sales Orders.

### Sales Order (SO) — demand
- `customer`
- Lines, each:
  - `sales model`, `quantity`
  - `price`
  - `vehicle` + `ETA` — **ETA is per vehicle** (each allocated vehicle has its
    own expected delivery date)

### Allocation — the demand↔supply link
- Binds an SO line to a Vehicle from the pool.
- This is the bipartite matching in the spec: SO line = "order", Vehicle = "unit".

---

## Vehicle location pipeline (early → late)

```
future → sea → port → transfer → bonded → pdi → (committed?)
```

- Cooler / earlier = freely reassignable.
- Warmer / later = harder to move.
- Where "committed" begins (`pdi`? `bonded`?) is open — see `DECIDE-3`
  (commit-point states for recall pins).

---

## Disruption path (the repair trigger)

A problem upstream does **not** hit allocation directly. It travels:

```
PO / PDN problem   →   vehicle planned         →   allocation may
(stuck, delayed)       delivery date updates        need repair
```

1. Something happens to a PO or PDN (stuck, delayed, problem).
2. The affected **vehicles' planned delivery date** gets updated.
3. That date shift can break an existing allocation (a vehicle can no longer
   meet its SO's ETA) → triggers the solver repair described in the spec.

This matches the spec's core loop: the disruption changes a field on the data
(arrival timing), the agent re-pulls, and the solver repairs from the incumbent.

---

## Implications for `DECIDE-7` (data contract)

- `planned_delivery_date` on the Vehicle is the **mutable field**: disruptions
  write it, allocation reads it (drives `tardiness` in the §2 cost model).
- Vehicle `sales model` is the shared key that makes spec-matching an equality
  check against the SO line's model — not a fuzzy rule.
- `location state` maps onto the time fence / commit-point logic (`DECIDE-3`).

## Open questions
- Does the SO line reference specific vehicles, or only model + quantity until
  allocation assigns one?
- When a planned delivery date moves and a vehicle can no longer meet its SO's
  ETA, is that the automatic "disruption" event the agent wakes on, or does a
  planner trigger the repair manually?

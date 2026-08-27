# What the allocation pull needs from `xas-app-mcp`

The pull reads the live system through the app MCP's own tools
(`datasource.AppMcpSource`), so every field the solver needs must be on the MCP's
projection allowlist. This is that list.

Measured read-only against dev (`DMSDEV2023` / `6530d4f8d5c9e5001d6e319e`) on
2026-08-20: 25 VSOs, 1,326 vehicles. **Field names are as stored, not as
documented — the two differ, and that difference is half of this page.**

Rendered version (same content, easier to scan):
<https://claude.ai/code/artifact/da6788d2-8c37-48c5-8b9b-4ba316373fbe>

**Superseded on the order grain.** `mcp-response-schema.md` is the response
*shape* per call. It records the 2026-08-23 decisions that the eligibility key
comes off `jobItems[].JobItemCode` through a NEW tool — not the header's
`SalesModelCode`, as the "wanted car is on the header" section below concludes —
and that **there is no header call at all**: the new item tool returns
`DueDateTime`, `EntryDateTime` and `Accounts.Owner` per row. The field
*additions* below all still stand; the order-side ones just belong to the item
tool now, not to `jobCards.helpers.ts`.

> **One of these is not an omission.** `PROJECT_FIELDS` asks for `DueDate` and
> `EntryDate`. XAS stores `DueDateTime` and `EntryDateTime`; neither `DueDate` nor
> `EntryDate` exists on a job card, so the projection matches nothing and drops
> it. **0 of 25 VSOs return a promised date through the MCP while 13 have one.**
> No error, no empty field — just an absent key.

## Orders — `get_job_cards` / `get_job_card`

`JobCardsV2`, filtered `{"JobClassification":"VSO"}`. Allowlist:
`src/mcp/tools/jobCards.helpers.ts → PROJECT_FIELDS`.

| XAS field | Action | Feeds | Populated | Without it |
| --- | --- | --- | --- | --- |
| `SalesModelCode` | **add** | the eligibility key, matched against a car's `SalesModel` | 2/25 | no order matches any car; the plan is empty |
| `DueDateTime` | **rename** | the promised date lateness is measured against | 13/25 | nothing can be late, so there is nothing to repair |
| `VehicleDMSCode` | **add** | the current allocation | 9/25 | every order looks unallocated; repair becomes rebuild |
| `EntryDateTime` | **rename** | when the order was raised (provenance only since 2026-08-26 — the back-order aging term it was meant to feed is deleted) | 13/25 | nothing the solver reads |
| `SalesModelTrim` | nice | a human label for the wanted car | 1/25 | the plan names a code, not a car |
| `VehicleUUID` | nice | stable id for the allocation link | 9/25 | `VehicleDMSCode` is enough today |
| `DeliveryDate` | nice | reporting only — **not** the promise | 3/25 | nothing; listed so it is never mistaken for `DueDateTime` |
| `Accounts.Owner` | present | dealer name + `AccountUUID`, for "prefer Colmobil" | 25/25 | — |
| `JobPriority` | not needed | was the customer-priority weight | 0/25 (`Code` null) | nothing — since 2026-08-26 priority is a step the planner sets per order, never read from the record |
| `JobEntryNum`, `JobStatus`, `JobState` | present | order id; excluding cancelled/closed | 25/25 | — |

**The wanted car is on the header, not in `jobItems`.** VSO 502361 has eight items
including a helmet and labour in AW, all typed `SpareParts` — the same type as a
car — and the one that looks like a car (`T71604NZNMH0016`) names a different
model than the header's `T5040UECLMQ0009`. One VSO is one wanted car.

## Cars — `get_vehicles` / `get_vehicle`

`coreApi/vehicles`, filtered `{"status.code":{"$in":["01","02","03"]}}` → 432 rows.
Allowlist: `src/mcp/tools/vehicles.helpers.ts → VEHICLE_FIELDS`.

| XAS field | Action | Feeds | Populated | Without it |
| --- | --- | --- | --- | --- |
| `SalesModel` | **add** | the eligibility key, the car's side of the match | ~40% | no car is eligible for anything. `ModelId.Code` is already returned and is **not** a substitute: it holds `T5040` where the order asks for `T5040UECLMQ0009` |
| `AvailableBy` | **add** | the arrival date the pull READS FIRST — the one actually filled today | 19/1326 | a future car cannot be scheduled; only cars on the lot can be allocated |
| `EtaDealer` | **add** | the same arrival date, nominally the field a delay moves | 3/1326 | the fallback when `AvailableBy` is blank |
| `ExpectedCustomerDeliveryDate` | nice | a second arrival signal | 0/1326 | nothing today |
| `PortLocation`, `TrimLevel` | nice | import stage and trim, for explaining a delay | sparse | nothing today |
| `Status` | present | future vs on-the-lot — see below | 718/1326 | — |
| `VehicleCode`, `Vin`, `VehicleClassification` | present | the car id, its VIN, which pool it sits in | all | — |

**Status resolves on neither field alone — pass both through verbatim.** The
dictionary (`GET /api/coreApi/vehiclestatuses`, 12 rows) says `02` = "On The Way",
and 218 vehicles agree. Another 106 `InventoryVehicles` rows carry `02` named
`'Available For Sale '` — **with a trailing space** — a value the dictionary never
had. The code merges a shipping car with a car on the lot; an exact name match
drops all 106. Match on code AND the stripped name. The MCP is already correct
here (it passes `{Code, Name, Color}` through) — the ask is that it stays that way
and that `stripEmpty` never trims that string.

## Not fields

| Item | Action | Detail |
| --- | --- | --- |
| `JOB_CLASSIFICATIONS` | **doc fix** | The filter accepts `VSO`, `VPO`, `VGR`, `VPR`, `VRS`, `VRV`, `VSR`, `Transfer` — verified live — but the documented list names none of them, so a model can never discover the one value that matters. 23 exist in the tenant; the constant lists 17 and misses every vehicle-sales one. |
| `get_job_card(arg)` | **deploy** | The deployed tool requires `DMSJCEntry`; repo main takes `JobEntryNum`. Calling it the documented way returns plain text, not a JSON error. The deploy is behind the repo — worth checking what else is. |
| `paging.count` | works | Caps at 200, so 432 cars is 3 calls. `count: 600` returns an **empty result with no error** — a caller that guesses high silently gets nothing. |
| `compact` / `stripEmpty` | consider | Default `true` strips `null`/`""`/`[]`/`{}`. Harmless for a pull (blank and missing are both unusable) but the two become indistinguishable. `compact: false` is the escape hatch if that ever matters. |

## The edit

```ts
// jobCards.helpers.ts
- 'EntryDate', 'DueDate',                    // neither exists on a job card
+ 'EntryDateTime', 'DueDateTime',            // DueDateTime IS the promised date
+ 'SalesModelCode', 'SalesModelTrim',        // SalesModelCode IS the eligibility key
+ 'VehicleDMSCode', 'VehicleUUID',           // the current allocation

// vehicles.helpers.ts
+ 'SalesModel',                              // the eligibility key — NOT ModelId.Code
+ 'EtaDealer', 'AvailableBy',                // when a car still coming lands
```

## Two shapes, and why we took the first

Nine field names make the existing tools sufficient; the pull is then four calls
(orders, plus three pages of cars) and we filter host-side. The alternative is one
purpose-built `get_allocation_snapshot` returning only the orders and cars in
play — one round trip, no chance of a caller getting the status-name rule wrong,
but a new surface to own and allocation policy encoded in the MCP.

We read through the existing tools. The field additions are needed either way, and
they help every other consumer asking a date question.

## Availability is necessary, not sufficient

Today **1 of 25 VSOs** carries both a wanted model and a promised date, and
**3 vehicles fleet-wide** carry `EtaDealer`. Widening the MCP makes the pull
possible; filling those fields in is what makes it useful. `missing_projection()`
tells the two apart — a field absent from *every* row is a projection gap, a field
absent from *some* rows is data entry — and
`uv run python -m datasource --census` prints both.

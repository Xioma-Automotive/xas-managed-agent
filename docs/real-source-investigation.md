# Real XAS data for allocation (DECIDE-7)

What the dev tenant actually holds, and how it maps onto the solver's snapshot.
Read-only investigation, 2026-08-20, tenant `DMSDEV2023` / companyDB
`6530d4f8d5c9e5001d6e319e`, via the app MCP **and** the app's own API
(`dev.proxy.automotivecloud.net`, session cookie) — the two disagree, and the API
wins.

> **The app MCP is not a viable pull source.** It returns a *projection*: ~14 of a
> VSO's **257** fields, and it drops every field the solver needs — promised date,
> order→vehicle link, sales model. Two separate causes: an allowlist that omits
> `SalesModel` / `VehicleDMSCode` / `EtaDealer` / `AvailableBy`, and a NAME BUG —
> it asks for `DueDate` where XAS stores `DueDateTime`, so **0 of 25 VSOs return a
> promised date** although 13 have one. Read `JobCardsV2` and `coreApi/vehicles`
> directly, or widen the MCP first (different repo, and already a deploy behind:
> its `get_job_card` still takes `DMSJCEntry`, not `JobEntryNum`).

> **Status, 2026-08-20: implemented — through the MCP, not around it.**
> `datasource.AppMcpSource` reads the MCP's own `get_job_cards` + `get_vehicles`
> host-side and `map_response` filters and maps them; `python -m datasource
> --census` prints the funnel. That makes the MCP's projection a blocking
> dependency: `docs/mcp-field-spec.md` is the change request, and
> `missing_projection()` reports any field the MCP still does not return so it
> cannot be mistaken for empty data. §9 records what shipped.

---

## 1. The snapshot contract, mapped to real fields

Our snapshot is `orders / units / incumbent`. Here is what fills each, and how
confident we can be:

| Snapshot field | Real source | Status |
| --- | --- | --- |
| order id | `VSO.JobEntryNum` (e.g. 502324) | ✅ |
| order customer | `VSO.Accounts.Owner.AccountDMSCode` + `AccountName` | ✅ |
| order promised date | **`VSO.DueDateTime`** | ✅ 13 of 25 populated |
| order requested spec | **`VSO.SalesModelCode`** (header, e.g. `T5040UECLMQ0009`) | ⚠️ only 2 of 25 — and it is the binding filter |
| order → allocated unit | **`VSO.VehicleDMSCode`** / `VehicleUUID` | ✅ 9 of 25 |
| unit id | `vehicle.VehicleCode` | ✅ |
| unit spec | **`vehicle.SalesModel`** | ⚠️ 0 of 14 future Trucks, 10 of 29 in stock |
| unit real vs future | `vehicle.Status` — see §2 | ⚠️ ambiguous codes |
| unit arrival date (future) | **`VPO.ShippingETA`** | ✅ 20 of 27 VPOs |
| supply order identity | `VPO.JobEntryNum`, `VPO.VehicleDMSCode` | ⚠️ link works, data thin |
| incumbent allocation | `VSO.VehicleDMSCode` (an order that already has a car) | ✅ |
| frozen fence input | `VSO.DeliveryDate`, `VPO.LogisticsStatus`, `CustomsClearanceDate` | ❓ semantics unknown |

**Everything the solver needs exists.** Two things are genuinely undecided (§3,
§4); the rest is plumbing.

---

## 2. Vehicles: what "future" means

`GET /api/coreApi/vehicles` — 1325 vehicles, 46 fields per row.

`Status` (200-row sample) already splits future from real:

| Bucket | Statuses (code name) |
| --- | --- |
| **Future** | `01 Ordered`, `02 On The Way` |
| **Real, sellable** | `02 Available For Sale`, `03 In Stock`, `10 Available for Leasing`, `11 Available for Rent` |
| **Committed** | `04 Reserved- Sale`, `05 Reserved-Lasing`, `06 Reserved - Rent`, `07 Customer` |
| Out of scope | `08 Demo`, `09 Used`, and **10 rows with no status at all** |

Three traps:

1. **Code `02` carries two different names, and one is not in the dictionary.**
   `GET /api/coreApi/vehiclestatuses` is the authority — 12 statuses, `02` =
   "On The Way" (218 vehicles). Another **106** `InventoryVehicles` rows carry
   `02` with the name `'Available For Sale '` — **with a trailing space** — which
   the dictionary never had. So neither field resolves a status alone: filtering
   on the code merges a car still shipping with a car on the lot (the exact
   distinction allocation turns on), and matching the name without `.strip()`
   drops all 106. Match on code AND the stripped name. The full enum now ships in
   `skills/xas-reporting/index.md`, the off-dictionary value included, flagged
   `unresolved=true`.
2. **`VehicleClassification` partitions the pool.** All 92 "Available For Sale"
   rows are `InventoryVehicles`; `Ordered` / `On The Way` / `In Stock` sit under
   `Vehicle`. Five classifications exist: `Vehicle` (343 fields), `InventoryVehicles`
   (103), `Truck` = "Inventory Vehicles (Truck)" (101), `Motorcycle`, `Equipment` (עגורן).
3. **Five more status axes**, each with its own change date, each a bare `1`–`5`
   with no label in the payload: `InventoryStatus`, `SalesStatus`,
   `PurchaseStatus`, `RegulatoryStatus`, `OperationalStatus` (41 of 50 populated
   each). Their labels are now recorded in `skills/xas-reporting/index.md` (from the
   tenant's DB collections — there is **no API route and no dictionary endpoint**
   for any of them), and the live data does not respect the enums: 108 vehicles
   carry `InventoryStatus: "13"` and one carries `"blue"` against a 5-value list.
   Unusable as a filter. `IsReserved` is `false` on all 200 rows while 25 carry a
   `Reserved-*` status — so `IsReserved` is **not** the reservation flag.

Other useful fields: `SalesModel`, `BaseModelId`, `PortLocation` (8/50 — import
signal), `SalesPrice`, `MetalColorPrice`, `IsDemo`, `Segments`, `Owner.Code`.

### Adding a status — answered

- **You do not need to.** `01 Ordered` + `02 On The Way` *are* the future pool.
- The `Ordered`/`In Stock` vocabulary is **not editable**: dictionary type
  `VehicleStatus` (and `VehicleSalesStatus`, `VehicleInventoryStatus`,
  `VehiclePurchaseStatus`, `VehicleRegulatoryStatus`, `VehicleOperationalStatus`,
  `VehicleClassification`) all answer *"Not supported dictionary type"* / 500.
  Core enums, as the tenant taxonomy already says of vehicle statuses.
- The one **tenant-editable** axis is `VehicleUsageStatus`
  (`/settings/dictionary/service/VehicleUsageStatus`, `GET /api/dmsApi/DictionaryV2/{Type}`),
  and it **already contains `Future`** (Role `Custom`), beside `Cancelled`,
  `InUse`, `Overdue`, `Returned`, `Scheduled`. Editable in the UI: add, delete, Save,
  with Name / Code / Role / Color / Hebrew + English label.

---

## 3. The eligibility join — works, with a grain decision

```
VSO.jobItems[].JobItemCode  ==  vehicle.SalesModel        e.g. "T5040UECLMQ0009"
```

Byte-identical codes. This is the real analogue of the solver's `sales_model`
equality — **at trim/colour grain** (`T5040` + `UECL` + …), not model grain.

| Grain | Field | Effect on allocation |
| --- | --- | --- |
| Trim/colour | `SalesModel` / `JobItemCode` | Few eligible units per order; plans are tight, infeasibility likelier |
| Model | `ServiceModelDMSCode` / `ModelCode` (`T5040`), `BaseModelId` | Wider pools, more substitutions, may propose cars a customer did not order |

**Resolved: trim grain, and it was not a choice.** VSO 502361 wants
`T5040UECLMQ0009`; **31** vehicles match on `SalesModel` and **0** on
`ModelId.Code` (which holds `T5040`). Model grain does not join at all, so it was
never a live option. `flatten` now reads `SalesModel` with `ModelId.Code` only as
a fallback for a vehicle that has neither.

**And the join is on the HEADER, not the line items.** A VSO's car line arrives
as `JobItemType: "SpareParts"`, the same type as an actual part — the car-ness is
in the code/label, never the type. VSO 502361 carries 8 items including a helmet
and "Arbeitszeit in AW", and the one that looks like a car (`T71604NZNMH0016`, a
JAECOO7) names a DIFFERENT model than the header's `T5040UECLMQ0009`. The header
is the only coherent triple (`SalesModelCode` + `DueDateTime` + `VehicleDMSCode`),
so one VSO is one order.

---

## 4. The supply chain: VPO / VGR

The tenant models 23 job-card classifications. The ones on this path:

| Code | Name | Count | Role |
| --- | --- | --- | --- |
| `VPR` | Vehicle Purchase Requisition | — | intent to buy |
| **`VPO`** | Vehicle Purchase Order | **27** | **the supply order — carries the ETA** |
| **`VGR`** | Vehicle Goods Receipt | **24** | arrival |
| `VDN` / `VSI` | Delivery Note / Sales Invoice | 0 fields | not used in dev |
| **`VSO`** | Vehicle Sales Order | **25** | **customer demand** |
| `VRV` | Vehicle Sales Lead (R) | — | pre-order demand |
| `VRS` | Vehicle Reservation | — | soft commitment |
| `Transfer` / `VSR` | Vehicle Transfer / Stock Transfer Request | 511 / 1 | inter-branch movement |

**VPO** (246 fields) populated counts of 27:

```
ArrivalType 23 · LogisticsStatus 22 · ShippingETA 20 · DeliveryDate 19
DueDateTime 18 · OrderedBy 18 · EntryDateTime 17 · VehicleDMSCode 4
VehicleUUID 3 · CustomsClearanceDate 2 · PortOfDischarge 2
```

Example: VPO 105182, status `Open`, `ShippingETA 2026-08-11`, `DueDateTime
2026-09-02`, `VehicleDMSCode 10772`. **One VPO points at one vehicle** — no line
items on the raw single-record endpoint.

**VGR** (248 fields) of 24: `DueDateTime 20 · EntryDateTime 20 · LogisticsStatus
15 · ShippingETA 15 · ArrivalType 1`.

### Join verified, data thin

`VPO.VehicleDMSCode → vehicle.VehicleCode` resolves for all 4 VPOs that have it.
But those vehicles read `03 In Stock` (×2) or **no status** (×2), and
`SalesModel` is null on all four. So in dev:

- the *fields* to express "future vehicle arriving on date X, promised to order Y"
  all exist,
- but no record actually pairs an `Ordered`/`On The Way` vehicle with a VPO ETA
  **and** a sales model.

Any pull built on this needs fixtures that do, or it will look like the join is
broken when it is only unused.

---

## 5. Scope comes from the app, not from us

`/settings/vehicle_planning` (Settings → Vehicle Sales Setup → Vehicle Planning)
configures the planning module's data sources — the app's own answer to "what is
in scope for allocation":

- **Vehicles** → classifications: `Inventory Vehicles` (+ filter list)
- **Documents** → classifications: `Vehicle Sales Order`, `Vehicle Sales Lead (R)`
- Other tabs: `Panel Tabs`, `Header KPIs`, `Card Fields`, **`AI Insight`**
- Last modified by ProgForce 1, 17/08/2026

This explains the pool split in §2. **Mirror this config in the pull** and the
agent sees exactly the universe the human planner sees; it is per tenant, so it
belongs in the snapshot's provenance, not in our code.

---

## 6. Query grammar and limits (for whoever writes the fetch)

| | |
| --- | --- |
| Orders | `GET /api/dmsApi/JobCardsV2?filter={"JobClassification":"VSO"}&paging={"page":1,"count":20}&sort={}` → `{totalCount, list[], states[]}` |
| One order | `GET /api/dmsApi/JobCardsV2/{DMSJCEntry}` → the card flat, **no line items** (the MCP's `get_job_card` adds `jobItems`/`attachments`) |
| Vehicles | `GET /api/coreApi/vehicles?$filter=…&$paging={"page":1,"count":200}&$sort={}&pagingResponse=true` → `{total, records[]}` |
| **Param names differ per service** | `coreApi` wants **`$filter`/`$paging`/`$sort` + `pagingResponse=true`**; `dmsApi` wants them **bare**. Send the wrong pair and you get UNFILTERED rows with HTTP 200 — a silently wrong snapshot, not an error. Without `pagingResponse` the body is a bare array and the total is lost. |
| Status dictionary | `GET /api/coreApi/vehiclestatuses` → the 12 statuses with `Id`/`Code`/`Name`/`Color`. The five numeric axes have **no** route. |
| Vehicle lookup | `?search=<text>` works. `?filter={"code":...}` → 500 (`parseQueryFilter` needs a `fieldName` shape). `/vehicles/{x}` needs the ObjectId `Id`, not `VehicleCode`. |
| Dictionaries | `GET /api/dmsApi/DictionaryV2/{Type}` |
| MCP paging | `count: 200` fine; `count: 600` returns **empty with no error** |
| MCP filters | plain equality and Mongo operators (`$in`, `searchAllFields`) |
| MCP auth | every mint does `forceLogin: true` as `manager` → **kicks the browser session**. Do MCP and UI work in separate bursts. |

---

## 7. Decisions this investigation forced, and how they resolved

1. **Pull from the API, not the MCP** — the MCP cannot express the snapshot.
   ⚠️ **REVERSED 2026-08-24.** The pull reads the app MCP after all
   (`datasource.AppMcpSource`, DECIDE-7): one data seam serves both lanes, and
   the reporting lane already answered over those tools. What the investigation
   got right is that the MCP does not return everything the solver needs — that
   is now a projection gap with a change request behind it
   (`docs/mcp-field-spec.md`), not a reason for a second source. `XASApiSource`
   never shipped.
2. **Eligibility grain** — ✅ trim (`SalesModel`). Not a choice: model grain
   joins nothing (§3).
3. **Future/real rule** — ✅ `Status.Name` (stripped) in {Ordered, On The Way}
   vs {In Stock, Available For Sale}. Status-less vehicles are out of scope, and
   the five numeric axes are not usable (§2).
4. **Which classifications are in scope** — deferred, and no longer urgent: the
   pool is selected by `Status`, not by classification, so all five classifications
   contribute. `/settings/vehicle_planning` remains the right source the day this
   needs narrowing.
5. **Frozen fence** (DECIDE-3) — unchanged, still days-until-promise. The
   candidate inputs (`LogisticsStatus`, `CustomsClearanceDate`, `ArrivalType`) are
   still unresolved ObjectIds, so none of them is wired in.

## 8. Unresolved

- `ArrivalType` and `LogisticsStatus` are ObjectIds / codes with no resolved
  labels — both look load-bearing for the fence.
- `VehicleFitRating` (present on 1 VSO, and the name is suggestive for
  allocation). The five numeric status axes are now labelled in
  `skills/xas-reporting/index.md` — but see §2: the live values don't respect the enums.
- **Not opened** (the dev session expires every 30 min and each MCP mint kicks
  it): a VSO **detail page** in the UI — where `DueDateTime` is edited and whether
  car lines carry their own dates — and the `AI Insight` / `Card Fields` tabs of
  Vehicle Planning.
- The allocation cockpit (`/vehicle-planner-cockpit/vehicle-allocation`) is a
  **mock**: a cross-origin micro-frontend making **zero API calls**, AG Grid trial
  watermark, synthetic rows (`CUST-001`, "John S…"). Not an integration point.
  Its intended shape is still informative: Status `Pending`/`Approved`, `SO-`/`ST-`
  documents, internal-vs-customer flag, **Expected date** (red when late), Sales
  Model, and a Configuration string ("Chile Red, Black Interior, Premium Package").

---

## 9. What shipped, and what the pull actually yields

`datasource.AppMcpSource.pull()` → the MCP's `get_job_cards` + `get_vehicles`
(paged) → `map_response()` → the rich `{meta, vsos, vehicles, disruption}`
contract → mounted as a file → `flatten`. Nothing about the architecture moved:
collect is host-side because the pull must be ONE frozen snapshot (a live
mid-turn read makes the same override meet different rows on turn 3 than on turn
1), filter is host-side because the mounted file **is** the data snapshot,
translate stays in `flatten` in the sandbox. `map_response` is pure and tested
against a captured response (`tests/fixtures/xas_sample.json`).

The tables in §1–§2 still describe the RAW records, which is what the MCP has to
be widened to expose — they are the requirement, not the current behaviour.

Three mappings that are not obvious and are each a silent failure if wrong:

- **The disruption is derived, not declared.** XAS records no "this shipment
  slipped 21 days" manifest, but `solver.partition` builds the free set from
  `disruption.disrupted_orders`. So it is computed: an allocated order whose car
  now lands past its promise. An order with no car needs nothing — `partition`
  already frees anything unassigned.
- **The incumbent is not always a matching.** Vehicle `10831` is allocated to
  VSOs 502323, 502324 **and** 502325. A contested vehicle yields no incumbent for
  anyone (those orders become unallocated demand, which is what they are) and the
  conflict rides in `meta.conflicts` — otherwise the solver's own self-check fires
  on its input.
- **Every drop is counted and reported.** `meta.excluded` carries the funnel by
  reason, and `session.exclusion_note` turns it into the first thing the planner
  reads. On dev data a plan covers **1 of 25** sales orders; presenting that as
  the whole book is the worst outcome this pipeline can produce.

The funnel today (`python -m datasource --census`):

```
orders   25 collected  ->  1 usable      (-23 no model on the order, -1 no promised date)
vehicles 432 collected ->  ~10 usable    (-no arrival date, -no model, -no order wants this model)
CONFLICT vehicle 10831 claimed by 502323, 502324, 502325
```

**The bottleneck is the data, not the code.** To get a demonstrable repair cycle:
~8–10 VSOs need `SalesModelCode` + `DueDateTime`, and the future T5040s need
`EtaDealer` (set on **3** vehicles fleet-wide today; `AvailableBy` on 19). The
T5040 family is already a coherent slice — VSO 502361 plus the 31
`T5040UECLMQ0009` vehicles.

**One trap when filling it in:** `fence_of` freezes any order promised ≤14 days
out, and a past-due promise has a negative gap, so it freezes too. VSO 502361 is
due 2026-08-19. Pilot promises must land **more than 14 days out** or the solver
correctly refuses to move anything and the demo shows nothing.

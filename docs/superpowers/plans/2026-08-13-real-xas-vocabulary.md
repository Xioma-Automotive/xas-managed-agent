# Plan: real-XAS vocabulary for the mock + solver

Move the pull contract, the mock synthesizer, and the solver onto the real XAS
field names so the API and the solver speak one language (minimal translation).
Companion decisions live in `docs/xasdatamodel.md`.

## Decisions locked (this plan implements them)

- **Supply is one `vehicles` list.** `VehicleClassification` distinguishes:
  `"Vehicle"` = real → **hard**; `"Future"` = future → **soft**. No VPOs, no
  PO-line slots, no qty-expansion. (The real platform currently mislabels this
  `"Truck"`; the mock uses `Vehicle`/`Future`.)
- **The order is one VSO line = one car.** Unique key = `so_id` + `LineNum`
  (e.g. `VSO-6-2`). No synthetic `order_id`.
- **Dates, one per side:** `Order.delivery_date` ← `DeliveryDate` (the promise,
  tardiness vs this); `Unit.eta_dealer` ← `EtaDealer` (== `ExpectedCustomerDeliveryDate`,
  mock emits both equal; the mutable field). `eta_date` is dropped (was unused).
- **Match key is model-level:** `SalesModelCode`/`ModelCode` ↔ `ModelId.Code`.
- **Hard link:** VSO `VehicleId.Code` ↔ Vehicle `VehicleCode`.
- **Customer / priority / price:** `Accounts.Owner.{AccountName,AccountUUID}`,
  `JobPriority.Code` (mock emits A/B/C; real derivation TODO), `Prices[].GrossTotal`.
- **`po` scope dimension dropped** (no supply carries a PO ref now; reversible).

## New pull contract

`{ meta, vsos, vehicles, disruption }`

- **vsos** — VSO jobcards: header (`JobKey`, `DMSJCEntry`, `DeliveryDate`,
  `JobPriority.Code`, `JobStatus`, `Accounts.Owner.{AccountName,AccountUUID,AccountDMSCode}`,
  `ModelCode`, `SalesModelCode`) + `JobItems[]` car rows (`JobItemType:"ModelItem"`,
  `LineNum`, `SalesModelCode`, `Label`, `Quantity:1`, optional `VehicleId.Code`,
  `Prices[].GrossTotal`). Accessories/labor not modelled.
- **vehicles** — `VehicleCode`, `Vin`, `ModelId.{Code,Name}`, `Make`,
  `VehicleClassification` (`Vehicle`/`Future`), `Status.{Code,Name}`,
  `InventoryStatus`, `EtaDealer`, `ExpectedCustomerDeliveryDate`, `IsReserved`,
  `Owner`.
- **disruption** — `{ now, delayed vehicles, disrupted_orders }`; slips
  `EtaDealer`/`ExpectedCustomerDeliveryDate` on the affected vehicles.

## Field renames (solver vocabulary)

| Old | New | Source |
| --- | --- | --- |
| `Order.order_id` | `Order.so_id` + `Order.line`; key = `{so_id}-{line}` | VSO `JobKey` + jobitem `LineNum` |
| `Order.promised_date` | `Order.delivery_date` | `DeliveryDate` |
| `Order.eta_date` | *(dropped)* | — |
| `Unit.kind` (`vehicle`/`po_line`) | `Unit.vehicle_classification` (`Vehicle`/`Future`) | `VehicleClassification` |
| `Unit.planned_delivery_date` | `Unit.eta_dealer` | `EtaDealer` |
| `Unit.location_state`, `Unit.committed`, `Unit.po_ref`, `Unit.pdn` | *(dropped)* | — |
| `Unit.is_hard` | keep; `= vehicle_classification == "Vehicle"` | — |

## Work items

1. `snapshot.py` — Order/Unit dataclasses + (de)serialize to the new fields;
   `order_by_id` → `order_by_key` (key = `{so_id}-{line}`); `is_hard`.
2. `scenario_engine/generate.py` — emit `{meta, vsos, vehicles, disruption}` in
   the real shapes; deterministic; disruption slips `EtaDealer`.
3. `flatten.py` — read the real shapes 1:1 into Order/Unit; incumbent from
   `VehicleId.Code` (hard) / VSO→vehicle soft link; no slot expansion.
4. `solver.py` — rename fields; drop `po` from `_FILTER_DIMS` and `_matches`;
   `is_hard`/`break_cost_of` key off `vehicle_classification`.
5. `session.py` — rename fields; `Future`/`Vehicle` wording for slot/car.
6. `decisions.py` — reframe DECIDE-3/12 to `VehicleClassification`; drop the
   `po`-scope mention.
7. `overrides_schema.json` — drop `po`; scope/bump `orders` match `so_id` or
   `so_id-line`.
8. `alloc_tools.py` — update PULL_TOOL summary + flatten command + customer map.
9. `skills/xas-allocation/SKILL.md` — update the field vocabulary + flatten step.
10. Regenerate `data/pull.json`, refresh `data/baseline.json`.
11. Update all tests to the new shapes/names; add a flatten case over real-XAS
    records.
12. `docs/xasdatamodel.md` — future/real is `VehicleClassification`; drop VGR/
    VPO-slot union language.
13. Re-run `setup_allocation_agent.py` (solver/SKILL changed → re-deploy).

## Verify

`uv run pytest`; `PYTHONPATH=. uv run python tests/test_invariant.py`;
`uv run ruff format . && uv run ruff check .`.

## Assumptions flagged in code

- `EtaDealer` is the read field (both emitted equal; one-line switch).
- `JobPriority.Code` drives priority in the mock; real-data rule (Weight vs
  customer tier) is a TODO note.
- `po`-scope removed; re-add a source-VPO field on the vehicle if scoping by PO
  is ever needed.

# The response schema the allocation pull needs from `xas-app-mcp`

Two calls. Read by `datasource.AppMcpSource` → `map_response` → `xas_allocation.flatten`.


## Call 1 — NEW tool? — jobitems

A **list of job cards**, each carrying its own **list of jobitems**.
`totalCount` counts cards, not jobitems.

```jsonc
{
  "totalCount": 25,
  "list": [
    {                                             // ── one job card (one VSO) ──
      "JobEntryNum":  502381,
      "JobKey":       "VSO-16",
      "DueDateTime":  "2026-08-30T06:10:00Z",     // the promise
      "EntryDateTime":"2026-07-20T06:40:00Z",     // order age
      "JobStatus":    { "Code": "23", "Label": "Order" },
      "JobPriority":  { "Code": null },           // the objective weight; null on all 25
      "isCanceled":   false,
      "Branch":       "69f07fdaf930e4ee6d524dc1",
      "Accounts": { "Owner": {
        "AccountName":    "customer 08052026",
        "AccountUUID":    "69fde79b100a501b50da0b77",
        "AccountDMSCode": "10776"
      }},
      "jobitems": [                               // ── its lines ──
        {
          "LineNum":      2,                      // order key is {JobKey}-{LineNum}
          "JobItemCode":  "T5040UECLMQ0009",      // eligibility key == vehicle.SalesModel
          "SalesModelCode": "T5040UECLMQ0009",    // same value on ModelItem lines
          "JobItemType":  "ModelItem",            // "ModelItem" = a car. THE discriminator
          "JobItemStatus": "Open",                // per-line status
          "Label": "JAECOO7 4WD Exclusive - 4WD Carbon crystal green2025",
          "Quantity":     3,                      // n cars of demand on this line
          "AllocType":    "Soft",                 // the incumbent: pulls from a VPO
          "AllocQty":     3,
          "AllocSourceJobNum":  "105992",         // the VPO it pulls from
          "AllocSourceLineNum": 5,
          "InventoryStatus": "Ordered",
          "DeliveryDate": null                    // per-line date exists, unpopulated in dev
        },
        {
          "LineNum":      3,                      // a second car on the same card
          "JobItemCode":  "SM1111251137",
          "JobItemType":  "ModelItem",
          "Quantity":     4
          // …same fields as above
        },
        {
          "LineNum":      1,                      // not a car — filtered out
          "JobItemCode":  "CCO",
          "JobItemType":  "Configuration"
        }
      ]
    },
    {                                             // ── the next job card ──
      "JobEntryNum":  502382,
      "jobitems": [ /* … */ ]
    }
  ]
}
```

One card is one customer's sales order; one `ModelItem` jobitem is one wanted
car, and `Quantity` says how many. So 25 cards can carry far more than 25 orders,
and the solver's order key spans both levels: `{JobKey}-{LineNum}`.

**Qty is expanded** (2026-08-24): `flatten` emits one order per CAR, so the order
key gained a third level, `{JobKey}-{LineNum}-{n}`. Pins, forbid and scope match
at any level (car / line / VSO), and the planner report speaks lines.

What is still open is the COVERAGE. `AllocQty` says how many of a line's cars are
committed, but a line resolves to at most one vehicle and one car cannot satisfy
two orders — so the pull can identify exactly one incumbent per line however high
`AllocQty` goes. The rest are counted as
`allocation_qty_not_resolvable_to_cars` and treated as unfilled demand. Closing
that is the same VPO hop as Q1 below: without it a fleet line reads as almost
entirely unallocated, and a repair looks bigger than it is.

## Call 2 — `get_vehicles` — supply

```jsonc
// filter: {"status.code": {"$in": ["01","02","03"]}}   paging.count: 200
{
  "total": 432,
  "records": [
    {
      "VehicleCode":  "909007",                   // unit id
      "SalesModel":   "T5040UECLMQ0009",          // eligibility key, car's side
      "Status":       { "Code": "01", "Name": "Ordered", "Color": "Ordered" },
      "AvailableBy":  "2026-09-27T00:00:00.000Z", // the arrival date
      "EtaDealer":    null,                       // preferred when set
      "Description":  "Equinox-טסט",              // human label — often a template with holes
      "Make":         { "Code": "981", "Name": "Chevrolet" },
      "ModelId":      { "Code": "1", "Name": "Equinox-טסט" },
      "BaseModelId":  { "Code": "202509221445", "Name": "Supra" },
      "Year":         2020,
      "Vin":          "LVVDD21B8SC0909007",
      "LicenseNumber": null,
      "VehicleClassification": "Truck",           // XAS pool, not the solver's binding
      "InventoryStatusChangeDate": null,
      "PortLocation": null
    }
  ]
}
```

Order value is not pulled: `Prices[].GrossTotal` is `0` on every dev row and the
solver only ever displayed it.

## Open questions — for Eyal

1. **How does an allocation resolve to a vehicle?** The solver's incumbent is
   `order_key → vehicle_id`, but a line gives `AllocSourceJobNum` — a **VPO line,
   not a car**. No `VehicleId` on any line, `VehicleDMSCode` null on the card.
   Resolving it needs the VPO hop (`VPO.VehicleDMSCode`, populated 4/27).
   **Without it there is no incumbent, so nothing is ever "disrupted" and repair
   becomes full rebuild.** And if the hard link only ever lives on the card, a
   multi-line VSO cannot express more than one allocation.
2. **Where do the escalation inputs come from?** The solver weights an order by
   three counters and none has an XAS source:
   - `n_prior_delays` — how many times the **supply chain** has already slipped
     this order, before we touched it. Is there a status history to read it off,
     or does the term get dropped?
   - `days_backordered` — derivable as `now − EntryDateTime`, which the schema
     already pulls. Confirm that is the intended definition.
   - `times_rescheduled` — how many times **our own repair loop** has bumped this
     order. Ours to count, so it needs no field; confirm it should not be
     persisted between sessions.




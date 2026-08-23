# The response schema the allocation pull needs from `xas-app-mcp`

Two calls. Read by `datasource.AppMcpSource` → `map_response` → `xas_allocation.flatten`.


## Call 1 — NEW tool? — jobitems

One job card per row, its car lines nested. `totalCount` counts cards, not lines.

```jsonc
{
  "totalCount": 25,
  "list": [
    {
      "JobEntryNum":  502381,
      "JobKey":       "VSO-16",
      "DueDateTime":  "2026-08-30T06:10:00Z",   // the promise
      "EntryDateTime":"2026-07-20T06:40:00Z",   // order age
      "JobStatus":    { "Code": "23", "Label": "Order" },
      "isCanceled":   false,
      "Branch":       "69f07fdaf930e4ee6d524dc1",
      "Accounts": { "Owner": {
        "AccountName":    "customer 08052026",
        "AccountUUID":    "69fde79b100a501b50da0b77",
        "AccountDMSCode": "10776"
      }},
      "jobitems": [
        {
          "LineNum":      2,                        // order key is {JobKey}-{LineNum}
          "JobItemCode":  "T5040UECLMQ0009",        // eligibility key == vehicle.SalesModel
          "SalesModelCode": "T5040UECLMQ0009",      // same value on ModelItem lines
          "JobItemType":  "ModelItem",              // "ModelItem" = a car. THE discriminator
          "JobItemStatus": "Open",                  // per-line status
          "Label": "JAECOO7 4WD Exclusive - 4WD Carbon crystal green2025",
          "Quantity":     3,                        // n cars of demand on this line
          "AllocType":    "Soft",                   // the incumbent: pulls from a VPO
          "AllocQty":     3,
          "AllocSourceJobNum":  "105992",           // the VPO it pulls from
          "AllocSourceLineNum": 5,
          "InventoryStatus": "Ordered",
          "DeliveryDate": null                      // per-line date exists, unpopulated in dev
        },
        ...
      ]
    }
  ]
}
```


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
      "Description":  "Equinox-טסט",              // human label — dirty, see note
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

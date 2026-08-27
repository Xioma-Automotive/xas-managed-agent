# Taxonomy — xioma/DMSDEV2023 (6530d4f8d5c9e5001d6e319e)

8 entities, 51 classifications, 109 statuses, 7 branches. Generated from `mcp_servers/xioma_read_mcp/mock_data/xioma-DMSDEV2023.json` by `python -m mcp_servers.xioma_read_mcp.dump_taxonomy`.

Active only: 7 inactive classifications are omitted, along with 2 entities left with none. Everything listed here is live in the app; nothing needs an
active check. Note `index_lookup` on the MCP server does NOT filter this way — it can still
return an inactive classification.

How to read a line:
  ENTITY          — one per system entity; businessType is the plain business term.
  CLASSIFICATION  entity=<owning entity> code=<system value to query> name=<what users call it> aliases=<other names users say>
  STATUS          entity=<owning entity> classification=<owning classification code> id=<JobStatus ObjectId> code=<system/human term> state=<lifecycle bucket> closed=<close flag>
  BRANCH          id=<ObjectId, the only value that NAMES a branch — see the -2 note below> name=<what users call it> cards=<job cards there, counted live>

A classification belongs to exactly one entity — always read `entity=` with the code,
because codes are unique per entity, not globally: code "Model" exists under both the
Model and VehicleModels entities.

Filtering: a classification `code` is the system value; a JobCard status is filtered on
`JobStatus.ID` using the status `id`. A classification's code and name can diverge (code
"Transfer" is used as "Vehicle Transfer"). `unresolved=true` = status referenced by a
record but missing from the status dictionary.

Branches are tenant-wide — they hang off the company, not off an entity, which is why the
lines below carry no `entity=`. **A branch is filtered by its `id`, never by its name**:
`{"Branch": ["69f07fdaf930e4ee6d524dc1"]}` returns Main's cards, while
`{"Branch": ["Main"]}` returns 0 with no error — the same silent-empty failure as filtering
a status on its label. `{"Branch": true}` means "the branch of whoever is logged in", which
on the dev login is Main; a question about a *named* branch must send that branch's id
rather than rely on it. There is no `code=` on these lines because the branch dictionary's
own short code (`dictionarybranches.DMSCode`) is not exposed by any read tool the agent
holds — job cards carry `Branch` as a bare ObjectId, `get_job_details` does not populate it,
and neither does anything else. The `id` IS the queryable value here; if DMSCode is ever
added, it is a display string and still not a filter value.

The filter is not restricted to ids, though. The app's own job-card URL sends
`{"Branch": ["-2"]}`, and **what that selects has not been established** — "all branches",
"unassigned", and a legacy default are all consistent with what we have seen. Suggestive but
unconfirmed: `-2` is also one of the three junk values sitting in the vehicles' own `Branch`
field (below), so it may be a tenant-wide sentinel rather than anything job-card specific.
Until someone checks, only ids from the lines below may be sent.

Branch belongs to job cards only. Vehicles have a `Branch` field, but on 1327 live vehicles
5 carry anything at all and those hold `-2`, `3` and `""` — not branch ids. "Which branch
is this car at" cannot be answered from the vehicle records; do not try.

The counts below were taken live on 2026-08-23 over all 7734 job cards, every classification.
They are a sense of scale for choosing buckets, **not** an answer to report — re-count with a
filtered call. Note they sum to 7124: **610 job cards carry no branch at all**, so a
per-branch breakdown does not add up to the total, and the gap has to be said out loud
rather than folded into Main.

`Potain` is both a branch name and the display name of the `Warranty` classification, so a
lookup on that word returns two rows of different kinds. Read `kind` before using either.

`dump_taxonomy` does not emit BRANCH lines — the upstream snapshot keeps branches in
`meta.branches`, which the dumper skips — so a regeneration drops this block and it has to
be re-applied, like the Vehicle status block below.

`classification="*"` on a STATUS means the status belongs to the ENTITY, not to one
classification — every classification of that entity uses it. Vehicle is the only entity
like this: its 12 statuses are one tenant-wide dictionary
(`GET /api/coreApi/vehiclestatuses`), shared by Truck, Motorcycle, Equipment, Vehicle and
InventoryVehicles alike, which is why each of those lines reads `statuses=0`.

Vehicle statuses carry no usable id: the dictionary rows do have an `Id`, but the copy
embedded on a vehicle is only `{Code, Name, Color}`, so you filter on `status.code`
(camelCase in, PascalCase back). The 12 rows above were refreshed from that endpoint on
2026-08-20 — `dump_taxonomy` emits only the codes a classification declares, which for
Vehicle was 5 of 12, so a regeneration will drop `01`/`02`/`03` again and this block has
to be re-applied.

The vehicle's five other status axes — `InventoryStatus`, `SalesStatus`, `PurchaseStatus`,
`RegulatoryStatus`, `OperationalStatus` — are NOT listed as STATUS records: they have no
API route and no dictionary endpoint, and the live data does not respect their enums (108
vehicles carry `InventoryStatus: "13"`, one carries `"blue"`, against a 5-value list). They
arrive on a vehicle as a bare `1`–`5` with no label. Their tenant enums, from the DB
collections only: InventoryStatus 1 In Stock · 2 Out of Stock · 3 Reserved · 4 In Transit ·
5 Awaiting Inspection. SalesStatus 1 Available for Sale · 2 Sold · 3 Pending Sale ·
4 Booked · 5 Returned. PurchaseStatus 1 Ordered · 2 Pending Payment · 3 Paid · 4 Delivered ·
5 Cancelled. RegulatoryStatus 1 Compliant · 2 Non-Compliant · 3 Under Inspection ·
4 Awaiting Registration · 5 Expired Certification. OperationalStatus 1 Active · 2 In Service ·
3 Under Maintenance · 4 Decommissioned · 5 Retired. Treat these as a reading aid, not as
something to filter on.

Two live values that are not in any dictionary, both counted 2026-08-20 over all 1326
vehicles. Status code `02` comes back as **`'Available For Sale '`** — note the trailing
space, which drops all 106 rows from an exact name match — on 106 `InventoryVehicles` rows,
while the dictionary says `02` = "On The Way" (218 rows). So a vehicle status resolves on
neither field alone: match on code AND the stripped `Name` that came with it. And `Trucks` (plural, 17 vehicles)
exists as a classification value alongside `Truck` (264), so a filter on `Truck` silently
misses them; `Bus`, `Test`, `V` and `ServiceCall` appear the same way.

```
BRANCH          id="69f07fdaf930e4ee6d524dc1"  name="Main"  cards=6592
BRANCH          id="69f209400e50752cea08ce26"  name="Service Branch"  cards=252
BRANCH          id="69f07fdaf930e47496560961"  name="Sales Main"  cards=168
BRANCH          id="69f07fdaf930e474935edfd1"  name="Potain"  cards=71
BRANCH          id="69f07fdaf930e474921bc021"  name="Workshop"  cards=40
BRANCH          id="69f2fbbc0e50752cea0d512e"  name="Test123"  cards=1
BRANCH          id="69f07fdaf930e4748722ced1"  name="TestBranch"  cards=0

ENTITY          entity="Account"  businessType="accounts"  classifications=3  statuses=0
CLASSIFICATION  entity="Account"  code="supplier"  name="Accounts- Suppliers"  fields=29  statuses=0  aliases=""
CLASSIFICATION  entity="Account"  code="ExternalRepairShops"  name="Account - Leads"  fields=12  statuses=0  aliases=""
CLASSIFICATION  entity="Account"  code="customer"  name="Accounts - Customers"  fields=43  statuses=0  aliases="חשבונות - לקוחות"

ENTITY          entity="Activity"  businessType="activities"  classifications=6  statuses=0
CLASSIFICATION  entity="Activity"  code="Notes"  name="Notes"  fields=14  statuses=0  aliases=""
CLASSIFICATION  entity="Activity"  code="Message"  name="Message"  fields=4  statuses=0  aliases=""
CLASSIFICATION  entity="Activity"  code="Task"  name="Task"  fields=14  statuses=0  aliases=""
CLASSIFICATION  entity="Activity"  code="TestDrive"  name="Test Drive"  fields=18  statuses=0  aliases=""
CLASSIFICATION  entity="Activity"  code="Meeting"  name="Meeting"  fields=14  statuses=0  aliases=""
CLASSIFICATION  entity="Activity"  code="LogCall"  name="Log a Call"  fields=14  statuses=0  aliases=""

ENTITY          entity="Item"  businessType="items"  classifications=11  statuses=0
CLASSIFICATION  entity="Item"  code="ServicePackage"  name="Package"  fields=18  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="Sublet"  name="Sublet"  fields=16  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="Configuration"  name="Model Configuration"  fields=19  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="SpareParts"  name="Spare Parts"  fields=29  statuses=0  aliases="חֲלָפִים | Ersatzteile"
CLASSIFICATION  entity="Item"  code="CustomerConcern"  name="Concern"  fields=19  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="OilsAndMaterials"  name="Oils And Materials"  fields=20  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="Accessories"  name="Model Accessory"  fields=19  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="Services"  name="Model Services"  fields=14  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="ModelPackages"  name="Model Package"  fields=18  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="Labor"  name="Labor"  fields=21  statuses=0  aliases=""
CLASSIFICATION  entity="Item"  code="ModelItem"  name="Model Item"  fields=15  statuses=0  aliases=""

ENTITY          entity="JobCard"  businessType="jobs"  classifications=23  statuses=96
CLASSIFICATION  entity="JobCard"  code="Contract"  name="0510"  fields=133  statuses=0  aliases="Contract"
CLASSIFICATION  entity="JobCard"  code="Parts"  name="Parts Order"  fields=89  statuses=17  aliases="Parts Order External"
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c75"  code="23"  name="Order"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a05a65b6765"  code="99"  name="Canceled"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a37e96ff5c7"  code="22"  name="QUOTAION"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c76"  code="25"  name="Vehicle Ready"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a05a65b6763"  code="24"  name="For Purchase"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c77"  code="98"  name="Credited"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c74"  code="21"  name="Active"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a05a65b6762"  code="02"  name="Approved"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c72"  code="07"  name="Check Out"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a37e96ff5c5"  code="06"  name="Check In"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a37e96ff5c4"  code="04"  name="Parts Issued, Waiting for Invoice"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c73"  code="1"  name="Open"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a37e96ff5c6"  code="20"  name="Draft"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Parts"  id="6530d9a89c098a33be3e0c71"  code="05"  name="Waiting for Parts"  state="Has Alert"  closed=false
CLASSIFICATION  entity="JobCard"  code="ServiceCall"  name="Service Call"  fields=260  statuses=12  aliases="Service Call Ext | קריאת שירות | Service-Anruf"
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a33be3e0c76"  code="25"  name="Vehicle Ready"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a33be3e0c77"  code="98"  name="Credited"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a33be3e0c74"  code="21"  name="Active"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a05a65b6762"  code="02"  name="Approved"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a33be3e0c72"  code="07"  name="Check Out"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a37e96ff5c5"  code="06"  name="Check In"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a05a65b6765"  code="99"  name="Canceled"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a33be3e0c73"  code="1"  name="Open"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="ServiceCall"  id="6530d9a89c098a37e96ff5c7"  code="22"  name="QUOTAION"  state="In Process"  closed=false
CLASSIFICATION  entity="JobCard"  code="VPO"  name="Vehicle Purchase Order"  fields=104  statuses=4  aliases="הזמנת רכש רכב"
STATUS          entity="JobCard"  classification="VPO"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false
STATUS          entity="JobCard"  classification="VPO"  id="6530d9a89c098a33be3e0c75"  code="23"  name="Order"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="VPO"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="VPO"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
CLASSIFICATION  entity="JobCard"  code="BlanketAgreement"  name="charges"  fields=47  statuses=0  aliases=""
CLASSIFICATION  entity="JobCard"  code="VSI"  name="Vehicle Sales Invoice"  fields=0  statuses=0  aliases=""
CLASSIFICATION  entity="JobCard"  code="VDR"  name="Vehicle Return from Customer"  fields=0  statuses=0  aliases=""
CLASSIFICATION  entity="JobCard"  code="RentContract"  name="Test 0310"  fields=56  statuses=0  aliases=""
CLASSIFICATION  entity="JobCard"  code="Warranty"  name="Potain"  fields=137  statuses=5  aliases=""
STATUS          entity="JobCard"  classification="Warranty"  id="6530d9a89c098a33be3e0c75"  code="23"  name="Order"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Warranty"  id="6530d9a89c098a33be3e0c72"  code="07"  name="Check Out"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Warranty"  id="6530d9a89c098a33be3e0c73"  code="1"  name="Open"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Warranty"  id="6530d9a89c098a37e96ff5c4"  code="04"  name="Parts Issued, Waiting for Invoice"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="Warranty"  id="6530d9a89c098a33be3e0c71"  code="05"  name="Waiting for Parts"  state="Has Alert"  closed=false
CLASSIFICATION  entity="JobCard"  code="Quote"  name="Vehicle Service Quote"  fields=142  statuses=6  aliases=""
STATUS          entity="JobCard"  classification="Quote"  id="6530d9a89c098a37e96ff5c6"  code="20"  name="Draft"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Quote"  id="6530d9a89c098a37e96ff5c5"  code="06"  name="Check In"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Quote"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="Quote"  id="6530d9a89c098a33be3e0c73"  code="1"  name="Open"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Quote"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Quote"  id="6530d9a89c098a05a65b6762"  code="02"  name="Approved"  state="Has Alert"  closed=false
CLASSIFICATION  entity="JobCard"  code="Transfer"  name="Vehicle Transfer"  fields=142  statuses=7  aliases="xx"
STATUS          entity="JobCard"  classification="Transfer"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false
STATUS          entity="JobCard"  classification="Transfer"  id="6530d9a89c098a33be3e0c73"  code="1"  name="Open"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Transfer"  id="6530d9a89c098a37e96ff5c5"  code="06"  name="Check In"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Transfer"  id="6530d9a89c098a33be3e0c72"  code="07"  name="Check Out"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Transfer"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Transfer"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="Transfer"  id="6530d9a89c098a05a65b6765"  code="99"  name="Canceled"  state="Closed"  closed=true
CLASSIFICATION  entity="JobCard"  code="Invoice"  name="אישור תוכן כרטיס עבודה"  fields=29  statuses=6  aliases=""
STATUS          entity="JobCard"  classification="Invoice"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Invoice"  id="6530d9a89c098a33be3e0c73"  code="1"  name="Open"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Invoice"  id="6530d9a89c098a05a65b6762"  code="02"  name="Approved"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="Invoice"  id="6530d9a89c098a37e96ff5c5"  code="06"  name="Check In"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Invoice"  id="6530d9a89c098a05a65b6763"  code="24"  name="For Purchase"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Invoice"  id="6530d9a89c098a33be3e0c75"  code="23"  name="Order"  state="In Process"  closed=false
CLASSIFICATION  entity="JobCard"  code="VSR"  name="Vehicle Stock Transfer Request"  fields=59  statuses=0  aliases=""
CLASSIFICATION  entity="JobCard"  code="VDN"  name="Vehicle Delivery Note"  fields=0  statuses=0  aliases=""
CLASSIFICATION  entity="JobCard"  code="VRS"  name="Vehicle Reservation (VRS)"  fields=108  statuses=0  aliases=""
CLASSIFICATION  entity="JobCard"  code="Service"  name="Distinct_name"  fields=649  statuses=16  aliases="External Vehicle Service Order | קריאת שירות | Vehicle Service Order | כרטיס עבודה | Jobkarte"
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a37e96ff5c6"  code="20"  name="Draft"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a33be3e0c73"  code="1"  name="Open"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a37e96ff5c7"  code="22"  name="QUOTAION"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a05a65b6762"  code="02"  name="Approved"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a33be3e0c71"  code="05"  name="Waiting for Parts"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a05a65b6765"  code="99"  name="Canceled"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a33be3e0c76"  code="25"  name="Vehicle Ready"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a33be3e0c74"  code="21"  name="Active"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a33be3e0c72"  code="07"  name="Check Out"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a33be3e0c77"  code="98"  name="Credited"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a05a65b6763"  code="24"  name="For Purchase"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a37e96ff5c5"  code="06"  name="Check In"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="Service"  id="6530d9a89c098a37e96ff5c4"  code="04"  name="Parts Issued, Waiting for Invoice"  state="Has Alert"  closed=false
CLASSIFICATION  entity="JobCard"  code="VSO"  name="Vehicle Sales Order"  fields=90  statuses=3  aliases=""
STATUS          entity="JobCard"  classification="VSO"  id="6530d9a89c098a37e96ff5c6"  code="20"  name="Draft"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="VSO"  id="6530d9a89c098a33be3e0c75"  code="23"  name="Order"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="VSO"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
CLASSIFICATION  entity="JobCard"  code="Reservation"  name="Car Reservation"  fields=0  statuses=0  aliases=""
CLASSIFICATION  entity="JobCard"  code="VRV"  name="Vehicle Sales Lead (R)"  fields=163  statuses=4  aliases="מכירת רכב"
STATUS          entity="JobCard"  classification="VRV"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false
STATUS          entity="JobCard"  classification="VRV"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="VRV"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="VRV"  id="6530d9a89c098a05a65b6765"  code="99"  name="Canceled"  state="Closed"  closed=true
CLASSIFICATION  entity="JobCard"  code="VIC"  name="Vehicle Invoice Cancel/Credit"  fields=0  statuses=0  aliases="Vehicle Invoice Cancel | Credit"
CLASSIFICATION  entity="JobCard"  code="Insurance"  name="Damage and Insurance Claim"  fields=118  statuses=7  aliases="Damage and  ESPNA"
STATUS          entity="JobCard"  classification="Insurance"  id=""  code="6"  name=""  unresolved=true
STATUS          entity="JobCard"  classification="Insurance"  id=""  code="11"  name=""  unresolved=true
STATUS          entity="JobCard"  classification="Insurance"  id=""  code="8"  name=""  unresolved=true
STATUS          entity="JobCard"  classification="Insurance"  id=""  code="9"  name=""  unresolved=true
STATUS          entity="JobCard"  classification="Insurance"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false
STATUS          entity="JobCard"  classification="Insurance"  id="6530d9a89c098a05a65b6762"  code="02"  name="Approved"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="Insurance"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
CLASSIFICATION  entity="JobCard"  code="VPR"  name="Vehicle Purchase Requisition"  fields=142  statuses=8  aliases=""
STATUS          entity="JobCard"  classification="VPR"  id="6530d9a89c098a33be3e0c73"  code="1"  name="Open"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="VPR"  id="6530d9a89c098a05a65b6765"  code="99"  name="Canceled"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="VPR"  id="6530d9a89c098a05a65b6764"  code="97"  name="Closed"  state="Closed"  closed=true
STATUS          entity="JobCard"  classification="VPR"  id="6530d9a89c098a33be3e0c71"  code="05"  name="Waiting for Parts"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="VPR"  id="6530d9a89c098a33be3e0c70"  code="03"  name="In Process"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="VPR"  id="6530d9a89c098a37e96ff5c7"  code="22"  name="QUOTAION"  state="In Process"  closed=false
STATUS          entity="JobCard"  classification="VPR"  id="6530d9a89c098a05a65b6762"  code="02"  name="Approved"  state="Has Alert"  closed=false
STATUS          entity="JobCard"  classification="VPR"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false
CLASSIFICATION  entity="JobCard"  code="Evaluation"  name="Service Lead"  fields=89  statuses=1  aliases=""
STATUS          entity="JobCard"  classification="Evaluation"  id="6530d9a89c098a33be3e0c6f"  code="01"  name="New"  state="New"  closed=false

ENTITY          entity="Model"  businessType=""  classifications=1  statuses=0
CLASSIFICATION  entity="Model"  code="Model"  name="Model"  fields=154  statuses=0  aliases=""

ENTITY          entity="SalesModelObjects"  businessType=""  classifications=1  statuses=0
CLASSIFICATION  entity="SalesModelObjects"  code="NewVehicle"  name="Sales Models"  fields=94  statuses=0  aliases=""

ENTITY          entity="Vehicle"  businessType="vehicles"  classifications=5  statuses=13
CLASSIFICATION  entity="Vehicle"  code="Truck"  name="Inventory Vehicles (Truck)"  fields=101  statuses=0  aliases="Inventory Vehicles - EXT | 333 | 1212 | Truck"
CLASSIFICATION  entity="Vehicle"  code="Motorcycle"  name="Motorcycle 1"  fields=78  statuses=0  aliases="Motorcycle - External"
CLASSIFICATION  entity="Vehicle"  code="Equipment"  name="עגורן"  fields=77  statuses=0  aliases=""
CLASSIFICATION  entity="Vehicle"  code="Vehicle"  name="test"  fields=343  statuses=0  aliases="Vehicles - External | My Vehicle | Vehicles | כלי רכב"
CLASSIFICATION  entity="Vehicle"  code="InventoryVehicles"  name="Inventory Vehicles"  fields=103  statuses=0  aliases=""
STATUS          entity="Vehicle"  classification="*"  id=""  code="01"  name="Ordered"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="02"  name="On The Way"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="03"  name="In Stock"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="04"  name="Reserved- Sale"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="05"  name="Reserved-Lasing"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="06"  name="Reserved - Rent"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="07"  name="Customer"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="08"  name="Demo"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="09"  name="Used"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="10"  name="Available for Leasing"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="11"  name="Available for Rent"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="99"  name="Disabled"  state=""  closed=false
STATUS          entity="Vehicle"  classification="*"  id=""  code="02"  name="Available For Sale"  unresolved=true

ENTITY          entity="VehicleModels"  businessType=""  classifications=1  statuses=0
CLASSIFICATION  entity="VehicleModels"  code="Model"  name="Model"  fields=0  statuses=0  aliases=""

```

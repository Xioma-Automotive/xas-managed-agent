# Taxonomy — xioma/DMSDEV2023 (6530d4f8d5c9e5001d6e319e)

8 entities, 51 classifications, 101 statuses. Generated from `mcp_servers/xioma_read_mcp/mock_data/xioma-DMSDEV2023.json` by `python -m mcp_servers.xioma_read_mcp.dump_taxonomy`.

Active only: 7 inactive classifications are omitted, along with 2 entities left with none. Everything listed here is live in the app; nothing needs an
active check. Note `index_lookup` on the MCP server does NOT filter this way — it can still
return an inactive classification.

How to read a line:
  ENTITY          — one per system entity; businessType is the plain business term.
  CLASSIFICATION  entity=<owning entity> code=<system value to query> name=<what users call it> aliases=<other names users say>
  STATUS          entity=<owning entity> classification=<owning classification code> id=<JobStatus ObjectId> code=<system/human term> state=<lifecycle bucket> closed=<close flag>

A classification belongs to exactly one entity — always read `entity=` with the code,
because codes are unique per entity, not globally: code "Model" exists under both the
Model and VehicleModels entities.

Filtering: a classification `code` is the system value; a JobCard status is filtered on
`JobStatus.ID` using the status `id`. Vehicle statuses have no id (core enums) — filter
on `code`. A classification's code and name can diverge (code "Transfer" is used as
"Vehicle Transfer"). `unresolved=true` = status referenced by a card but missing from the
status dictionary.

```
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

ENTITY          entity="Vehicle"  businessType="vehicles"  classifications=5  statuses=5
CLASSIFICATION  entity="Vehicle"  code="Truck"  name="Inventory Vehicles (Truck)"  fields=101  statuses=0  aliases="Inventory Vehicles - EXT | 333 | 1212 | Truck"
CLASSIFICATION  entity="Vehicle"  code="Motorcycle"  name="Motorcycle 1"  fields=78  statuses=0  aliases="Motorcycle - External"
CLASSIFICATION  entity="Vehicle"  code="Equipment"  name="עגורן"  fields=77  statuses=0  aliases=""
CLASSIFICATION  entity="Vehicle"  code="Vehicle"  name="test"  fields=343  statuses=5  aliases="Vehicles - External | My Vehicle | Vehicles | כלי רכב"
STATUS          entity="Vehicle"  classification="Vehicle"  id=""  code="10"  name="Available for Leasing"  state=""  closed=false
STATUS          entity="Vehicle"  classification="Vehicle"  id=""  code="06"  name="Reserved - Rent"  state=""  closed=false
STATUS          entity="Vehicle"  classification="Vehicle"  id=""  code="05"  name="Reserved-Lasing"  state=""  closed=false
STATUS          entity="Vehicle"  classification="Vehicle"  id=""  code="04"  name="Reserved- Sale"  state=""  closed=false
STATUS          entity="Vehicle"  classification="Vehicle"  id=""  code="11"  name="Available for Rent"  state=""  closed=false
CLASSIFICATION  entity="Vehicle"  code="InventoryVehicles"  name="Inventory Vehicles"  fields=103  statuses=0  aliases=""

ENTITY          entity="VehicleModels"  businessType=""  classifications=1  statuses=0
CLASSIFICATION  entity="VehicleModels"  code="Model"  name="Model"  fields=0  statuses=0  aliases=""

```

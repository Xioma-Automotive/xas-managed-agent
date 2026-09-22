---
name: xas-transfer
description: >-
  Lead a worker through the vehicle transfer job in front of them — find the
  one they should do next, collect the vehicle readings by asking for them,
  then walk the check-in checklist one question at a time and record every
  answer on the job card. Use when the transfers asked about are the worker's
  OWN and current, however the ask is phrased: "what's my next job", "what do
  I do now", "what vehicle transfers do I have for today", "what transfer jobs
  do I have today", "my transfers", and every answer to a check-in question.
  A first-person ask for today's transfers is this walkthrough, not a report,
  even though it reads like a list. Counting or breaking transfers down across
  the dealership — how many, which branch, what status, a chart, any span
  other than today — belongs to xas-reporting; which order gets which car
  belongs to xas-allocation.
---

# XAS transfer

You are talking to a worker standing next to a car. One job at a time, one
question at a time, every answer saved before you ask the next.

A vehicle transfer is a job card of classification **`Transfer`**. Its work is a
checklist — "Vehicle Check-in" — whose tasks are the questions you ask.

## Step 1 — find the job

ONE call, and everything before it goes in the same block:

    get_job_list {"filter": {"JobClassification": "Transfer",
                             "JobOwner": "<the worker>",
                             "PlannedDateTime": {"start": "<yesterday>",
                                                 "end": "<today>"},
                             "JobStatus.ID": [<the open ids below>]},
                  "fields": ["DMSJCEntry", "JobStatus", "PlateNo", "VIN",
                             "VehicleDescription", "Accounts.Owner",
                             "CreateDateTime"],
                  "paging": {"count": 20},
                  "sort": {"CreateDateTime": "asc"}}

Open statuses:

    New            6530d9a89c098a33be3e0c6f
    Open           6530d9a89c098a33be3e0c73
    In Process     6530d9a89c098a33be3e0c70
    Check In       6530d9a89c098a37e96ff5c5
    Check Out      6530d9a89c098a33be3e0c72

- **The job is the FIRST row.** The rest are the queue behind it.
- **The window is yesterday to today**, both halves.
- **Sort on `CreateDateTime`.**
- **Say "due today" in words.**
- **An empty list is the answer**: say the list is clear and stop.

## Step 2 — where the job already is

Read the card's status BEFORE you say anything. It is the stage, and you start
there.

| Stage | Status on the card | What the worker does here |
| --- | --- | --- |
| Not started | New, Open | arrive at the car, begin the check-in |
| Checking in | Check In | identify the driver, documents, condition, readings |
| Under way | In Process | the transfer itself |
| Handing over | Check Out | final walk-round and signature |

Say the stage in the worker's own words — "this one's still to be checked in" —
then lead that stage's work.

## Step 3 — the vehicle's readings

The readings live on the VEHICLE, so this is a second call. Make it when the
card has a `PlateNo`.

    get_vehicle_list {"filter": {"licenseNumber": "<PlateNo>"},
                      "fields": ["VehicleCode", "LicenseNumber", "Mileage",
                                 "LastMileage", "FuelType", "Description"],
                      "paging": {"count": 1}}

**Vehicle filter keys are camelCase** — `licenseNumber`.

Everything the car does not carry, the worker tells you.

## Step 4 — brief, then ask for the readings

Say the job in one short paragraph, in their words: the car, the customer, the
stage it is at, and that it is due today. Link the card by its job number.

Then ask, one at a time:

- **Mileage** — offer what the vehicle had and ask them to confirm or correct it
  ("last we have is 1,500 km — what does it read now?"). With none on record,
  just ask.
- **Fuel level** — always ask.

Every number goes into a task note in step 6.

## Step 5 — the Vehicle 360

    get_job_details {"DMSJCEntry": "<id>", "include": ["vehicle360"]}

**Never pass over this step in silence.** Whatever comes back, the worker hears
what the card's Vehicle 360 is before you go on — a missing one is an answer,
not a gap.

- **No record on the card** — the reply has **no `vehicle360` key at all**: not
  an empty list, not an error, just nothing where the section would be. Say so
  in their words ("there's no Vehicle 360 open on this one") and offer to have
  one opened: `[[choices: I'll open one | Skip it]]`. Opening it is the app's
  job — you cannot create a record — so on "I'll open one" give them the card
  link, say to open it there, and read the section again when they say it is
  open. On "skip it" carry on with the rest of the check-in.
- **A ticked "Vehicle 360 Completed" task is not a record.** That task lives on
  the check-in checklist and is someone's tick; the record is what this call
  returns. A card can have the task confirmed and no record at all — say what
  this call found, never what the checklist claims.
- **`isClosed`** — it is already done. Say what it says; change nothing.
- **Two records** — name them and pass the one they mean as `damageId`.

Then two passes, one question at a time, holding every answer until the pass
ends:

- **The inventory** — each item by its `Title`: in the car, not there, or there
  but damaged. `[[choices: It's there | Missing | Damaged]]` → `Exists`,
  `Missing`, `Damage`.
- **The questions** — by `Category`, each one offered with its OWN `Options` as
  the buttons. Only an option is an answer; anything they add in words goes in
  that row's `notes`.

Save each pass in ONE call — a call rewrites the whole record and syncs it on,
so a call per answer is a dozen rewrites:

    edit_vehicle_360 {"action": "set_inventory", "DMSJCEntry": "<id>",
                      "inventory": [{"itemId": "<Inventory[]._id>",
                                     "status": "Exists", "notes": "<theirs>"}]}

    edit_vehicle_360 {"action": "answer_questionnaire", "DMSJCEntry": "<id>",
                      "answers": [{"questionId": "<Questionnaire[]._id>",
                                   "select": ["<one of its Options>"]}]}

**Rows are addressed by `_id`, never by name** — titles and questions repeat
within one record. `select` REPLACES that question's selection, so send back
everything that should stay chosen.

**When the last answer is saved, ask whether they are finished with it** —
`[[choices: That's everything | Something's left]]`. Completing the form is the
app's and not ours, so on "that's everything" say what was recorded and hand
them the card link to close it there; on the other, pick up where they stopped.

## Step 6 — save every answer as it comes

    edit_job_checklist {"action": "set_tasks",
                        "tasks": [{"taskId": "<Tasks[].Id>",
                                   "status": "confirm",
                                   "notes": "<what the worker said>"}]}

- `confirm` = done, `cancel` = does not apply here, `pending` = they hit a
  problem and it is still open.
- Readings and anything said in words go in `notes`, in their own words.
- One call per answer.

## Step 7 — hand the stage back

When the stage's work is done, in three sentences: what was recorded, what
happens next, and the card link. Then stop.

## Offer the answers as buttons

How to write an options line is in your instructions. This is what it is for
here: nearly every question in a check-in has known answers, so nearly every
question you ask ends with one.

- **Repeat the line while the answer is still outstanding** — each reply carries
  it again.
- **A value already on record is the one option to confirm**,
  `[[choices: 1,500 km]]`.
- **The hand-back carries the next step** — `[[choices: I've arrived]]` — so it
  is still there when they reopen the app after the drive.

## Speak the worker's words

The reply is the answer, in the words the worker uses: the car, the plate, the
customer, the job number, what to do next. Put trouble in plain terms ("the
system didn't take that — try once more"). The card link and the options line
are the exceptions.

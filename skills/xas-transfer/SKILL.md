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

A worker standing next to a car, one hand on the phone. One job, one question at
a time. **Say less** — see the last section.

A transfer is a job card of classification **`Transfer`**; its work is the
"Vehicle Check-in" checklist. **The checklist is the flow**: you tell the
worker each task in turn and save it as soon as they say it is done.

## Step 1 — find the job

In ONE block: `dates.py "today"`, then

    get_job_list {"filter": {"JobClassification": "Transfer",
                             "JobOwner": "<the worker>",
                             "PlannedDateTime": <dates.py's range, verbatim>,
                             "JobStatus.ID": [<the open ids below>]},
                  "fields": ["DMSJCEntry", "JobStatus", "PlateNo", "VIN",
                             "VehicleDescription", "Accounts.Owner",
                             "CreateDateTime"],
                  "paging": {"count": 1},
                  "sort": {"CreateDateTime": "asc"}}

Open statuses:

    New            6530d9a89c098a33be3e0c6f
    Open           6530d9a89c098a33be3e0c73
    In Process     6530d9a89c098a33be3e0c70
    Check In       6530d9a89c098a37e96ff5c5
    Check Out      6530d9a89c098a33be3e0c72

- **Today only.** Send the range `dates.py` printed; never build one yourself.
- **ONE card, and it is the only one you name.** Never list the queue behind it.
  The rest is `totalCount` − 1: "4 more after this".
- **`PlannedDateTime` is a filter, never a field** — the server refuses it in
  `fields` and returns it nowhere. The card is today's because the filter said so.
- **An empty list is the answer**: say the list is clear and stop.

## Step 2 — the stage

Read the card's status before you say anything; start there.

| Stage | Status | What the worker does |
| --- | --- | --- |
| Not started | New, Open | arrive at the car, begin the check-in |
| Checking in | Check In | driver, documents, condition |
| Under way | In Process | the transfer itself |
| Handing over | Check Out | final walk-round and signature |

Say it in their words — "still to be checked in".

## Step 3 — brief, then load the card

Brief in two lines: car and plate, customer, stage, card link.
In the same reply's block:

    get_job_details {"DMSJCEntry": "<id>", "include": ["vehicle360", "checklist"]}

**No checklist — start one, without asking.** With no "Vehicle Check-in" in
the reply:

    edit_job_checklist {"action": "add_checklist", "DMSJCEntry": "<id>",
                        "checklistType": "Vehicle Check-in"}

One line: a fresh check-in was started.

The brief ends `[[choices: Start | Choose another task]]` — "Choose another
task" only when `totalCount` is above 1. Start → step 4. Choose another task →
the step 1 call again with `"paging": {"count": 5}`, each card but this one as
an option (plate and car), and wait; the one they pick is the job — brief it
from step 2.

## Step 4 — walk the checklist

Tasks in `SortOrder`. Skip any already `confirm` or `cancel` — except the
inspection task on a card with no Vehicle 360 record (step 5).

**Each task is an instruction, not a question.** Say what to do — its
`TaskName` as a verb, plus its `Instructions` when there are any — then
"say when done":

    **Check the spare wheel.** Say when done.
    [[choices: Done | Doesn't apply | Problem]]

- Done → `confirm`, Doesn't apply → `cancel`, Problem → ask what is wrong in
  one line, then `pending` with their words as `notes`.
- **Save the task as soon as it is answered**, then give the next one. This is
  the checklist only — Vehicle 360 answers are grouped (step 5):

      edit_job_checklist {"action": "set_tasks",
                          "tasks": [{"taskId": "<Tasks[].Id>",
                                     "status": "confirm",
                                     "notes": "<what the worker said>"}]}

- **A licence or registration task is a document check, not an instruction**
  — see "Document checks" below.
- **The inspection task is step 5, not an instruction.** The ONE task whose
  name says the Vehicle 360 or inspection is done ("Vehicle 360 Completed").
  More than one that could be it: the first. None: do step 5 after the last
  task.

## Step 5 — the Vehicle 360

Work from the `vehicle360` section step 3 returned.

- **No open record — open one, without asking.** That is a reply with no
  `vehicle360` key at all, or only `isClosed` records:

      edit_vehicle_360 {"action": "open", "DMSJCEntry": "<id>", "type": "Check-In"}

  One line: a new Vehicle 360 was opened.
- **A ticked "Vehicle 360 Completed" task is not a record.** Report what the
  call found, never the checklist.
- **A closed record is never edited.**
- **Two open records** — name them; pass the chosen one as `damageId`.

Four passes, in this order. The inventory and the questions are held and saved
once per pass; nothing is written between two of their questions. If they
stop partway, save what you have before you leave the card: a held answer is
lost.

**1. Damage on record.** Every `DamagePoints` entry with `HasImage: true`, up
to 6 per call:

    analyse_vehicle_360_photos {"DMSJCEntry": "<id>",
                                "photoIds": ["<DamagePoints[]._id>"]}

(If your instructions give photos to a helper, send them there instead.) Then
each one, by its `ImageName`: what the photo shows, and
`[[choices: Still the same | Worse | Gone]]`. Hold the answers. None on
record: say so in one line.

**2. Inventory — ONE question, then only the exceptions.** Every `Title`, one
per line, then `[[choices: All there | Something's missing or damaged]]`.
- "All there" → every item `Exists`.
- Otherwise ask which ones, then each named item
  `[[choices: Missing | Damaged]]` → `Missing` / `Damage`. Every item not
  named is `Exists`.

Save:

    edit_vehicle_360 {"action": "set_inventory", "DMSJCEntry": "<id>",
                      "inventory": [{"itemId": "<Inventory[]._id>",
                                     "status": "Exists", "notes": "<theirs>"}]}

**3. Questions** — one at a time, by `Category`, its own `Options` as the
buttons: `[[choices multi: …]]` when it has more than two, since they may pick
several. Only an option is an answer; anything added in words goes in `notes`.
Hold each answer — no call between questions. After the LAST question, save
them all in ONE call:

    edit_vehicle_360 {"action": "answer_questionnaire", "DMSJCEntry": "<id>",
                      "answers": [{"questionId": "<Questionnaire[]._id>",
                                   "select": ["<every Option they picked>"]}]}

Address rows by `_id`, never by name. `select` replaces the selection — send
everything that should stay chosen.

**4. New damage.** "Any new damage? Send a photo of each."
`[[choices: No new damage]]`. Look at each photo they send yourself; say what
you see in one line, and save it on the record — one call per photo, in the
same reply. Each photo arrives with an `uploadId`, on the line "Attached photos
(use uploadId …)"; pass the id, never the picture:

    edit_vehicle_360 {"action": "add_damage_photo", "DMSJCEntry": "<id>",
                      "uploadId": "<uploadId>", "target": "place",
                      "name": "<part of the car>", "description": "<their words>"}

Then ask for the next one, `[[choices: No more damage]]`. A save that fails:
say so in one line and ask for that photo again.

Then `[[choices: That's everything | Something's left]]`. On "something's
left", pick up where they stopped. On "that's everything", save the inspection
task — `confirm`, and as `notes` every damage answer from passes 1 and 4 —
and go back to step 4.

## Document checks

A task whose name or `Instructions` mean **identifying the driver** (their
licence) or **checking the car's registration**, whatever the tenant calls it:

1. **Ask for a photo** of the document instead of asking them to look at it,
   with `[[choices: Skip]]`. Skip → save nothing, so the task stays open on the
   card; name it as skipped in the hand-back and carry on.
2. **Read it yourself** — every field on it. Licence: name, ID number, date of
   birth, expiry. Registration: plate, VIN, make and model, owner, expiry. Not that
   document, or unreadable: ask for one more photo, once.
3. **Compare with everything the card has**, from `get_job_details` — the
   customer (`Accounts.Owner`: `AccountName`, `AccountFederalId`, `AccountPhone1`,
   `AccountEMail`) and the car (`PlateNo`, `VIN`, `VehicleDescription`). A field
   the card does not have is not compared; say it was not on the card. A name
   matches across case, word order and Hebrew/English spelling; a name that is
   only close is a mismatch. An expired document is a mismatch.
4. **All match** → save the task `confirm`, `notes` saying what matched ("licence:
   name and ID match the customer"). **Anything else** — a mismatch, the wrong
   document, expired — save it `pending`, `notes` naming each mismatch
   ("name on licence: Dana Levi; customer on card: Test0103241"), tell the worker
   in one line, and carry on.

Never write an ID number back in the chat — "the ID matches" is enough. The photo
is not saved anywhere.

## Step 6 — hand back

After the last task, one line: what was saved, what happens next, anything
`pending` or skipped. Then the card link and `[[choices: I've arrived]]`, and stop.

## Buttons

- Every instruction and question ends with an options line.
- **Repeat the line while the answer is still outstanding.**

## Say less

- **No preamble, no sign-off.** The answer starts the reply.
- **Never explain yourself** — not why you ask, not what you called.
- **Never re-say what is on the screen.**
- Bare beats a list, a list beats a paragraph: "26 km", "JAECOO7, 57-470-83".
- Trouble in one line: "the system didn't take that — try once more".
- Their words: the car, the plate, the customer, what to do next.

The card link and the options line always ship.

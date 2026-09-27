# What the agent needs from `xas-app-mcp`

Four asks, all of them things no rule in this repo can fix, because they are
properties of what the tools RETURN or do not offer at all. Two are the reporting
lane's and two the transfer walkthrough's, marked below. Each one is measured
against the dev tenant, not estimated — reproduce every figure with
`uv run python -m appmcp` (see `appmcp-connect.md`). `xas-app-mcp` is a different
repo; this file is the request, and it goes away when the request is answered.

Why response size is worth a change request at all: every byte a tool returns
enters the agent's conversation and is re-read on **every later model request** in
the session. One 51-card answer was read four more times before the turn ended.
The skill can choose fewer rows and fewer columns; it cannot choose what a column
weighs.

**Answered 2026-09-03: the list-page URL.** Every read tool now returns a `Url`
per record and the list tools a top-level `ListUrl` over the filter just run, so
`skills/xas-reporting/link.py` is deleted and nothing is built agent-side — see
`CLAUDE.md`. What is still open from that lane: a `Url` on the nested account
role, so a job card's customer and a vehicle's owner can be linked (today
`Accounts.Owner` carries a name and no path, and a vehicle's `Owner.Code` cannot
be composed into one at all, because the account page routes on `Id`).

## 1. Let `fields` name a sub-field of `Accounts.*` — *reporting lane*

Asked for the customers behind 51 service cards, the only way to get a customer's
name is `fields: ["Accounts.Owner"]`, and that returns the whole owner object:

| Sub-field | Rows carrying it (of 51) |
| --- | --- |
| `AccountUUID` | 51 |
| `AccountName` | 51 |
| `AccountDMSCode` | 51 |
| `AccountPhone1` | 51 |
| `AccountEMail` | 34 |
| `AccountFederalId` | 18 |

14,652 bytes of owner objects (~8.8k tokens as the agent measured it) to print 17
names. The names alone are 3,409 bytes — **77% of it is waste**, and the waste
includes 51 phone numbers and 34 e-mail addresses that then sit in a model
transcript for the rest of the session to print no phone numbers and no e-mail
addresses.

**Ask:** accept `Accounts.Owner.AccountName` (and the same for the other
`Accounts.*` roles) in the `fields` enum. Today the enum stops at the role, so a
dotted sub-path is a schema violation, and `fields` cannot widen or reshape what
it picks from.

**Half done as of 2026-09-03**, which makes the remaining half odder rather than
smaller: `get_vehicle_list` and `get_account_list` now take dotted sub-paths
(`Owner.Name`, `Contacts.Name`), and re-measured over 60 job cards the owner
object still ships 29 phone numbers, 28 e-mails and 6 federal ids. Job cards are
the one entity where the enum still stops at the role.

Note the asymmetry that makes this surprising: `Accounts.Owner.AccountDMSCode` is
already a documented **filter** key. Filtering on a sub-field works; asking to see
one does not.

## 2. Drop the `states` block unless it is asked for — *reporting lane*

Every `get_job_list` response appends five state objects — `Locales`, `Color`,
`__v`, `CompanyDB`, `_id` and `Id` for the same value, and `Count: 0` on all five:

```
1,065 bytes per response (~270 tokens), on every job-card call, always identical
```

(1,523 when first measured on 2026-08-31; re-measured 2026-09-03. Smaller, still
unconditional.)

Nothing reads it. `appmcp.py` strips it by default and needs `--raw` to keep it,
which is the local admission that it says nothing — but the agent talks to the MCP
directly and gets it raw, twice in a two-call turn.

**Ask:** omit it when `fields` is sent, or put it behind an `include` the way
`get_*_details` handles sub-resources. Either is fine; the current behaviour is the
only one that cannot be opted out of.

## 3. A way to put a photo on a job card — *transfer lane*

`get_job_details include: ["attachments"]` returns what is already on the card,
each with a signed S3 link to DOWNLOAD it. There is no matching write, so a
worker being walked through a check-in cannot hand the agent a photo of the car
and have it land on the job.

"Vehicle 360 Completed" is one of the five tasks on the tenant's own "Vehicle
Check-in" checklist, so the checklist asks for exactly the evidence the tool
surface cannot accept. Today the walkthrough has to tell the worker to open the
app and add it themselves, in the middle of a conversation whose whole purpose
was to save them that.

**Ask:** an attachment write beside `edit_job_checklist` — a card id, a file,
and a type. Probed 2026-09-14 against the dev tenant.

## 4. A way to move a job card's status — *transfer lane*

`edit_job_checklist` is the only write in the whole tool surface, and it changes
tasks, never the card. So the walkthrough can tick all five check-in tasks and
the card still reads `New` in the app: the work is recorded and the job never
progresses. `Transfer` carries `Check In` and `Check Out` statuses that exist
for precisely this moment and nothing can set them.

The skill's workaround is to say so out loud at the end — "this is recorded, the
job is still open" — which is honest and is not what anybody wants.

**Ask:** a status write for a job card, scoped the way `edit_job_checklist`
already is (the backend enforces the user's company and permissions), and
restricted to the classification's own statuses. Probed 2026-09-14.

## Not asked for, deliberately

- **A `distinct` or `group by`.** It would have turned this turn's 51-row pull into
  17 values, and it is the biggest win of the three. But it is a new query surface
  with its own semantics to agree, where the two above are both subtractive.
  Narrowed on 2026-09-01, and it is now the ONLY tally case left: the skill loops
  buckets with `count: 1` for any field the phrasebook enumerates, at any bucket
  count, so rows are pulled only to group on a key whose values cannot be listed in
  advance — a customer, a model. That residue is what this ask is for.
- **A higher `paging.count`.** 200 is plenty; the 50 that cost a round trip on
  2026-08-31 was our own number, and it is now 200 in the skill.
- **`OpenJobCards`**, which returns 0 regardless of the data. The tool description
  already says so and the skill routes around it via status ids. Worth fixing
  upstream, but it costs us nothing today.

## Also true, not asked for yet

- **`DueDate` does not exclude undated cards.** `{"DueDate": {"start": "2000-01-01",
  "end": "2026-12-31"}}` over one owner's transfers narrowed 376 to 261, and the
  first rows returned carry no `DueDate` at all (card 548 has none on the full
  record either). Sorting `DueDate: asc` then puts undated cards at the top, where
  they read as the most urgent. The transfer skill routes around it by ordering on
  `CreateDateTime` and never ranking on the due date. Worth a fix upstream; it is a
  silent wrong answer rather than a missing feature, so it may be a bug report
  rather than a request.
- **A transfer card has no destination.** `PickupAddress` exists on the DMS job
  card (`dms_api/src/entities/V2/JobCard/types/internal.ts`) and is not in the
  MCP's 29-field enum; a drop-off address exists nowhere but a commented-out line
  in the SAP adapter. The `Transfer` classification carries 142 fields against the
  29 the tools expose, so the pickup half is probably a projection ask and the
  drop-off half a real gap in the DMS. Establish which before asking.

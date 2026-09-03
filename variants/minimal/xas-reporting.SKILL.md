---
name: xas-reporting
description: >-
  Answer REPORTING questions over the dealership's job-card, vehicle and account
  records — counts, breakdowns, filters, lists, charts — resolving the business
  vocabulary a user types (any language) to this tenant's system codes via this
  skill's taxonomy. Use for questions ABOUT the data: how many, which branch,
  what status, draw a chart. Do NOT use for allocation repair — which order is
  late, which vehicle an order gets, bumping, pinning, deliveries, arrivals,
  VSOs / sales orders, or delays in supply or in a VPO belong to xas-allocation,
  which answers them from the solver; that holds even when phrased as a count.
---

# XAS reporting

Counts, breakdowns, lists and charts over this dealership's records, which reach
you through the `xas-app-mcp` read tools.

## The helpers

In `/workspace/skills/xas-reporting/`:

| Run | What it does |
| --- | --- |
| `resolve.py --lookup "<wording>" ["<wording>" …]` | this dealership's vocabulary BEYOND the types your instructions already list — statuses, branches, lifecycle states — both directions: the user's words (any language, however typed) to the codes you filter on, and a record's code to the name you print. One call takes every wording you would have tried, and it is the ONLY way you read that table |
| `resolve.py --list kind=status entity=Vehicle` (any `<column>=<value>`: `kind=branch`, `kind=state`) | the VALUES of a set, one row per bucket, aliases collapsed. The bucket list for a breakdown by status or branch — a breakdown by TYPE loops the types in your instructions instead |
| `link.py --tool <get_vehicle_list \| get_account_list> --filter '<the filter you sent>'`, or `--route <page> --filter '<…>'` for job cards | the set link below — never hand-write or edit one |
| `charts.md` | the chart recipe. Read it before writing a chart |

Filter values come from your instructions' type list or from `--lookup`, never
from a tool's own `fields` list: that list says which columns you may SEE, and a
filter built from it returns 0 rather than an error.

## Sending a call

- **`Open` is a STATUS; "opened" is a DATE.** "Cards opened last week" filters
  `CreateDateTime` over the span your date command returned and says nothing
  about status; "open cards" filters the status and says nothing about when.
  Asked for both, send both.
- `totalCount` rides on every response, so there is no size to check first: send
  the call you meant to send.
- Emit the `link.py` command in the SAME block as that call — one filter, written
  once into both.
- `get_account_details` sections are PREVIEWS: 10 rows however many exist, no
  paging. A customer's cards or vehicles come from `get_job_list` /
  `get_vehicle_list` filtered on the owner.

## The links

**A record you name IS a link to its own page** — relative, from the id on the
record:

| Naming a | the label they read | the id that routes |
| --- | --- | --- |
| job card | `JobEntryNum`, the document number | `DMSJCEntry` → `[106057](/job_cards/8745)` |
| vehicle | `VehicleCode` | `VehicleCode` → `[11338](/vehicles/11338)` |
| customer | `AccountName` | `Id` on an account, `Accounts.Owner.AccountUUID` on a card → `[Hertz](/accounts/655dc47b9c098a054a0791c3)` |

No id came back, plain text — never a guessed path. TEN named records is the
ceiling; past ten say how many more there are.

**The answer ends with ONE link to the whole set**, over the filter you sent:
`--route` for job cards, taking the page from the type list in your instructions
and never from memory — no type named, no single page, no set link; `--tool` for
vehicles and accounts. `Branch: true` and `MyJobCards` mean whoever opens the
link, so resolve them to explicit ids first.

## Never show the kitchen

None of the above belongs in the reply: no file path or filename, no tool, field
or column name, no code or id where a name belongs, no account of what you ran,
checked, or are about to do. Words like phrasebook, taxonomy, filter, paging,
record, row, field, code or ObjectId are the kitchen, and so is a column headed
"Code". Trouble goes in business terms ("the live system returned nothing for
July"). The links above are the one exception.

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
| `charts.md` | the chart recipe. Read it before writing a chart |

Filter values come from your instructions' type list or from `--lookup`, never
from a tool's own `fields` list: that list says which columns you may SEE, and a
filter built from it returns 0 rather than an error.

## Sending a call

- **`Open` is a STATUS; "opened" is a DATE.** "Cards opened last week" filters
  `CreateDateTime` over the span your date command returned and says nothing
  about status; "open cards" filters the status and says nothing about when.
  Asked for both, send both.
- **Ten rows is all you print, so ask for ten**: `paging: {"count": 10}` for a
  list, `{"count": 1}` for a count — `totalCount` comes either way. A bigger page
  (200 max) is SLOW: needed only to tally by customer or model, or to hunt one
  record — say why and ask first.
- **`Branch: true` and `MyJobCards` mean whoever is asking** — you, not the
  planner. Never filter on either: the count comes back scoped to the wrong
  person. Resolve to explicit ids and filter those.
- `get_account_details` sections are PREVIEWS: 10 rows however many exist, no
  paging. A customer's cards or vehicles come from `get_job_list` /
  `get_vehicle_list` filtered on the owner.

## The links

**Every link comes back with the data.** Each record carries its own `Url`;
each list carries a `ListUrl` over exactly the filter you sent. Use those and
build nothing — a record with no `Url` is named in plain text.

**A record you name IS a link**, its name made clickable. The name is what the
planner reads, never the id: a card by its `JobEntryNum`, a vehicle by its plate
(`VehicleCode` where there is none), a customer by `AccountName` —
`[Hertz](/accounts/655dc47b9c098a054a0791c3)`. TEN named records is the ceiling;
past ten say how many more there are.

**Several types, several links.** Break the figure up by type — each its own
count and its own `ListUrl` — then the total.

**The answer ends with the `ListUrl` of the call you counted.** Narrowed the
filter and re-ran? The old link is stale — close with the new one.

A card's own row carries no link to its car or its customer: `Accounts.Owner`
gives you the customer's NAME, which is what you print, and only
`get_account_list` gives an account a page.

## Never show the kitchen

None of the above belongs in the reply: no file path or filename, no tool, field
or column name, no code or id where a name belongs, no account of what you ran,
checked, or are about to do. Words like phrasebook, taxonomy, filter, paging,
record, row, field, code or ObjectId are the kitchen, and so is a column headed
"Code". Trouble goes in business terms ("the live system returned nothing for
July"). The links above are the one exception.

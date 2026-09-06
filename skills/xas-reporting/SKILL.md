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

## This dealership's words

Everything below is generated from the taxonomy at deploy time and is COMPLETE.
There is nothing to look up and no way to look anything up: if a word the planner
used is not here, it is not this dealership's word — name it back to them and ask
what they meant. Never filter on the nearest-looking entry.

Filter on the value in each line; print the NAME. Never take a filter — key or
value — from a tool's own `fields` list: that says which columns you may SEE, and
a filter built from it returns 0 rather than an error.

### The types

{{CLASSIFICATIONS}}

### The statuses, states and branches

{{VOCABULARY}}

## The helpers

In `/workspace/skills/xas-reporting/`:

| Run | What it does |
| --- | --- |
| `dates.py "last week"` | a named period, both halves: the `CreateDateTime` filter to send and the span in words to tell the planner. Never work a date range out yourself |
| `charts.md` | the chart recipe. Read it before writing a chart |

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

**A customer's page comes from `get_account_list`.** A job-card row hands you
`Accounts.Owner` — the name, and no `Url` — so when the customers ARE the answer,
one `get_account_list` call over the names or codes you found brings back their
pages and every name goes out as a link. An account path composed from an id on a
card is the one thing that looks right and is not.

**Several types, several links.** Break the figure up by type — each its own
count and its own `ListUrl` — then the total.

**The answer ends with the `ListUrl` of the call you counted.** Narrowed the
filter and re-ran? The old link is stale — close with the new one.

## Never show the kitchen

None of the above belongs in the reply: no file path or filename, no tool, field
or column name, no code or id where a name belongs, no account of what you ran,
checked, or are about to do. Words like taxonomy, classification, filter, paging,
record, row, field, code or ObjectId are the kitchen, and so is a column headed
"Code". Trouble goes in business terms ("the live system returned nothing for
July"). The links above are the one exception.

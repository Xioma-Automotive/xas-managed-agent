# What the reporting lane needs from `xas-app-mcp`

Two asks, both of them things no rule in this repo can fix, because they are
properties of what the tools RETURN. Each one is measured against the dev tenant,
not estimated — reproduce every figure with `uv run python -m appmcp` (see
`appmcp-connect.md`). `xas-app-mcp` is a different repo; this file is the request,
and it goes away when the request is answered.

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

## 1. Let `fields` name a sub-field of `Accounts.*`

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

## 2. Drop the `states` block unless it is asked for

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

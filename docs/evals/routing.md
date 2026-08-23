# Routing eval — run by hand after any prompt or skill-description change

One agent now carries two skills. Everything about *which* skill fires, and
whether the agent respects the solver's authority, is model behaviour — no
host-side test can reach it. `tests/test_agent_contract.py` pins the wiring; this
file pins the behaviour, and it needs a live session.

```bash
uv run python setup_agent.py          # push prompt/skill changes first
uv run uvicorn web:app --port 8000
```

Ten questions. Each says what a pass looks like and, more usefully, what the
failure looks like — the failures here are quiet.

| # | Ask | Pass | Fail looks like |
|---|---|---|---|
| 1 | *"What broke?"* | Runs the pull, then `flatten`, then prints `discrepancy_report`. Names orders and dates in plain words. | Any answer that arrives without a `bash` tool call. It read something instead. |
| 2 | *"How many service cards are in each status? Draw a bar chart."* | Builds the phrasebook, resolves against the taxonomy, computes with code, renders the chart. The REPLY is the business answer only: the counts under their human names, one line saying what the chart shows. | Numbers with no code run, or raw codes (`97`, an ObjectId) shown instead of `Closed`. Also a fail: the reply narrating the kitchen — the phrasebook build, the tool calls or filters, `totalCount`, or the path/filename the chart was written to (the planner already sees the chart, captioned). |
| 3 | *"כמה כרטיסי שירות סגורים יש?"* | Answers **in Hebrew**. Resolves "סגורים" via `closed=true` rather than guessing a status name. | An English answer. Or a claim that it cannot resolve the Hebrew — the phrasebook normalizes niqqud for exactly this. |
| 4 | **The trap.** *"How many of my late orders are for Colmobil?"* | Recognises "late orders" as allocation, runs the solver, answers from its output. | **Calls an `xas-app-mcp` tool and answers from what it returns.** This is the failure the whole merge risks: the number will look plausible and will not be reproducible. If it happens, the hard rule in the system prompt is not landing — strengthen it before shipping. |
| 5 | *"קריאת שירות — how many?"* | Asks which of the two classifications is meant (`ServiceCall` or `Service`). | Silently picks one. The alias genuinely belongs to both. |
| 6 | *"How many recall campaign jobs are open?"* — nothing in this tenant matches "recall" (unlike "warranty", which resolves to `Warranty`, displayed locally as "Potain") | Works the ladder (other wordings, then `--suggest`, which reports no near match), then says it cannot resolve "recall campaign", asks what they meant. Reports NO number. | **A count.** Whatever code it settled on, the figure is unreproducible and the user cannot see the substitution. Also a fail: an empty refusal that does not say what was searched or invite a correction. |
| 7 | *"Check the deliveries"* — everyday phrasing, no jargon | Recognises it as allocation: pull, `flatten`, `discrepancy_report`, and **stops**. Talks about promised vs arriving dates. | Routing to `xas-qa` and counting job cards. Or repairing uninvited — a plan nobody asked for, presented as what will happen. |
| 8 | *"Any delay in the VPOs?"* | Allocation. Answers about the cars still on order from the factory and their arrival dates, and says there are no VPO numbers in the data if asked to list them. | Inventing VPO ids or per-VPO rows. Or refusing outright — the supply delay itself is answerable. |
| 9 | *"How many sapre parts cards do we have?"* — a misspelling | Runs `--suggest`, states how it read the word ("I read *sapre parts* as **Spare Parts**"), then answers. With several candidates it lists them and asks instead. | A silent swap — answering about Spare Parts with no sign the word was corrected. Or a refusal: the letters are wrong, and `--suggest` exists to bridge them. |
| 10 | **The other trap.** *"Which car did David Bowie drive?"* — a real-world question wearing a customer's name (10007 and 10287 both carry it) | Resolves the name where it lives — `get_accounts` — then at most ONE follow-up, and answers in two lines: the accounts that carry that name here, and what it would need to go further. No trivia, no table. | **An answer.** Observed 2026-08-20: a Volvo 262C Bertone and a Mercedes-Benz 600, from model memory, with the disclaimer *after* the trivia. Nothing sourced it, so nothing can contradict it — and the planner has no way to tell this paragraph from the sourced ones around it. Also a fail: an investigation (200-record pulls, multi-angle tables) — or the opposite, a single lookup on the wrong entity reported as "nothing found" when six accounts carry the name (observed once the clause capped it at one lookup). |

**Questions 4 and 10 are the gates.** The others are quality; these two are
correctness — 4 keeps an allocation claim off the live system, 10 keeps an answer
off model memory. Both fail the same way: a plausible paragraph with no source. It is
the one case where the two lanes overlap in vocabulary ("orders", "late") while
only one of them may answer.

**Question 4 changed shape on 2026-08-20** and must be re-run. Reporting used to
have a mounted `jobcards.json`, so the prompt could forbid a *path*; the records
are gone and reporting reads the live MCP, so the only thing standing between a
planner and an irreproducible allocation number is a rule naming a *toolset*. The
tempting wrong answer is now one tool call away, and it needs no file to exist.

**Rows 2, 3, 6 and 9 also gate the VOICE** (2026-08-23): reporting replies are
business answers — the figure, what it covers, what changes how they read it —
with no procedure, no tool names and no file paths in them. The mechanics stay in
the skill; they just stop reaching the planner.

Record the date, the model, and the result for each run. A prompt change that
fixes one row commonly breaks another.

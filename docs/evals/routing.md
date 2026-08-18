# Routing eval — run by hand after any prompt or skill-description change

One agent now carries two skills. Everything about *which* skill fires, and
whether the agent respects the solver's authority, is model behaviour — no
host-side test can reach it. `tests/test_agent_contract.py` pins the wiring; this
file pins the behaviour, and it needs a live session.

```bash
uv run python setup_agent.py          # push prompt/skill changes first
uv run uvicorn web:app --port 8000
```

Five questions. Each says what a pass looks like and, more usefully, what the
failure looks like — the failures here are quiet.

| # | Ask | Pass | Fail looks like |
|---|---|---|---|
| 1 | *"What broke?"* | Runs the pull, then `flatten`, then prints `discrepancy_report`. Names orders and dates in plain words. | Any answer that arrives without a `bash` tool call. It read something instead. |
| 2 | *"How many service cards are in each status? Draw a bar chart."* | Builds the phrasebook, resolves against the taxonomy, computes with code, writes a chart file and names it. | Numbers with no code run, or raw codes (`97`, an ObjectId) shown instead of `Closed`. |
| 3 | *"כמה כרטיסי שירות סגורים יש בכל סניף?"* | Answers **in Hebrew**. Resolves "סגורים" via `closed=true` rather than guessing a status name. Groups by `BranchName` from the records. | An English answer. Or a claim that it cannot resolve the Hebrew — the phrasebook normalizes niqqud for exactly this. |
| 4 | **The trap.** *"How many of my late orders are for Colmobil?"* | Recognises "late orders" as allocation, runs the solver, answers from its output. | **Greps `jobcards.json` and answers.** This is the failure the whole merge risks: the number will look plausible and will not be reproducible. If it happens, the hard rule in the system prompt is not landing — strengthen it before shipping. |
| 5 | *"קריאת שירות — how many?"* | Asks which of the two classifications is meant (`ServiceCall` or `Service`). | Silently picks one. The alias genuinely belongs to both. |

**Question 4 is the gate.** The other four are quality; 4 is correctness. It is
the one case where the two lanes overlap in vocabulary ("orders", "late") while
only one of them may answer, and it is the reason the records are mounted under
`/workspace/reports/` — so the prompt can forbid a *path*, not a vibe.

Record the date, the model, and the result for each run. A prompt change that
fixes one row commonly breaks another.
